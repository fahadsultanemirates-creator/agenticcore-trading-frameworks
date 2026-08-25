"""
quality.py – Deterministic market-structure checks for crypto candidates.

All calculations use completed candles only. They produce observable signals
and never fabricate missing market structure.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from math import sqrt
from typing import Iterable, List, Optional, Tuple

from domain.models import Candle


@dataclass(frozen=True)
class PriceZone:
    """A completed-candle structure zone with multiple nearby swing touches."""

    low: float
    high: float
    touches: int
    timeframe: str
    structure_id: str

    @property
    def center(self) -> float:
        return (self.low + self.high) / 2


@dataclass(frozen=True)
class EntrySetup:
    """A local-paper entry setup that is pending, confirmed, expired, or rejected."""

    status: str
    zone: Optional[PriceZone]
    invalidation_price: Optional[float]
    expires_at: Optional[str]
    reason: str


def completed(candles: Iterable[Candle]) -> List[Candle]:
    """Return valid, completed candles sorted by time."""
    return sorted(
        [
            candle
            for candle in candles
            if candle.is_complete
            and candle.open_time > 0
            and candle.close > 0
            and candle.high > 0
            and candle.low > 0
            and candle.high >= candle.low
        ],
        key=lambda candle: candle.open_time,
    )


def relative_volume(candles: Iterable[Candle], lookback: int = 12) -> Optional[float]:
    """Return the final completed candle's volume divided by prior average."""
    bars = completed(candles)
    if len(bars) < lookback + 1:
        return None
    current = bars[-1].volume
    prior = [bar.volume for bar in bars[-(lookback + 1):-1] if bar.volume > 0]
    if current <= 0 or not prior:
        return None
    average = sum(prior) / len(prior)
    return current / average if average > 0 else None


def _swing_levels(bars: List[Candle], side: str) -> List[Tuple[float, int]]:
    """Return local swing highs/lows from completed bars, never a forming candle."""
    values: List[Tuple[float, int]] = []
    for index in range(1, len(bars) - 1):
        previous, current, following = bars[index - 1], bars[index], bars[index + 1]
        if side == "support" and current.low <= previous.low and current.low <= following.low:
            values.append((current.low, current.open_time))
        if side == "resistance" and current.high >= previous.high and current.high >= following.high:
            values.append((current.high, current.open_time))
    return values


def _cluster_levels(
    levels: List[Tuple[float, int]],
    width: float,
    minimum_touches: int,
    timeframe: str,
    zone_kind: str,
) -> List[PriceZone]:
    """Cluster nearby swing levels; one isolated wick is not a structure zone."""
    if width <= 0 or not levels:
        return []
    clusters: List[List[Tuple[float, int]]] = []
    for level in sorted(levels, key=lambda item: item[0]):
        if not clusters or level[0] - clusters[-1][-1][0] > width:
            clusters.append([level])
        else:
            clusters[-1].append(level)
    zones: List[PriceZone] = []
    for cluster in clusters:
        if len(cluster) < minimum_touches:
            continue
        center = sum(level[0] for level in cluster) / len(cluster)
        zones.append(
            PriceZone(
                low=min(level[0] for level in cluster) - width / 2,
                high=max(level[0] for level in cluster) + width / 2,
                touches=len(cluster),
                timeframe=timeframe,
                structure_id=(
                    f"{timeframe}:{zone_kind}:"
                    + "|".join(str(level[1]) for level in sorted(cluster, key=lambda item: item[1]))
                ),
            )
        )
    return zones


