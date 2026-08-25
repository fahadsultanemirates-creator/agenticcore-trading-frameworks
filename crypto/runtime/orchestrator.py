"""
orchestrator.py – Cycle runner for Crypto Standard Tier 1.

Responsibilities:
- Fetch public market data via MexcPublicClient.
- Filter and rank the universe via scanner.
- Score top candidates via signals.
- Write runtime/state.json and logs/audit.jsonl atomically.
- No order submission, no private credentials, no live execution.

Signal/paper mode is enforced; any non-signal call raises RuntimeError.
"""

from __future__ import annotations

import os
import sys
import traceback
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from adapters.mexc_public import MexcApiError, MexcPublicClient
from adapters.market_context import fetch_market_context
from analysis.cross_market import apply_cross_market_confirmation
from analysis.microstructure import analyze_microstructure
from analysis.quality import return_correlation
from analysis.scanner import filter_universe, rank_candidates
from analysis.signals import score_snapshot
from config.settings import CryptoSettings
from domain.models import (
    Candidate,
    CandidateStatus,
    DataStatus,
    DailyGuardStatus,
    EntryStatus,
    FrameworkConfig,
    FrameworkMode,
    FrameworkState,
    MarketSnapshot,
    MarketMover,
    ReadinessStatus,
    SignalStatus,
    pending_state,
)
from risk.daily_guard import evaluate_daily_guard, is_daily_guard_halted
from runtime.paper_positions import (
    load_open_paper_positions,
    load_paper_daily_pnl,
    update_paper_positions,
)
from runtime.notifications import emit_cycle_notifications
from runtime.pending_setups import apply_pending_setup_expiry
from runtime.trade_intelligence import analyze_paper_events
from storage.state import append_jsonl, now_iso, write_json_atomic
from storage.memory import CoinMemoryStore


def _make_config_snapshot(settings: CryptoSettings) -> FrameworkConfig:
    return FrameworkConfig(
        signal_mode=settings.signal_mode,
        candidate_count=settings.candidate_count,
        max_open_positions=settings.max_open_positions,
        position_notional_usdt=settings.position_notional_usdt,
        max_isolated_margin_per_position_usdt=settings.max_isolated_margin_per_position_usdt,
        daily_loss_limit_usdt=settings.daily_loss_limit_usdt,
        daily_profit_target_usdt=settings.daily_profit_target_usdt,
        take_profit_usdt=settings.take_profit_usdt,
        basket_profit_target_usdt=settings.basket_profit_target_usdt,
        stop_atr_period=settings.stop_atr_period,
        stop_atr_multiplier=settings.stop_atr_multiplier,
        minimum_stop_pct=settings.minimum_stop_pct,
        maximum_stop_pct=settings.maximum_stop_pct,
        profit_lock_activation_pct=settings.profit_lock_activation_pct,
        profit_lock_protection_pct=settings.profit_lock_protection_pct,
        leverage_min=settings.leverage_min,
        leverage_max=settings.leverage_max,
        margin_mode=settings.margin_mode,
    )


def _audit_event(
    audit_path: str,
    event: str,
    status: str,
    detail: str,
    extra: Optional[Dict[str, Any]] = None,
) -> None:
    record: Dict[str, Any] = {
        "time": now_iso(),
        "event": event,
        "status": status,
        "detail": detail,
    }
    if extra:
        record.update(extra)
    append_jsonl(audit_path, record)


def _clear_plan(candidate: Candidate) -> None:
    """Leave the observation visible while removing any blocked paper plan."""
    candidate.planned_quantity = None
    candidate.planned_margin_usdt = None
    candidate.planned_stop_price = None
    candidate.planned_take_profit_price = None
    candidate.profit_lock_trigger_price = None
    candidate.profit_lock_stop_price = None
    candidate.planned_target_profit_usdt = None


