"""
signals.py – Transparent, deterministic signal and confidence scoring.

Signal scoring is based entirely on completed candle data, funding bias,
volume/OI ratio, and spread. Confidence is bounded 0–100.
Unknown or missing data ALWAYS reduces confidence or rejects the candidate.

No private data, no live execution triggers, no external dependencies.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import List, Optional

from config.settings import CryptoSettings
from analysis.quality import (
    completed,
    evaluate_entry_setup,
    failed_breakout,
    is_clear_of_zone,
    pullback_retest,
    relative_volume,
    structure_zones,
)
from domain.models import (
    Candle,
    Candidate,
    CandidateStatus,
    DataStatus,
    EntryStatus,
    FundingInfo,
    MarketSnapshot,
    OpenInterest,
    ReadinessStatus,
    SignalStatus,
    Ticker,
)
from risk.profit_lock import calculate_profit_lock_levels
from risk.sizing import SizingError, calculate_quantity_for_notional
from risk.trade_levels import (
    calculate_estimated_isolated_margin,
    calculate_target_price,
    derive_volatility_stop_price,
    quantize_price,
)


# ── Staleness check ────────────────────────────────────────────────────────────

def _is_stale(fetched_at_iso: Optional[str], threshold_seconds: int) -> bool:
    """Return True if fetched_at is None or older than threshold_seconds."""
    if fetched_at_iso is None:
        return True
    try:
        dt = datetime.fromisoformat(fetched_at_iso.replace("Z", "+00:00"))
        age = time.time() - dt.timestamp()
        return age > threshold_seconds
    except (ValueError, TypeError):
        return True


# ── Candle trend ───────────────────────────────────────────────────────────────

def _candle_trend(candles: List[Candle]) -> Optional[str]:
    """
    Determine trend from completed candles only.
    Returns "long", "short", or None (insufficient data).
    Uses simple close-above-sma logic: last close vs. SMA of prior closes.
    """
    completed = [c for c in candles if c.is_complete]
    if len(completed) < 5:
        return None

    closes = [c.close for c in completed if c.close > 0]
    if len(closes) < 5:
        return None

    last_close = closes[-1]
    sma = sum(closes[-5:-1]) / 4  # 4-bar SMA from the bars before the last
    if sma <= 0:
        return None

    if last_close > sma * 1.001:
        return "long"
    if last_close < sma * 0.999:
        return "short"
    return None


# ── Funding bias ───────────────────────────────────────────────────────────────

def _funding_bias(funding: Optional[FundingInfo]) -> Optional[str]:
    """
    Positive funding → longs pay shorts → short bias.
    Negative funding → shorts pay longs → long bias.
    Returns "long", "short", or None (unknown).
    """
    if funding is None or funding.current_rate is None:
        return None
    rate = funding.current_rate
    if rate > 0.0003:
        return "short"
    if rate < -0.0003:
        return "long"
    return None  # neutral / too small to matter


# ── Volume/OI check ────────────────────────────────────────────────────────────

def _vol_oi_ratio(ticker: Optional[Ticker], oi: Optional[OpenInterest]) -> Optional[float]:
    """Return 24h volume / OI ratio if both available, else None."""
    if ticker is None or ticker.turnover_24h_usdt is None:
        return None
    if oi is None or oi.value_usdt is None or oi.value_usdt <= 0:
        return None
    return ticker.turnover_24h_usdt / oi.value_usdt


def _price_action_pct(candles: List[Candle]) -> Optional[float]:
    """Completed-candle movement over the available window, never a forecast."""
    finished = completed(candles)
    if len(finished) < 2 or finished[0].close <= 0:
        return None
    return ((finished[-1].close - finished[0].close) / finished[0].close) * 100


# ── Main scoring ───────────────────────────────────────────────────────────────

def score_snapshot(
    snapshot: MarketSnapshot,
    settings: CryptoSettings,
) -> Candidate:
    """
    Score a MarketSnapshot and return a Candidate.

    Confidence is evidence-weighted, not a neutral 50 plus small adjustments.
    Missing confirmations earn no points and also prevent paper sizing. The score
    is a transparent quality gate — not a probability of future profit.
    """
    settings.validate()
    symbol = snapshot.symbol
    ticker = snapshot.ticker
    candles = snapshot.candles
    funding = snapshot.funding
    oi = snapshot.open_interest
    flow = snapshot.microstructure

    # ── Validate minimum data requirements ────────────────────────────────────
    if ticker is None:
        return Candidate(
            rank=0,
            symbol=symbol,
            selection_status=CandidateStatus.REJECTED,
            signal_status=SignalStatus.UNKNOWN,
            data_status=DataStatus.NOT_CONNECTED,
            confidence=None,
            note="No ticker data",
        )

    if _is_stale(ticker.fetched_at, settings.stale_data_threshold_seconds):
        return Candidate(
            rank=0,
            symbol=symbol,
            selection_status=CandidateStatus.REJECTED,
            signal_status=SignalStatus.UNKNOWN,
            data_status=DataStatus.STALE,
            confidence=None,
            note="Ticker data is stale",
        )

    # ── Confidence scoring ────────────────────────────────────────────────────
    confidence = 10  # fresh ticker baseline only
    notes: List[str] = []

    # 15-minute direction and 4-hour confirmation
    trend = _candle_trend(candles)
    completed_count = len([c for c in candles if c.is_complete])
    if completed_count < 7:
        notes.append("insufficient candles")
        trend = None
    elif trend is not None:
        confidence += 20
        notes.append(f"15m trend={trend}")
    else:
        notes.append("no 15m trend")

    context_candles = snapshot.context_candles
    higher_timeframe_valid = len(completed(context_candles)) >= 8
    context_trend = _candle_trend(context_candles)
    if trend is not None and context_trend == trend:
        confidence += 15
        notes.append("higher-timeframe aligned")
    elif context_trend is None:
        notes.append("no higher-timeframe confirmation")
    else:
        notes.append("higher-timeframe diverges")

    mid_candles = snapshot.mid_candles
    mid_trend = _candle_trend(mid_candles)
    if trend is not None and mid_trend == trend:
        confidence += 5
        notes.append("1h aligned")
    elif mid_trend is None:
        notes.append("no 1h confirmation")
    else:
        notes.append("1h diverges")

    # Funding bias
    f_bias = _funding_bias(funding)

    # Vol/OI
    vol_oi = _vol_oi_ratio(ticker, oi)
    if vol_oi is not None and vol_oi > 1.5:
        confidence += 10
        notes.append("high vol/OI")
    elif vol_oi is None:
        notes.append("no OI data")

    # Trend + funding agreement
    signal_status = SignalStatus.UNKNOWN
    if trend is not None and f_bias is not None:
        if trend == f_bias:
            confidence += 10
            signal_status = SignalStatus.LONG if trend == "long" else SignalStatus.SHORT
            notes.append(f"trend+funding={trend}")
        else:
            notes.append(f"trend/funding diverge")
    elif trend is not None:
        signal_status = SignalStatus.LONG if trend == "long" else SignalStatus.SHORT
        notes.append(f"trend={trend} (no funding confirmation)")

    # Completed-candle relative volume
    rel_volume = relative_volume(candles)
    if rel_volume is not None and rel_volume >= settings.relative_volume_min:
        confidence += 15
        notes.append(f"relative volume={rel_volume:.2f}x")
    elif rel_volume is None:
        notes.append("no relative-volume confirmation")
    else:
        notes.append(f"relative volume weak={rel_volume:.2f}x")

    # Public order-flow proxies add confirmation only. They cannot form a
    # signal by themselves and never identify a wallet or named participant.
    if flow is None:
        notes.append("no public order-flow confirmation")
    else:
        if flow.buy_pressure_pct is not None:
            if trend == "long" and flow.buy_pressure_pct >= 55:
                confidence += 5
                notes.append(f"recent buy pressure={flow.buy_pressure_pct:.0f}%")
            elif trend == "short" and flow.buy_pressure_pct <= 45:
                confidence += 5
                notes.append(f"recent sell pressure={100 - flow.buy_pressure_pct:.0f}%")
            else:
                notes.append(f"recent flow not aligned={flow.buy_pressure_pct:.0f}% buy")
        if flow.order_book_imbalance_pct is not None:
            if trend == "long" and flow.order_book_imbalance_pct >= 15:
                confidence += 5
                notes.append("bid-depth support")
            elif trend == "short" and flow.order_book_imbalance_pct <= -15:
                confidence += 5
                notes.append("ask-depth pressure")
            else:
                notes.append("depth imbalance not aligned")
        if flow.large_trade_count:
            notes.append(f"{flow.large_trade_count} large-trade flow proxy")

    # Build multi-touch zones from both the 4h context and 15m entry structure.
    # The local zone is used for a precise entry; the 4h zone remains the source
    # of wider context when local swings are not established.
    context_support, context_resistance = structure_zones(
        context_candles,
        ticker.last_price,
        lookback=settings.context_candle_limit,
        zone_width_pct=settings.structure_zone_width_pct,
        minimum_touches=settings.structure_zone_min_touches,
    )
    local_support, local_resistance = structure_zones(
        candles,
        ticker.last_price,
        lookback=settings.candle_limit,
        zone_width_pct=settings.structure_zone_width_pct,
        minimum_touches=settings.structure_zone_min_touches,
    )
    support_zone = local_support or context_support
    resistance_zone = local_resistance or context_resistance
    level_clear = is_clear_of_zone(
        trend,
        ticker.last_price,
        support_zone,
        resistance_zone,
        settings.minimum_level_distance_pct,
    )
    opposing_zone_distance_pct: Optional[float] = None
    if trend == "long" and resistance_zone is not None and ticker.last_price:
        opposing_zone_distance_pct = (resistance_zone.low - ticker.last_price) / ticker.last_price
    elif trend == "short" and support_zone is not None and ticker.last_price:
        opposing_zone_distance_pct = (ticker.last_price - support_zone.high) / ticker.last_price
    if level_clear is True:
        confidence += 10
        notes.append("room to next structure zone")
    elif level_clear is False:
        notes.append("too close to opposing structure zone")
    else:
        notes.append("structure zones unavailable")

    retest_confirmed = pullback_retest(candles, trend)
    if retest_confirmed is True:
        confidence += 10
        notes.append("pullback/retest confirmed")
    elif retest_confirmed is False:
        notes.append("no pullback/retest")
    else:
        notes.append("pullback/retest unknown")

    entry_zone = support_zone if trend == "long" else resistance_zone if trend == "short" else None
    fake_reversal = failed_breakout(candles, trend, support_zone, resistance_zone)
    entry_setup = evaluate_entry_setup(
        candles,
        trend,
        entry_zone,
        ticker.last_price,
        settings.entry_zone_proximity_pct,
        settings.entry_setup_expiry_candles,
    )
    entry_status = entry_setup.status
    if fake_reversal is True:
        entry_status = EntryStatus.REJECTED
        notes.append("failed breakout/reversal detected")
    elif entry_status == EntryStatus.CONFIRMED:
        confidence += 10
        notes.append("entry-zone retest/reclaim confirmed")
    elif entry_status == EntryStatus.PENDING:
        notes.append(f"pending setup: {entry_setup.reason}")
    elif entry_status == EntryStatus.EXPIRED:
        notes.append("entry setup expired")
    else:
        notes.append("entry setup unavailable")

    if ticker.spread_pct is not None and ticker.spread_pct <= settings.max_spread_pct * 0.5:
        confidence += 10
        notes.append("clean spread")
    else:
        notes.append("elevated or unknown spread")

    # Clamp
    confidence = max(0, min(100, confidence))

    # Determine data status
    data_status = DataStatus.FRESH if ticker.fetched_at else DataStatus.STALE

    planned_side = trend if signal_status in (SignalStatus.LONG, SignalStatus.SHORT) else None
    planned_quantity: Optional[float] = None
    planned_margin_usdt: Optional[float] = None
    planned_stop_price: Optional[float] = None
    planned_take_profit_price: Optional[float] = None
    profit_lock_trigger_price: Optional[float] = None
    profit_lock_stop_price: Optional[float] = None
    planned_target_profit_usdt: Optional[float] = None

    standard_quality_gate_passed = (
        planned_side is not None
        and higher_timeframe_valid
        and rel_volume is not None
        and rel_volume >= settings.relative_volume_min
        and level_clear is True
        and retest_confirmed is True
        and entry_status == EntryStatus.CONFIRMED
        and fake_reversal is False
    )
    # A trial canary proves the live path with a broader, directional setup.
    # It never accepts missing data, a failed breakout, a weak volume signal, or
    # an absent higher-timeframe context; it only removes the completed-retest
    # requirement that makes a fresh live validation wait indefinitely.
    trial_quality_gate_passed = (
        planned_side is not None
        and higher_timeframe_valid
        and rel_volume is not None
        and rel_volume >= 1.0
        and level_clear is True
        and fake_reversal is False
    )
    quality_gate_passed = (
        trial_quality_gate_passed
        if settings.trial_entry_mode
        else standard_quality_gate_passed
    )
    confidence_floor = (
        settings.trial_minimum_signal_confidence
        if settings.trial_entry_mode
        else settings.minimum_signal_confidence
    )
    if (
        quality_gate_passed
        and confidence >= confidence_floor
        and snapshot.contract is not None
    ):
        if settings.trial_entry_mode and entry_status != EntryStatus.CONFIRMED:
            notes.append("trial profile: directional setup accepted before completed retest")
        try:
            raw_stop_price = derive_volatility_stop_price(
                side=planned_side,
                entry_price=ticker.last_price or 0,
                candles=candles,
                atr_period=settings.stop_atr_period,
                atr_multiplier=settings.stop_atr_multiplier,
                minimum_stop_pct=settings.minimum_stop_pct,
                maximum_stop_pct=settings.maximum_stop_pct,
            )
            planned_stop_price = quantize_price(
                raw_stop_price,
                snapshot.contract,
                "up" if planned_side == "long" else "down",
            )
            planned_quantity = calculate_quantity_for_notional(
                entry_price=ticker.last_price or 0,
                notional_usdt=settings.position_notional_usdt,
                contract=snapshot.contract,
                leverage=settings.leverage_max,
                margin_mode=settings.margin_mode,
            )
            raw_take_profit_price = calculate_target_price(
                side=planned_side,
                entry_price=ticker.last_price or 0,
                quantity=planned_quantity,
                contract=snapshot.contract,
                target_profit_usdt=settings.take_profit_usdt,
            )
            planned_take_profit_price = quantize_price(
                raw_take_profit_price,
                snapshot.contract,
                "up" if planned_side == "long" else "down",
            )
            planned_margin_usdt = calculate_estimated_isolated_margin(
                entry_price=ticker.last_price or 0,
                quantity=planned_quantity,
                contract=snapshot.contract,
                leverage=settings.leverage_max,
            )
            if planned_margin_usdt > settings.max_isolated_margin_per_position_usdt:
                raise SizingError(
                    f"Fixed notional would require ${planned_margin_usdt:.8f} isolated "
                    f"margin for {snapshot.contract.symbol}, above the "
                    f"${settings.max_isolated_margin_per_position_usdt:.2f} cap."
                )
            profit_lock = calculate_profit_lock_levels(
                side=planned_side,
                entry_price=ticker.last_price or 0,
                take_profit_price=planned_take_profit_price,
                activation_pct=settings.profit_lock_activation_pct,
                protection_pct=settings.profit_lock_protection_pct,
            )
            profit_lock_trigger_price = quantize_price(
                profit_lock.activation_price,
                snapshot.contract,
                "up" if planned_side == "long" else "down",
            )
            profit_lock_stop_price = quantize_price(
                profit_lock.protected_stop_price,
                snapshot.contract,
                "up" if planned_side == "long" else "down",
            )
            planned_target_profit_usdt = (
                abs(planned_take_profit_price - (ticker.last_price or 0))
                * planned_quantity
                * snapshot.contract.contract_size
            )
            notes.append(
                f"ATR stop; target≥${planned_target_profit_usdt:.2f}; "
                f"margin≈${planned_margin_usdt:.2f}; "
                f"lock {settings.profit_lock_activation_pct}%→"
                f"{settings.profit_lock_protection_pct}%"
            )
        except SizingError as exc:
            planned_stop_price = None
            planned_quantity = None
            notes.append(f"paper size rejected: {exc}")
    elif planned_side is not None:
        notes.append("paper sizing blocked: entry quality gate or confidence")

    return Candidate(
        rank=0,  # rank assigned by orchestrator
        symbol=symbol,
        selection_status=CandidateStatus.SELECTED,
        signal_status=signal_status,
        data_status=data_status,
        risk_tier=(
            "trial"
            if settings.trial_entry_mode and quality_gate_passed and confidence >= confidence_floor
            else "tier-1"
            if quality_gate_passed and confidence >= confidence_floor
            else "low"
        ),
        confidence=confidence,
        planned_quantity=planned_quantity,
        planned_side=planned_side,
        note="; ".join(notes) if notes else "ok",
        last_price=ticker.last_price,
        spread_pct=ticker.spread_pct,
        turnover_24h_usdt=ticker.turnover_24h_usdt,
        funding_rate=funding.current_rate if funding else None,
        oi_usdt=oi.value_usdt if oi else None,
        planned_margin_usdt=planned_margin_usdt,
        planned_stop_price=planned_stop_price,
        planned_take_profit_price=planned_take_profit_price,
        profit_lock_trigger_price=profit_lock_trigger_price,
        profit_lock_stop_price=profit_lock_stop_price,
        planned_target_profit_usdt=planned_target_profit_usdt,
        contract_size=snapshot.contract.contract_size if snapshot.contract else None,
        relative_volume=rel_volume,
        support_price=support_zone.center if support_zone else None,
        resistance_price=resistance_zone.center if resistance_zone else None,
        pullback_confirmed=retest_confirmed,
        support_zone_low=support_zone.low if support_zone else None,
        support_zone_high=support_zone.high if support_zone else None,
        support_zone_touches=support_zone.touches if support_zone else None,
        resistance_zone_low=resistance_zone.low if resistance_zone else None,
        resistance_zone_high=resistance_zone.high if resistance_zone else None,
        resistance_zone_touches=resistance_zone.touches if resistance_zone else None,
        entry_status=entry_status,
        entry_zone_low=entry_setup.zone.low if entry_setup.zone else None,
        entry_zone_high=entry_setup.zone.high if entry_setup.zone else None,
        entry_invalidation_price=entry_setup.invalidation_price,
        entry_expires_at=entry_setup.expires_at,
        fake_reversal_detected=fake_reversal,
        opposing_zone_distance_pct=opposing_zone_distance_pct,
        entry_structure_id=entry_setup.zone.structure_id if entry_setup.zone else None,
        price_action_15m_pct=_price_action_pct(candles),
        price_action_1h_pct=_price_action_pct(mid_candles),
        trend_1h=mid_trend,
        buy_pressure_pct=flow.buy_pressure_pct if flow else None,
        order_book_imbalance_pct=flow.order_book_imbalance_pct if flow else None,
        large_trade_count=flow.large_trade_count if flow else None,
        largest_trade_notional_usdt=flow.largest_trade_notional_usdt if flow else None,
    )
