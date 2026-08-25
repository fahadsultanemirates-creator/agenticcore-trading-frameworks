"""
paper_positions.py – Local-only paper position lifecycle manager.

This module never calls a private exchange API. Positions are theoretical local
records used to verify stop, target, fee, and profit-lock behavior before any
private account is connected.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Tuple

from config.settings import CryptoSettings
from domain.models import Candidate, EntryStatus, Ticker
from storage.state import read_json_safe, write_json_atomic

FEE_RATE = 0.0004
# Dubai's UTC+4 offset is fixed; avoid an optional tzdata dependency on Windows.
DUBAI = timezone(timedelta(hours=4), name="Asia/Dubai")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _is_current_dubai_day(timestamp: Any, now: datetime | None = None) -> bool:
    """Treat malformed/missing closure dates as untrusted, not as today's P&L."""
    if not isinstance(timestamp, str):
        return False
    try:
        parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        reference = (now or datetime.now(timezone.utc)).astimezone(DUBAI)
        return parsed.astimezone(DUBAI).date() == reference.date()
    except ValueError:
        return False


def daily_realized_pnl(
    positions: List[Dict[str, Any]], now: datetime | None = None
) -> float:
    """Return only closed local paper P&L attributable to the current Dubai day."""
    return round(
        sum(
            float(position.get("net_pnl_usdt") or 0.0)
            for position in positions
            if position.get("status") == "closed"
            and _is_current_dubai_day(position.get("closed_at"), now)
        ),
        8,
    )


def load_paper_daily_pnl(state_path: str) -> float:
    """Read the persisted date-scoped local paper P&L before a new cycle starts."""
    return daily_realized_pnl(list(read_json_safe(state_path).get("positions") or []))


def load_open_paper_positions(state_path: str) -> List[Dict[str, Any]]:
    """Return only persisted local paper exposure for pre-entry correlation checks."""
    return [
        position
        for position in list(read_json_safe(state_path).get("positions") or [])
        if isinstance(position, dict) and position.get("status") == "open"
    ]


def _gross_pnl(position: Dict[str, Any], price: float) -> float:
    direction = 1 if position["side"] == "long" else -1
    return direction * (price - position["entry_price"]) * position["quantity"] * position["contract_size"]


def _net_pnl_at_price(position: Dict[str, Any], price: float) -> float:
    exit_fee = abs(price * position["quantity"] * position["contract_size"]) * FEE_RATE
    return _gross_pnl(position, price) - float(position.get("entry_fee_usdt") or 0.0) - exit_fee


def _close(position: Dict[str, Any], price: float, reason: str) -> None:
    gross = _gross_pnl(position, price)
    exit_fee = abs(price * position["quantity"] * position["contract_size"]) * FEE_RATE
    position.update(
        {
            "status": "closed",
            "exit_price": price,
            "closed_at": _now(),
            "close_reason": reason,
            "gross_pnl_usdt": round(gross, 8),
            "fees_usdt": round(position.get("entry_fee_usdt", 0.0) + exit_fee, 8),
            "net_pnl_usdt": round(gross - position.get("entry_fee_usdt", 0.0) - exit_fee, 8),
        }
    )


def _candidate_evidence(candidate: Candidate) -> Dict[str, Any]:
    """Capture the exact public facts available at the local paper entry."""
    return {
        "confidence": candidate.confidence,
        "note": candidate.note,
        "entry_status": candidate.entry_status,
        "signal_status": candidate.signal_status,
        "price_action_15m_pct": candidate.price_action_15m_pct,
        "price_action_1h_pct": candidate.price_action_1h_pct,
        "trend_1h": candidate.trend_1h,
        "relative_volume": candidate.relative_volume,
        "funding_rate": candidate.funding_rate,
        "oi_usdt": candidate.oi_usdt,
        "buy_pressure_pct": candidate.buy_pressure_pct,
        "order_book_imbalance_pct": candidate.order_book_imbalance_pct,
        "large_trade_count": candidate.large_trade_count,
        "market_cap_usd": candidate.market_cap_usd,
        "market_cap_rank": candidate.market_cap_rank,
        "support_price": candidate.support_price,
        "resistance_price": candidate.resistance_price,
        "entry_structure_id": candidate.entry_structure_id,
        "cross_market_status": candidate.cross_market_status,
        "cross_market_agreement": candidate.cross_market_agreement,
        "cross_market_adjustment": candidate.cross_market_adjustment,
        "cross_market_evidence": candidate.cross_market_evidence,
    }