def _apply_coin_metadata(
    candidates: List[Candidate],
    market_context: Dict[str, Any],
) -> None:
    """Attach only unambiguous cap/supply metadata; it never changes a signal."""
    raw_coins = market_context.get("coingecko", {}).get("coins", [])
    if not isinstance(raw_coins, list):
        return
    by_symbol: Dict[str, List[Dict[str, Any]]] = {}
    for item in raw_coins:
        if not isinstance(item, dict):
            continue
        symbol = str(item.get("symbol") or "").upper()
        if symbol:
            by_symbol.setdefault(symbol, []).append(item)
    for candidate in candidates:
        base = candidate.symbol.upper().replace("_USDT", "").replace("USDT", "")
        matches = by_symbol.get(base, [])
        if len(matches) != 1:
            continue
        coin = matches[0]
        candidate.market_cap_usd = coin.get("market_cap_usd")
        candidate.market_cap_rank = coin.get("market_cap_rank")
        candidate.fully_diluted_valuation_usd = coin.get("fully_diluted_valuation_usd")
        candidate.circulating_supply = coin.get("circulating_supply")
    # CoinMarketCap is optional and never replaces an unambiguous CoinGecko
    # match. It fills only missing public metadata when the operator supplies
    # its credential through the secret mechanism.
    cmc_coins = market_context.get("coinmarketcap", {}).get("coins", [])
    if not isinstance(cmc_coins, list):
        return
    cmc_by_symbol: Dict[str, List[Dict[str, Any]]] = {}
    for item in cmc_coins:
        if isinstance(item, dict) and item.get("symbol"):
            cmc_by_symbol.setdefault(str(item["symbol"]).upper(), []).append(item)
    for candidate in candidates:
        if candidate.market_cap_usd is not None:
            continue
        base = candidate.symbol.upper().replace("_USDT", "").replace("USDT", "")
        matches = cmc_by_symbol.get(base, [])
        if len(matches) != 1:
            continue
        coin = matches[0]
        candidate.market_cap_usd = coin.get("market_cap_usd")
        candidate.fully_diluted_valuation_usd = coin.get("fully_diluted_valuation_usd")
        candidate.circulating_supply = coin.get("circulating_supply")


def _memory_operation(settings: CryptoSettings, operation: Any) -> tuple[Any, Optional[str]]:
    """Run a memory operation without making storage availability a trade signal."""
    try:
        with CoinMemoryStore(settings.memory_db_path) as memory:
            return operation(memory), None
    except Exception as exc:
        return None, str(exc)


def _set_memory_error(state: FrameworkState, error: Optional[str]) -> None:
    if error:
        state.memory_status = "degraded"
        state.memory_error = error[:500]


def _defer_paper_entry(candidate: Candidate, reason: str) -> None:
    """Keep the setup visible, but make a stale or moved price non-tradable."""
    _clear_plan(candidate)
    candidate.entry_status = EntryStatus.PENDING
    candidate.selection_status = CandidateStatus.PENDING
    candidate.note = f"{candidate.note}; {reason}".strip("; ")


def _revalidate_paper_entries(
    client: MexcPublicClient,
    candidates: List[Candidate],
    existing_open_positions: List[Dict[str, Any]],
    settings: CryptoSettings,
    audit_path: str,
) -> Dict[str, Any]:
    """
    Fetch a fresh public ticker snapshot immediately before a paper entry.

    A planned trade is not allowed to use a price left over from the earlier
    scan. The structural plan is intentionally not recomputed here; an absent,
    invalidated, or moved-outside-zone setup is deferred to the next complete
    market-analysis cycle instead.
    """
    open_count = sum(
        1
        for position in existing_open_positions
        if position.get("status") == "open"
    )
    capacity = max(0, settings.max_open_positions - open_count)
    ready = [
        candidate
        for candidate in candidates
        if candidate.entry_status == EntryStatus.CONFIRMED
        and candidate.planned_side in ("long", "short")
        and candidate.planned_quantity is not None
    ]
    if capacity == 0 or not ready:
        return {}

    try:
        fresh_tickers = {
            ticker.symbol: ticker
            for ticker in client.get_all_tickers()
            if ticker.last_price is not None and ticker.last_price > 0
        }
    except Exception as exc:
        for candidate in ready:
            _defer_paper_entry(candidate, "final entry recheck unavailable; deferred")
        _audit_event(
            audit_path,
            "paper_entry_recheck",
            "unavailable",
            "Fresh MEXC ticker recheck failed; all new paper entries deferred",
            {"error": str(exc)[:300], "symbols": [candidate.symbol for candidate in ready]},
        )
        return {}

    accepted: Dict[str, Any] = {}
    for candidate in ready:
        ticker = fresh_tickers.get(candidate.symbol)
        if ticker is None:
            _defer_paper_entry(candidate, "final entry ticker unavailable; deferred")
            continue
        price = ticker.last_price
        if (
            candidate.entry_zone_low is None
            or candidate.entry_zone_high is None
            or not candidate.entry_zone_low <= price <= candidate.entry_zone_high
        ):
            _defer_paper_entry(candidate, "price left confirmed entry zone; deferred")
            continue
        if (
            candidate.planned_side == "long"
            and candidate.entry_invalidation_price is not None
            and price <= candidate.entry_invalidation_price
        ) or (
            candidate.planned_side == "short"
            and candidate.entry_invalidation_price is not None
            and price >= candidate.entry_invalidation_price
        ):
            _defer_paper_entry(candidate, "entry invalidation reached on final recheck; deferred")
            continue
        candidate.last_price = price
        accepted[candidate.symbol] = ticker

    _audit_event(
        audit_path,
        "paper_entry_recheck",
        "configured",
        f"{len(accepted)} of {len(ready)} paper entries revalidated",
        {
            "accepted_symbols": sorted(accepted),
            "deferred_symbols": sorted(
                candidate.symbol
                for candidate in ready
                if candidate.symbol not in accepted
            ),
        },
    )
    return accepted


