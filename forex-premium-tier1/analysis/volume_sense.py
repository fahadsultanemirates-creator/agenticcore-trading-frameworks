"""
Premium Tier 1 — VolumeSense module.

Design rules:
- Uses ONLY completed candles (no forming candle tick volume).
- Measures current bar's tick_volume against a trailing median/average window.
- Classifies as LOW / NORMAL / HIGH with a numeric participation score (0–1).
- Explains WHY in a human-readable reason string.
- Does NOT hard-block metals; uses volume as context/support for metals.
- Forex entries MAY be gated by LOW volume when configured.
- The module never calls an LLM.

Participation score semantics:
  0.0 – 0.35 → LOW volume
  0.35 – 0.70 → NORMAL volume
  0.70 – 1.0 → HIGH volume  (capped at 1.0 when 2× median)
"""
from __future__ import annotations
import logging
from dataclasses import dataclass
from typing import Optional

import numpy as np
import pandas as pd

from domain.models import VolumeRegime

logger = logging.getLogger("premium.volume_sense")

# Symbols treated as metals — volume used as context, not hard gate
METAL_SYMBOLS = frozenset({"XAUUSD", "XAGUSD", "GOLD", "SILVER"})

# Default thresholds (configurable)
DEFAULT_WINDOW = 50         # bars for trailing median
LOW_THRESHOLD = 0.65        # below 65% of median → LOW
HIGH_THRESHOLD = 1.40       # above 140% of median → HIGH


@dataclass
class VolumeClassification:
    """Result of a VolumeSense.classify() call."""
    pair: str
    regime: VolumeRegime
    participation_score: float    # 0.0–1.0
    current_volume: float
    median_volume: float
    mean_volume: float
    ratio_to_median: float
    is_metal: bool
    reason: str
    window_used: int

    def to_dict(self) -> dict:
        return {
            "pair": self.pair,
            "regime": self.regime.value,
            "participation_score": round(self.participation_score, 4),
            "current_volume": self.current_volume,
            "median_volume": round(self.median_volume, 2),
            "mean_volume": round(self.mean_volume, 2),
            "ratio_to_median": round(self.ratio_to_median, 4),
            "is_metal": self.is_metal,
            "reason": self.reason,
            "window_used": self.window_used,
        }


class VolumeSense:
    """
    Volume participation classifier for Premium Tier 1.

    Parameters
    ----------
    window : int
        Number of completed candles for the trailing window.
    low_threshold : float
        Ratio below which volume is classified as LOW (default 0.65).
    high_threshold : float
        Ratio above which volume is classified as HIGH (default 1.40).
    """

    def __init__(
        self,
        window: int = DEFAULT_WINDOW,
        low_threshold: float = LOW_THRESHOLD,
        high_threshold: float = HIGH_THRESHOLD,
    ):
        self.window = window
        self.low_threshold = low_threshold
        self.high_threshold = high_threshold

    def _is_metal(self, pair: str) -> bool:
        return any(m in pair.upper() for m in ("XAU", "XAG", "GOLD", "SILVER"))

    def classify(self, pair: str, df: pd.DataFrame) -> VolumeClassification:
        """
        Classify volume for the last completed candle in df.

        The df must contain only completed candles.
        The last row of df is the most-recently-completed candle.
        The window is computed from all rows EXCLUDING the last (current) row,
        to avoid the current bar biasing its own baseline.
        """
        if df is None or len(df) < 5:
            return VolumeClassification(
                pair=pair,
                regime=VolumeRegime.UNKNOWN,
                participation_score=0.5,
                current_volume=0,
                median_volume=0,
                mean_volume=0,
                ratio_to_median=1.0,
                is_metal=self._is_metal(pair),
                reason="Insufficient data for volume classification",
                window_used=0,
            )

        volumes = df["tick_volume"].astype(float)

        # Current = last completed candle
        current_vol = float(volumes.iloc[-1])

        # Baseline window: all candles before the current one, up to self.window bars
        baseline_series = volumes.iloc[-(self.window + 1):-1]
        if len(baseline_series) < 3:
            baseline_series = volumes.iloc[:-1]

        median_vol = float(np.median(baseline_series))
        mean_vol = float(np.mean(baseline_series))

        # Avoid division by zero
        if median_vol <= 0:
            return VolumeClassification(
                pair=pair,
                regime=VolumeRegime.UNKNOWN,
                participation_score=0.5,
                current_volume=current_vol,
                median_volume=median_vol,
                mean_volume=mean_vol,
                ratio_to_median=1.0,
                is_metal=self._is_metal(pair),
                reason="Median volume is zero — cannot classify",
                window_used=len(baseline_series),
            )

        ratio = current_vol / median_vol
        is_metal = self._is_metal(pair)

        # Classify regime
        if ratio < self.low_threshold:
            regime = VolumeRegime.LOW
        elif ratio > self.high_threshold:
            regime = VolumeRegime.HIGH
        else:
            regime = VolumeRegime.NORMAL

        # Participation score: 0–1 range
        # 0.0 = extremely low (ratio=0), 0.5 = at median, 1.0 = at 2× median+
        score = min(1.0, max(0.0, ratio / 2.0))

        # Build reason string
        pct = (ratio - 1.0) * 100
        direction_str = f"{'above' if pct >= 0 else 'below'} median by {abs(pct):.0f}%"
        asset_context = (
            "(metal — used as context, not a hard gate)"
            if is_metal
            else "(forex — may gate entry if low)"
        )
        reason_parts = [
            f"Current vol={current_vol:.0f}, median={median_vol:.0f} over {len(baseline_series)} bars.",
            f"Ratio={ratio:.3f} → {direction_str}.",
            f"Regime: {regime.value.upper()}. Score={score:.3f}.",
            asset_context,
        ]

        if regime == VolumeRegime.LOW:
            if is_metal:
                reason_parts.append(
                    "Low volume for metals: treated as weaker participation context, not a rejection."
                )
            else:
                reason_parts.append(
                    "Low volume for forex: signal confidence will be reduced; entry may be gated."
                )
        elif regime == VolumeRegime.HIGH:
            reason_parts.append("High volume: strong participation supports signal conviction.")
        else:
            reason_parts.append("Normal volume: adequate participation for standard analysis.")

        return VolumeClassification(
            pair=pair,
            regime=regime,
            participation_score=round(score, 4),
            current_volume=current_vol,
            median_volume=median_vol,
            mean_volume=mean_vol,
            ratio_to_median=round(ratio, 4),
            is_metal=is_metal,
            reason=" ".join(reason_parts),
            window_used=len(baseline_series),
        )
