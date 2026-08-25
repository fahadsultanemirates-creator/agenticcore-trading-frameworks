"""
Premium Tier 1 — Multi-Timeframe Market Data Pipeline.

Fetches and analyses M15, H1, H4 completed candles.
Produces per-pair multi-TF snapshots and confluence scores.
No LLM call in this path.
"""
from __future__ import annotations
import logging
from typing import Optional, Union

from .indicators import compute_indicators
from domain.models import Signal, VolumeRegime

logger = logging.getLogger("premium.multitf")

TIMEFRAMES = ["M15", "H1", "H4"]
PRIMARY_TF = "M15"


def _confluence_agreement(
    tf_results: dict,
    primary_direction: str,
) -> tuple[int, int, str]:
    """
    Check how many TFs agree with the primary direction.

    Returns:
        agreements  — count of agreeing TFs
        total_tfs   — TFs with data
        summary     — human-readable explanation
    """
    agreements = 0
    details = []
    for tf, snap in tf_results.items():
        if snap is None:
            details.append(f"{tf}:NO_DATA")
            continue
        bull = snap["bullish_signals"]
        bear = snap["bearish_signals"]
        if primary_direction == "BUY" and bull > bear:
            agreements += 1
            details.append(f"{tf}:BUY✓")
        elif primary_direction == "SELL" and bear > bull:
            agreements += 1
            details.append(f"{tf}:SELL✓")
        else:
            details.append(f"{tf}:NEUTRAL")

    total_tfs = sum(1 for v in tf_results.values() if v is not None)
    summary = f"{agreements}/{total_tfs} [{', '.join(details)}]"
    return agreements, total_tfs, summary


def _tf_agreement_score(agreements: int, total_tfs: int) -> float:
    """
    Translate TF agreement into a 0–1 score.
    Full agreement (3/3 or 2/2) → 1.0
    2/3 → 0.6
    1/3 → 0.2
    0   → 0.0
    """
    if total_tfs == 0:
        return 0.0
    ratio = agreements / total_tfs
    if ratio >= 1.0:
        return 1.0
    elif ratio >= 0.65:
        return 0.6
    elif ratio >= 0.33:
        return 0.2
    return 0.0


class MultiTimeframeAnalyser:
    """
    Fetches completed candles across M15/H1/H4 and produces a per-pair
    analysis snapshot suitable for confidence scoring and signal generation.
    """

    def __init__(self, bridge, volume_sense=None):
        self.bridge = bridge
        self.volume_sense = volume_sense  # optional VolumeSense instance

    def analyse_pair(self, pair: str) -> Optional[dict]:
        """
        Analyse one pair across all timeframes.
        Returns a snapshot dict or None if insufficient data.
        """
        tf_results = {}
        tf_candles = {}
        for tf in TIMEFRAMES:
            try:
                df = self.bridge.get_completed_candles(pair, timeframe=tf, bars=250)
                tf_candles[tf] = df
                tf_results[tf] = compute_indicators(pair, df)
            except Exception as exc:
                logger.warning(f"[MTF] {pair}/{tf} error: {exc}")
                tf_results[tf] = None
                tf_candles[tf] = None

        # Primary snapshot from M15; fall back to H1, H4
        primary = tf_results.get(PRIMARY_TF)
        if primary is None:
            for tf in ["H1", "H4"]:
                primary = tf_results.get(tf)
                if primary is not None:
                    break
        if primary is None:
            logger.warning(f"[MTF] {pair}: no primary TF snapshot available")
            return None

        # Direction
        bull = primary["bullish_signals"]
        bear = primary["bearish_signals"]
        if bull > bear:
            direction = "BUY"
        elif bear > bull:
            direction = "SELL"
        else:
            direction = "HOLD"

        agreements, total_tfs, confluence_summary = _confluence_agreement(
            tf_results, direction
        )
        tf_agreement_score = _tf_agreement_score(agreements, total_tfs)

        # Volume sense on primary TF candles
        volume_result = None
        if self.volume_sense and tf_candles.get(PRIMARY_TF) is not None:
            try:
                volume_result = self.volume_sense.classify(
                    pair, tf_candles[PRIMARY_TF]
                )
            except Exception as exc:
                logger.warning(f"[MTF] {pair} volume_sense error: {exc}")

        return {
            "pair": pair,
            "direction": direction,
            "primary_tf": PRIMARY_TF,
            "primary": primary,
            "timeframes": {tf: v for tf, v in tf_results.items() if v is not None},
            "tf_candles": tf_candles,  # kept for downstream; not serialised
            "confluence": {
                "agreements": agreements,
                "total_tfs": total_tfs,
                "tf_agreement_score": tf_agreement_score,
                "summary": confluence_summary,
            },
            "volume": volume_result,
        }

    def scan_all(self, pairs: list) -> dict:
        """
        Scan all pairs. Returns {pair: snapshot | None}.
        Errors per pair are caught and logged.
        """
        results = {}
        for pair in pairs:
            try:
                results[pair] = self.analyse_pair(pair)
            except Exception as exc:
                logger.error(f"[MTF] scan_all {pair}: {exc}")
                results[pair] = None
        ok = sum(1 for v in results.values() if v is not None)
        logger.info(f"[MTF] Scanned {ok}/{len(pairs)} pairs successfully")
        return results