def _apply_portfolio_controls(
    scored: List[Candidate],
    snapshots: List[MarketSnapshot],
    settings: CryptoSettings,
    existing_positions: Optional[List[Dict[str, Any]]] = None,
) -> List[Candidate]:
    """
    Stop a theoretical basket from silently concentrating isolated margin into
    highly-correlated, same-direction coins. It only changes local paper plans.
    """
    snapshot_map = {snapshot.symbol: snapshot for snapshot in snapshots}
    existing_positions = existing_positions or []
    accepted: List[Candidate] = []
    planned_margin = sum(
        float(position.get("initial_margin_usdt") or settings.max_isolated_margin_per_position_usdt)
        for position in existing_positions
    )
    for candidate in scored:
        if candidate.planned_quantity is None or candidate.planned_side is None:
            candidate.correlation_status = "not_planned"
            continue
        if candidate.planned_margin_usdt is None:
            _clear_plan(candidate)
            candidate.correlation_status = "blocked"
            candidate.note += "; missing isolated-margin estimate"
            continue
        blocked = False
        # Compare every fresh plan with persisted paper exposure first. Symbols
        # are deliberately probed with the ranked universe below; if aligned
        # data is unavailable, the additional same-side plan is blocked.
        for position in existing_positions:
            if position.get("side") != candidate.planned_side:
                continue
            existing_symbol = str(position.get("symbol") or "")
            existing_snapshot = snapshot_map.get(existing_symbol)
            if existing_snapshot is None:
                _clear_plan(candidate)
                candidate.correlation_status = "blocked"
                candidate.note += f"; correlation data unavailable for open {existing_symbol}"
                blocked = True
                break
            correlation = return_correlation(
                snapshot_map[candidate.symbol].candles,
                existing_snapshot.candles,
            )
            if correlation is None or abs(correlation) >= settings.max_correlation:
                _clear_plan(candidate)
                candidate.correlation_status = "blocked"
                detail = "unavailable" if correlation is None else f"{correlation:.2f}"
                candidate.note += f"; correlation blocked ({detail} with open {existing_symbol})"
                blocked = True
                break
        if blocked:
            continue
        for existing in accepted:
            if existing.planned_side != candidate.planned_side:
                continue
            correlation = return_correlation(
                snapshot_map[candidate.symbol].candles,
                snapshot_map[existing.symbol].candles,
            )
            if correlation is None:
                _clear_plan(candidate)
                candidate.correlation_status = "blocked"
                candidate.note += f"; correlation unavailable with {existing.symbol}"
                blocked = True
                break
            if abs(correlation) >= settings.max_correlation:
                _clear_plan(candidate)
                candidate.correlation_status = "blocked"
                candidate.note += f"; correlation blocked ({correlation:.2f} with {existing.symbol})"
                blocked = True
                break
        if blocked:
            continue
        if (
            planned_margin + candidate.planned_margin_usdt
            > settings.max_total_isolated_margin_usdt
        ):
            _clear_plan(candidate)
            candidate.correlation_status = "blocked"
            candidate.note += "; basket isolated-margin cap blocked"
            continue
        candidate.correlation_status = "clear"
        planned_margin += candidate.planned_margin_usdt
        accepted.append(candidate)
    return scored


