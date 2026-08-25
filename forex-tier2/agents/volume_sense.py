"""Completed-candle tick-volume evidence for Tier 2 entries."""
from __future__ import annotations

from typing import Any

import pandas as pd


def completed_volume_snapshot(
    candles: pd.DataFrame | None,
    pair: str,
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Compare the latest *completed* candle's tick volume with its recent median.

    MT5 returns the currently-forming candle as the final row. It is deliberately
    excluded so a partially formed candle cannot manufacture a volume signal.
    """
    cfg = config or {}
    lookback = max(int(cfg.get("lookback_candles", 20)), 5)
    low_ratio = float(cfg.get("low_ratio", 0.65))
    high_ratio = float(cfg.get("high_ratio", 1.25))

    if candles is None or "tick_volume" not in candles or len(candles) < lookback + 2:
        return {
            "regime": "unknown",
            "ratio": None,
            "current": None,
            "baseline": None,
            "reason": "Insufficient completed tick-volume history",
        }

    completed = candles.iloc[:-1].copy()
    volumes = pd.to_numeric(completed["tick_volume"], errors="coerce").dropna()
    if len(volumes) < lookback + 1:
        return {
            "regime": "unknown",
            "ratio": None,
            "current": None,
            "baseline": None,
            "reason": "Insufficient valid completed tick-volume history",
        }

    current = float(volumes.iloc[-1])
    baseline = float(volumes.iloc[-(lookback + 1):-1].median())
    if baseline <= 0:
        return {
            "regime": "unknown",
            "ratio": None,
            "current": current,
            "baseline": baseline,
            "reason": "Tick-volume baseline is unavailable",
        }

    ratio = current / baseline
    if ratio < low_ratio:
        regime = "low"
    elif ratio >= high_ratio:
        regime = "high"
    else:
        regime = "normal"

    return {
        "regime": regime,
        "ratio": round(ratio, 3),
        "current": round(current, 2),
        "baseline": round(baseline, 2),
        "reason": f"Completed M15 tick volume is {ratio:.2f}× its {lookback}-candle median",
        "pair": pair,
    }