def _condition_tags(candidate: Candidate) -> List[str]:
    """Store deterministic condition labels for later outcome aggregation."""
    tags = ["entry_retest_confirmed"]
    if candidate.relative_volume is not None and candidate.relative_volume >= 1.1:
        tags.append("relative_volume_confirmed")
    if candidate.buy_pressure_pct is not None:
        tags.append("mexc_buy_pressure" if candidate.buy_pressure_pct >= 55 else "mexc_sell_pressure" if candidate.buy_pressure_pct <= 45 else "mexc_flow_neutral")
    if candidate.cross_market_agreement in {"long", "short"}:
        tags.append(f"cross_market_{candidate.cross_market_agreement}")
    elif candidate.cross_market_agreement == "mixed":
        tags.append("cross_market_mixed")
    return tags


def _ticker_evidence(ticker: Ticker) -> Dict[str, Any]:
    return {
        "last_price": ticker.last_price,
        "bid": ticker.bid,
        "ask": ticker.ask,
        "spread_pct": ticker.spread_pct,
        "turnover_24h_usdt": ticker.turnover_24h_usdt,
        "change_pct_24h": ticker.change_pct_24h,
        "fetched_at": ticker.fetched_at,
    }


def update_paper_positions(
    state_path: str,
    candidates: List[Candidate],
    tickers: Dict[str, Ticker],
    settings: CryptoSettings,
    allow_new_positions: bool = False,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any], List[Dict[str, Any]]]:
    """
    Apply mark-price exits and profit locks, then optionally open new qualified
    local paper positions. Returns records, summary, and immutable audit events.
    """
    previous = read_json_safe(state_path)
    positions = list(previous.get("positions") or [])
    notification_outbox = list(previous.get("notification_outbox") or [])
    events: List[Dict[str, Any]] = []

    for position in positions:
        if position.get("status") != "open":
            continue
        ticker = tickers.get(str(position.get("symbol")))
        price = ticker.last_price if ticker else None
        if price is None or price <= 0:
            continue
        position["last_mark_price"] = price
        position["last_marked_at"] = _now()
        position["marked_net_pnl_usdt"] = round(_net_pnl_at_price(position, price), 8)
        if not position.get("profit_lock_applied"):
            trigger = position.get("profit_lock_trigger_price")
            crossed = price >= trigger if position.get("side") == "long" else price <= trigger
            if trigger is not None and crossed:
                position["stop_price"] = position["profit_lock_stop_price"]
                position["profit_lock_applied"] = True
                position["profit_lock_applied_at"] = _now()
                events.append(
                    {
                        "event": "paper_profit_lock",
                        "symbol": position["symbol"],
                        "status": "applied",
                        "position": dict(position),
                    }
                )
        if position.get("side") == "long":
            if price >= position["take_profit_price"]:
                _close(position, price, "target")
            elif price <= position["stop_price"]:
                _close(position, price, "profit_lock" if position.get("profit_lock_applied") else "stop")
        else:
            if price <= position["take_profit_price"]:
                _close(position, price, "target")
            elif price >= position["stop_price"]:
                _close(position, price, "profit_lock" if position.get("profit_lock_applied") else "stop")
        if position.get("status") == "closed":
            if ticker is not None:
                position["close_evidence"] = _ticker_evidence(ticker)
            events.append(
                {
                    "event": "paper_position_closed",
                    "symbol": position["symbol"],
                    "status": "closed",
                    "position": dict(position),
                }
            )

    # Basket control is evaluated after individual stops/targets and before
    # opening anything new. If every open position has a current mark and the
    # aggregate marked net profit reaches $5, close the entire remaining basket.
    basket_marked_net_pnl: float | None = None
    basket_close_triggered = False
    basket_marks: List[Tuple[Dict[str, Any], Ticker, float]] = []
    basket_marking_complete = True
    for position in positions:
        if position.get("status") != "open":
            continue
        ticker = tickers.get(str(position.get("symbol")))
        price = ticker.last_price if ticker else None
        if price is None or price <= 0:
            basket_marking_complete = False
            break
        basket_marks.append((position, ticker, price))
    if basket_marking_complete and basket_marks:
        basket_marked_net_pnl = sum(_net_pnl_at_price(position, price) for position, _, price in basket_marks)
        if basket_marked_net_pnl >= settings.basket_profit_target_usdt:
            basket_close_triggered = True
            for position, ticker, price in basket_marks:
                _close(position, price, "basket_profit_target")
                position["close_evidence"] = _ticker_evidence(ticker)
                events.append(
                    {
                        "event": "paper_position_closed",
                        "symbol": position["symbol"],
                        "status": "closed",
                        "position": dict(position),
                    }
                )

    open_positions = [position for position in positions if position.get("status") == "open"]
    planned_margin = sum(
        float(position.get("initial_margin_usdt") or settings.max_isolated_margin_per_position_usdt)
        for position in open_positions
    )
    if settings.paper_trading_enabled and allow_new_positions:
        existing_symbols = {str(position.get("symbol")) for position in open_positions}
        for candidate in candidates:
            if (
                candidate.symbol in existing_symbols
                or candidate.planned_side not in ("long", "short")
                or candidate.entry_status != EntryStatus.CONFIRMED
                or candidate.planned_quantity is None
                or candidate.planned_stop_price is None
                or candidate.planned_take_profit_price is None
                or candidate.profit_lock_trigger_price is None
                or candidate.profit_lock_stop_price is None
                or candidate.last_price is None
                or candidate.contract_size is None
                or len(open_positions) >= settings.max_open_positions
                or candidate.planned_margin_usdt is None
                or (
                    planned_margin + candidate.planned_margin_usdt
                    > settings.max_total_isolated_margin_usdt
                )
                or candidate.correlation_status == "blocked"
            ):
                continue
            entry_fee = abs(candidate.last_price * candidate.planned_quantity * candidate.contract_size) * FEE_RATE
            position = {
                "id": uuid.uuid4().hex,
                "symbol": candidate.symbol,
                "side": candidate.planned_side,
                "status": "open",
                "opened_at": _now(),
                "entry_price": candidate.last_price,
                "quantity": candidate.planned_quantity,
                "contract_size": candidate.contract_size,
                "stop_price": candidate.planned_stop_price,
                "original_stop_price": candidate.planned_stop_price,
                "take_profit_price": candidate.planned_take_profit_price,
                "profit_lock_trigger_price": candidate.profit_lock_trigger_price,
                "profit_lock_stop_price": candidate.profit_lock_stop_price,
                "profit_lock_applied": False,
                "entry_fee_usdt": round(entry_fee, 8),
                "initial_margin_usdt": candidate.planned_margin_usdt,
                "confidence": candidate.confidence,
                "entry_evidence": _candidate_evidence(candidate),
                "condition_tags": _condition_tags(candidate),
            }
            positions.append(position)
            open_positions.append(position)
            existing_symbols.add(candidate.symbol)
            planned_margin += candidate.planned_margin_usdt
            events.append(
                {
                    "event": "paper_position_opened",
                    "symbol": candidate.symbol,
                    "status": "opened",
                    "position": dict(position),
                }
            )

    closed_positions = [position for position in positions if position.get("status") == "closed"]
    summary = {
        "enabled": settings.paper_trading_enabled,
        "open_count": len(open_positions),
        "closed_count": len(closed_positions),
        "planned_margin_usdt": round(planned_margin, 2),
        "realized_pnl_usdt": round(sum(float(position.get("net_pnl_usdt") or 0.0) for position in closed_positions), 8),
        "realized_pnl_today_usdt": daily_realized_pnl(positions),
        "basket_profit_target_usdt": settings.basket_profit_target_usdt,
        "basket_marked_net_pnl_usdt": (
            round(basket_marked_net_pnl, 8) if basket_marked_net_pnl is not None else None
        ),
        "basket_close_triggered": basket_close_triggered,
    }
    queued_ids = {
        str(event.get("id"))
        for event in notification_outbox
        if isinstance(event, dict) and event.get("id")
    }
    for event in events:
        position = event.get("position")
        if not isinstance(position, dict) or not position.get("id"):
            continue
        event["id"] = f"{event.get('event')}:{position['id']}"
        if event["id"] not in queued_ids:
            notification_outbox.append(dict(event))
            queued_ids.add(event["id"])
    write_json_atomic(
        state_path,
        {
            "positions": positions,
            "summary": summary,
            "notification_outbox": notification_outbox,
        },
    )
    return positions, summary, events