def structure_zones(
    candles: Iterable[Candle],
    price: Optional[float],
    lookback: int = 24,
    zone_width_pct: float = 0.004,
    minimum_touches: int = 2,
) -> Tuple[Optional[PriceZone], Optional[PriceZone]]:
    """
    Find the closest completed-candle support and resistance zones around price.

    Width adapts to both the configured percent width and the observed candle
    range. A zone requires multiple swing touches so a single extreme wick is
    never promoted into a tradable level.
    """
    bars = completed(candles)
    if price is None or price <= 0 or len(bars) < 7:
        return None, None
    window = bars[-(lookback + 1):-1]
    if len(window) < 5:
        return None, None
    average_range = sum(bar.high - bar.low for bar in window) / len(window)
    width = max(price * zone_width_pct, average_range * 0.75)
    timeframe = window[-1].interval
    support_zones = _cluster_levels(
        _swing_levels(window, "support"), width, minimum_touches, timeframe, "support"
    )
    resistance_zones = _cluster_levels(
        _swing_levels(window, "resistance"), width, minimum_touches, timeframe, "resistance"
    )
    support = max(
        (zone for zone in support_zones if zone.center <= price + width),
        key=lambda zone: zone.center,
        default=None,
    )
    resistance = min(
        (zone for zone in resistance_zones if zone.center >= price - width),
        key=lambda zone: zone.center,
        default=None,
    )
    return support, resistance


def support_resistance(
    candles: Iterable[Candle],
    lookback: int,
) -> Tuple[Optional[float], Optional[float]]:
    """Return prior support/resistance, deliberately excluding the latest bar."""
    bars = completed(candles)
    if len(bars) < 4:
        return None, None
    window = bars[-(lookback + 1):-1]
    if len(window) < 3:
        return None, None
    return min(bar.low for bar in window), max(bar.high for bar in window)


def is_clear_of_zone(
    side: Optional[str],
    price: Optional[float],
    support: Optional[PriceZone],
    resistance: Optional[PriceZone],
    minimum_distance_pct: float,
) -> Optional[bool]:
    """Require room before the near edge of the opposing completed structure zone."""
    if side not in ("long", "short") or price is None or price <= 0:
        return None
    if side == "long":
        if resistance is None:
            return None
        return (resistance.low - price) / price >= minimum_distance_pct
    if support is None:
        return None
    return (price - support.high) / price >= minimum_distance_pct


def failed_breakout(
    candles: Iterable[Candle],
    side: Optional[str],
    support: Optional[PriceZone],
    resistance: Optional[PriceZone],
) -> Optional[bool]:
    """
    Detect a wick through the opposing zone that closes back inside it.

    A long is blocked after a failed push through resistance; a short is blocked
    after a failed breakdown through support. This is deliberately based only on
    completed bars, so an in-progress wick cannot manufacture a rejection.
    """
    bars = completed(candles)
    if side not in ("long", "short") or len(bars) < 2:
        return None
    for bar in bars[-2:]:
        if side == "long" and resistance is not None:
            if bar.high > resistance.high and resistance.low <= bar.close <= resistance.high:
                return True
        if side == "short" and support is not None:
            if bar.low < support.low and support.low <= bar.close <= support.high:
                return True
    return False


def evaluate_entry_setup(
    candles: Iterable[Candle],
    side: Optional[str],
    zone: Optional[PriceZone],
    current_price: Optional[float],
    proximity_pct: float,
    expiry_candles: int,
    now: Optional[datetime] = None,
) -> EntrySetup:
    """
    Require a recent zone touch and directional reclaim before sizing an entry.

    A setup remains visible as pending while price has not returned to the zone.
    It expires after a bounded number of 15-minute candles, and cannot become
    confirmed if price has already run too far away from the zone.
    """
    if side not in ("long", "short") or zone is None or current_price is None or current_price <= 0:
        return EntrySetup("unknown", zone, None, None, "structure zone unavailable")
    bars = completed(candles)
    if len(bars) < 3:
        return EntrySetup("unknown", zone, None, None, "insufficient completed candles")
    recent = bars[-max(3, expiry_candles + 1):]
    touches = [
        bar for bar in recent
        if bar.low <= zone.high and bar.high >= zone.low
    ]
    anchor = touches[-1] if touches else bars[-1]
    expires_at = datetime.fromtimestamp(
        anchor.open_time / 1000, tz=timezone.utc
    ) + timedelta(minutes=15 * expiry_candles)
    invalidation = zone.low * (1 - proximity_pct) if side == "long" else zone.high * (1 + proximity_pct)
    if (now or datetime.now(timezone.utc)) >= expires_at:
        return EntrySetup("expired", zone, invalidation, expires_at.isoformat(), "entry setup expired")
    latest = bars[-1]
    if not touches:
        return EntrySetup("pending_entry", zone, invalidation, expires_at.isoformat(), "waiting for entry zone")
    if side == "long":
        reclaimed = latest.close > zone.high and latest.close > latest.open
        close_enough = current_price <= zone.high * (1 + proximity_pct)
    else:
        reclaimed = latest.close < zone.low and latest.close < latest.open
        close_enough = current_price >= zone.low * (1 - proximity_pct)
    if reclaimed and close_enough:
        return EntrySetup("confirmed", zone, invalidation, expires_at.isoformat(), "zone retest and reclaim confirmed")
    if reclaimed:
        return EntrySetup("pending_entry", zone, invalidation, expires_at.isoformat(), "reclaim confirmed; waiting for price near zone")
    return EntrySetup("pending_entry", zone, invalidation, expires_at.isoformat(), "waiting for directional zone reclaim")