def _movers(
    symbols: List[str],
    tickers: Dict[str, Any],
    reverse: bool,
    limit: int = 5,
) -> List[MarketMover]:
    movers = [
        MarketMover(
            symbol=symbol,
            change_pct_24h=tickers[symbol].change_pct_24h,
            turnover_24h_usdt=tickers[symbol].turnover_24h_usdt,
            spread_pct=tickers[symbol].spread_pct,
        )
        for symbol in symbols
        if tickers.get(symbol) is not None and tickers[symbol].change_pct_24h is not None
    ]
    return sorted(movers, key=lambda item: item.change_pct_24h or 0.0, reverse=reverse)[:limit]


def _finalize_cycle(
    settings: CryptoSettings,
    state_path: str,
    state: FrameworkState,
    paper_events: Optional[List[Dict[str, Any]]] = None,
    risk_blocked_symbols: Optional[List[str]] = None,
) -> FrameworkState:
    """Persist a completed/failed state and emit its safe operator updates."""
    _save_state(state_path, state)
    emit_cycle_notifications(
        settings,
        state,
        paper_events or [],
        risk_blocked_symbols=risk_blocked_symbols,
    )
    return state


def run_cycle(
    settings: CryptoSettings,
    client: Optional[MexcPublicClient] = None,
    cycle_count: int = 0,
    daily_pnl_usdt: Optional[float] = None,
) -> FrameworkState:
    """
    Execute one full market-data cycle.

    Parameters
    ----------
    settings    : CryptoSettings
    client      : MexcPublicClient (injectable for tests); created if None.
    cycle_count : int – monotonic counter to embed in state.
    daily_pnl_usdt : Optional current session P&L from a future paper/private
                     account feed. None remains explicitly unknown.

    Returns
    -------
    FrameworkState – complete state written to disk.
    """
    settings.validate()
    if not settings.signal_mode:
        raise RuntimeError(
            "Live execution is not implemented in Tier 1. signal_mode must be True."
        )

    state_path = os.path.join(settings.runtime_dir, "state.json")
    audit_path = os.path.join(settings.log_dir, "audit.jsonl")
    paper_path = os.path.join(settings.runtime_dir, "paper_positions.json")

    config_snapshot = _make_config_snapshot(settings)
    persisted_paper_pnl = load_paper_daily_pnl(paper_path) if settings.paper_trading_enabled else None
    persisted_open_positions = (
        load_open_paper_positions(paper_path) if settings.paper_trading_enabled else []
    )
    effective_daily_pnl = daily_pnl_usdt if daily_pnl_usdt is not None else persisted_paper_pnl
    daily_guard_status = evaluate_daily_guard(effective_daily_pnl, settings)

    if client is None:
        client = MexcPublicClient(timeout=settings.request_timeout_seconds)

    state = FrameworkState(
        exchange="MEXC Futures",
        mode=FrameworkMode.PAPER if settings.paper_trading_enabled else FrameworkMode.SIGNAL,
        market_data_status=ReadinessStatus.NOT_CONNECTED,
        account_status=ReadinessStatus.UNKNOWN,
        execution_status=ReadinessStatus.NOT_CONNECTED,
        blockchain_status=ReadinessStatus.PENDING,
        last_sync=None,
        config=config_snapshot,
        candidates=[],
        open_positions=[],
        cycle_count=cycle_count,
        daily_pnl_usdt=effective_daily_pnl,
        daily_guard_status=daily_guard_status,
        scan_coverage={},
        memory_status="initializing",
    )

    _audit_event(audit_path, "cycle_start", "pending", f"Cycle {cycle_count} started")
    _, memory_error = _memory_operation(
        settings,
        lambda memory: memory.record_heartbeat(
            cycle_count,
            "started",
            0,
            0,
            0,
            len(persisted_open_positions),
        ),
    )
    _set_memory_error(state, memory_error)
    if not memory_error:
        state.memory_status = "healthy"

    if is_daily_guard_halted(daily_guard_status) and not settings.paper_trading_enabled:
        state.last_error = f"Daily guard halted: {daily_guard_status}"
        _audit_event(
            audit_path,
            "daily_guard",
            "halted",
            state.last_error,
            {"daily_pnl_usdt": effective_daily_pnl},
        )
        return _finalize_cycle(settings, state_path, state)

    # ── Step 1: Fetch contract list ────────────────────────────────────────────
    contracts = []
    try:
        contracts = client.get_contract_list()
        _audit_event(
            audit_path, "contract_list", "configured",
            f"Fetched {len(contracts)} contracts",
        )
    except Exception as exc:
        _audit_event(audit_path, "contract_list", "not_connected", str(exc))
        state.last_error = f"Contract list: {exc}"
        return _finalize_cycle(settings, state_path, state)

    if not contracts:
        _audit_event(audit_path, "contract_list", "unknown", "Empty contract list returned")
        state.last_error = "Empty contract list"
        return _finalize_cycle(settings, state_path, state)

    # ── Step 2: Fetch tickers ──────────────────────────────────────────────────
    tickers = {}
    try:
        ticker_list = client.get_all_tickers()
        tickers = {t.symbol: t for t in ticker_list}
        _audit_event(
            audit_path, "tickers", "configured",
            f"Fetched {len(tickers)} tickers",
        )
        state.market_data_status = ReadinessStatus.CONFIGURED
    except Exception as exc:
        _audit_event(audit_path, "tickers", "not_connected", str(exc))
        state.last_error = f"Tickers: {exc}"
        return _finalize_cycle(settings, state_path, state)

    # ── Step 3: Filter universe ────────────────────────────────────────────────
    contract_map = {c.symbol: c for c in contracts}
    try:
        filtered = filter_universe(contract_map.values(), tickers, settings)
        _audit_event(
            audit_path, "universe_filter", "configured",
            f"{len(filtered)} symbols passed universe filter",
        )
    except Exception as exc:
        _audit_event(audit_path, "universe_filter", "unknown", str(exc))
        state.last_error = f"Universe filter: {exc}"
        return _finalize_cycle(settings, state_path, state)

    # ── Step 4: Rank and probe top candidates ─────────────────────────────────
    ranked = rank_candidates(filtered, tickers, settings.probe_limit)
    # Preserve candle coverage for open paper exposure even when it falls below
    # the fresh liquidity ranking; new same-side exposure must then correlate
    # against it or fail closed.
    for position in persisted_open_positions:
        symbol = str(position.get("symbol") or "")
        if symbol and symbol in contract_map and symbol not in ranked:
            ranked.append(symbol)
    _audit_event(
        audit_path, "ranking", "configured",
        f"Top {len(ranked)} symbols selected for probing",
    )
    state.scan_coverage = {
        "contracts_discovered": len(contracts),
        "tickers_received": len(tickers),
        "liquidity_eligible": len(filtered),
        "deep_probed": len(ranked),
        "microstructure_probed": min(len(ranked), settings.microstructure_probe_limit),
        "selected": 0,
    }
    _, memory_error = _memory_operation(
        settings,
        lambda memory: memory.record_universe(
            cycle_count,
            contracts,
            tickers,
            filtered,
            ranked,
        ),
    )
    _set_memory_error(state, memory_error)

    # ── Step 5: Fetch detailed data for probed symbols ────────────────────────
    snapshots: List[MarketSnapshot] = []
    for symbol in ranked:
        contract = contract_map.get(symbol)
        ticker = tickers.get(symbol)

        snapshot = MarketSnapshot(
            symbol=symbol,
            contract=contract,
            ticker=ticker,
            candles=[],
            mid_candles=[],
            funding=None,
            open_interest=None,
            data_status=DataStatus.PENDING,
            data_error=None,
        )

        # Candles
        try:
            candles = client.get_candles(
                symbol=symbol,
                interval="Min15",
                limit=settings.candle_limit,
            )
            snapshot.candles = candles
        except Exception as exc:
            snapshot.data_error = f"candles: {exc}"

        # 4-hour context anchors approximately two days of support/resistance.
        try:
            snapshot.context_candles = client.get_candles(
                symbol=symbol,
                interval="Hour4",
                limit=settings.context_candle_limit,
            )
        except Exception as exc:
            snapshot.data_error = (snapshot.data_error or "") + f"; context candles: {exc}"

        # 1-hour candles preserve the recent multi-hour cycle between the local
        # entry timeframe and the wider 4-hour structure.
        try:
            snapshot.mid_candles = client.get_candles(
                symbol=symbol,
                interval="Min60",
                limit=settings.candle_limit,
            )
        except Exception as exc:
            snapshot.data_error = (snapshot.data_error or "") + f"; 1h candles: {exc}"

        # Funding
        try:
            snapshot.funding = client.get_funding_rate(symbol)
        except Exception as exc:
            snapshot.data_error = (snapshot.data_error or "") + f"; funding: {exc}"

        # Open interest
        try:
            snapshot.open_interest = client.get_open_interest(symbol)
        except Exception as exc:
            snapshot.data_error = (snapshot.data_error or "") + f"; oi: {exc}"

        # Fetch deeper exchange flow only for the highest-liquidity shortlist.
        # It confirms an already-qualified setup but can never invent one.
        if len(snapshots) < settings.microstructure_probe_limit:
            try:
                snapshot.order_book = client.get_order_book(
                    symbol, limit=settings.order_book_depth
                )
            except Exception as exc:
                snapshot.data_error = (snapshot.data_error or "") + f"; depth: {exc}"
            try:
                snapshot.recent_trades = client.get_recent_trades(
                    symbol, limit=settings.recent_trade_limit
                )
            except Exception as exc:
                snapshot.data_error = (snapshot.data_error or "") + f"; trades: {exc}"
            snapshot.microstructure = analyze_microstructure(
                snapshot.contract,
                snapshot.order_book,
                snapshot.recent_trades,
            )

        snapshot.data_status = (
            DataStatus.FRESH if snapshot.data_error is None else DataStatus.STALE
        )
        snapshots.append(snapshot)

    _audit_event(
        audit_path, "snapshot_fetch", "configured",
        f"Fetched detailed data for {len(snapshots)} symbols",
    )

    # ── Step 6: Score and rank candidates ─────────────────────────────────────
    scored: List[Candidate] = []
    for snap in snapshots:
        try:
            candidate = score_snapshot(snap, settings)
            scored.append(candidate)
        except Exception as exc:
            scored.append(
                Candidate(
                    rank=0,
                    symbol=snap.symbol,
                    selection_status=CandidateStatus.REJECTED,
                    signal_status=SignalStatus.UNKNOWN,
                    data_status=DataStatus.STALE,
                    confidence=None,
                    note=f"Scoring error: {exc}",
                )
            )

    apply_pending_setup_expiry(
        os.path.join(settings.runtime_dir, "pending_setups.json"),
        scored,
        settings,
    )

    # Sort by confidence descending (None last)
    scored.sort(
        key=lambda c: (c.confidence is not None, c.confidence or 0),
        reverse=True,
    )
    # Independent public venues are deliberately fetched only after the
    # deterministic MEXC scan has formed its shortlist. They can affirm or
    # penalize a setup but never create a long/short direction by themselves.
    market_context = fetch_market_context(
        [candidate.symbol for candidate in scored],
        timeout=min(settings.request_timeout_seconds, 8),
        cross_market_probe_limit=settings.cross_market_probe_limit,
    )
    cross_market = market_context.get("cross_market") or {}
    for candidate in scored:
        apply_cross_market_confirmation(
            candidate,
            cross_market.get(candidate.symbol),
            settings,
        )
    scored.sort(
        key=lambda c: (c.confidence is not None, c.confidence or 0),
        reverse=True,
    )
    scored = _apply_portfolio_controls(
        scored,
        snapshots,
        settings,
        existing_positions=persisted_open_positions,
    )

    # Assign ranks, keep top N
    final_candidates: List[Candidate] = []
    for i, cand in enumerate(scored[: settings.candidate_count]):
        cand.rank = i + 1
        final_candidates.append(cand)
    state.scan_coverage["selected"] = len(final_candidates)
    selected_symbols = [candidate.symbol for candidate in final_candidates]
    guard_halted_before_cycle = is_daily_guard_halted(daily_guard_status)
    if guard_halted_before_cycle:
        for candidate in final_candidates:
            _clear_plan(candidate)
            candidate.correlation_status = "blocked"
            candidate.note += "; daily guard blocks new paper positions"

    _audit_event(
        audit_path,
        "candidates_selected",
        "configured",
        f"{len(final_candidates)} candidates selected",
        {
            "symbols": [c.symbol for c in final_candidates],
        },
    )

    top_gainers = _movers(filtered, tickers, reverse=True)
    top_losers = _movers(filtered, tickers, reverse=False)
    _apply_coin_metadata(final_candidates, market_context)
    # Always monitor existing local paper positions, even if the daily guard
    # already blocks new exposure. Existing stops and profit locks must stay live.
    paper_positions, paper_summary, paper_events = update_paper_positions(
        paper_path,
        [],
        tickers,
        settings,
        allow_new_positions=False,
    )
    current_daily_pnl = (
        daily_pnl_usdt
        if daily_pnl_usdt is not None
        else load_paper_daily_pnl(paper_path)
        if settings.paper_trading_enabled
        else None
    )
    current_daily_guard = evaluate_daily_guard(current_daily_pnl, settings)
    if settings.paper_trading_enabled and not is_daily_guard_halted(current_daily_guard):
        entry_tickers = _revalidate_paper_entries(
            client,
            final_candidates,
            paper_positions,
            settings,
            audit_path,
        )
        paper_positions, paper_summary, opening_events = update_paper_positions(
            paper_path,
            final_candidates,
            entry_tickers,
            settings,
            allow_new_positions=True,
        )
        paper_events.extend(opening_events)
    elif settings.paper_trading_enabled:
        for candidate in final_candidates:
            _clear_plan(candidate)
            candidate.correlation_status = "blocked"
            candidate.note += "; daily guard blocks new paper positions"
    _, memory_error = _memory_operation(
        settings,
        lambda memory: memory.record_candidates(
            cycle_count,
            scored,
            selected_symbols,
        ),
    )
    _set_memory_error(state, memory_error)
    for event in paper_events:
        _audit_event(
            audit_path,
            event["event"],
            event["status"],
            f"Paper position event for {event['symbol']}",
        )
    ai_summary = analyze_paper_events(settings, paper_path, paper_events)
    paper_summary["ai_explanations"] = ai_summary
    _, memory_error = _memory_operation(
        settings,
        lambda memory: memory.record_trade_events(paper_events),
    )
    _set_memory_error(state, memory_error)

    # ── Step 7: Build and save final state ────────────────────────────────────
    state.market_data_status = ReadinessStatus.LIVE
    state.last_sync = now_iso()
    state.candidates = final_candidates
    state.top_gainers = top_gainers
    state.top_losers = top_losers
    state.market_context = market_context
    state.open_positions = paper_positions
    state.paper_summary = paper_summary
    state.daily_pnl_usdt = current_daily_pnl
    state.daily_guard_status = current_daily_guard
    state.supervisor_status = ReadinessStatus.LIVE
    state.last_error = None
    snapshot, memory_error = _memory_operation(
        settings,
        lambda memory: (
            memory.record_heartbeat(
                cycle_count,
                "healthy",
                len(filtered),
                len(snapshots),
                len(memory.radar(settings.radar_limit)),
                len(paper_positions),
            ),
            memory.write_snapshot(
                settings.memory_snapshot_path,
                settings.radar_limit,
            ),
        )[1],
    )
    _set_memory_error(state, memory_error)
    if snapshot:
        state.radar = snapshot.get("radar") or []
        state.memory_last_update = snapshot.get("generated_at")
        state.scan_coverage["radar_count"] = len(state.radar)

    risk_blocked_symbols = [
        candidate.symbol
        for candidate in final_candidates
        if "basket risk cap blocked" in candidate.note
        or "correlation blocked" in candidate.note
        or "correlation unavailable" in candidate.note
    ]
    _audit_event(
        audit_path, "cycle_complete", "configured",
        f"Cycle {cycle_count} complete; {len(final_candidates)} candidates written",
    )

    return _finalize_cycle(
        settings,
        state_path,
        state,
        paper_events,
        risk_blocked_symbols,
    )


def _save_state(state_path: str, state: FrameworkState) -> None:
    """Save FrameworkState to disk; never raise (log instead)."""
    try:
        write_json_atomic(state_path, state.to_dict())
    except Exception as exc:
        # Last-resort stderr log; don't crash the cycle
        sys.stderr.write(f"[orchestrator] Failed to save state: {exc}\n")