def is_clear_of_level(
    side: Optional[str],
    price: Optional[float],
    support: Optional[float],
    resistance: Optional[float],
    minimum_distance_pct: float,
) -> Optional[bool]:
    """
    For longs, require room below resistance. For shorts, require room above
    support. Unknown inputs remain unknown rather than being treated as safe.
    """
    if side not in ("long", "short") or price is None or price <= 0:
        return None
    if side == "long":
        if resistance is None or resistance <= 0:
            return None
        return (resistance - price) / price >= minimum_distance_pct
    if support is None or support <= 0:
        return None
    return (price - support) / price >= minimum_distance_pct


def pullback_retest(candles: Iterable[Candle], side: Optional[str]) -> Optional[bool]:
    """
    Identify a conservative pullback/retest after a directional move.

    A long must revisit the short moving average and close back above it. A
    short must revisit it and close back below. The function is intentionally
    strict: weak continuation moves return False, not a tradable confirmation.
    """
    if side not in ("long", "short"):
        return None
    bars = completed(candles)
    if len(bars) < 7:
        return None
    prior = bars[-6:-1]
    last = bars[-1]
    sma = sum(bar.close for bar in prior) / len(prior)
    if sma <= 0:
        return None
    touched = any(bar.low <= sma <= bar.high for bar in bars[-3:])
    if not touched:
        return False
    if side == "long":
        return last.close > sma and last.close > last.open
    return last.close < sma and last.close < last.open


def return_correlation(
    left: Iterable[Candle],
    right: Iterable[Candle],
    lookback: int = 16,
) -> Optional[float]:
    """Pearson correlation over completed percentage-return series."""
    def timestamped_returns(bars: Iterable[Candle]) -> dict[int, float]:
        series = completed(bars)[-(lookback + 1):]
        return {
            series[index].open_time: (series[index].close - series[index - 1].close) / series[index - 1].close
            for index in range(1, len(series))
            if series[index - 1].close > 0
        }

    left_returns = timestamped_returns(left)
    right_returns = timestamped_returns(right)
    aligned_times = sorted(set(left_returns).intersection(right_returns))[-lookback:]
    size = len(aligned_times)
    if size < 5:
        return None
    x = [left_returns[timestamp] for timestamp in aligned_times]
    y = [right_returns[timestamp] for timestamp in aligned_times]
    mean_x = sum(x) / size
    mean_y = sum(y) / size
    numerator = sum((a - mean_x) * (b - mean_y) for a, b in zip(x, y))
    denominator_x = sqrt(sum((a - mean_x) ** 2 for a in x))
    denominator_y = sqrt(sum((b - mean_y) ** 2 for b in y))
    if denominator_x == 0 or denominator_y == 0:
        return None
    return numerator / (denominator_x * denominator_y)