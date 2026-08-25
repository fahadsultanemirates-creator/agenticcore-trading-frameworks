"""
Premium Tier 1 — Calibrated Confidence Scorer.

Design rules:
- Score is always clamped to [0, 100]. No unbounded additive bonuses.
- Four transparent components:
    base_technical      0–40  pts  (technical signal quality)
    timeframe_agreement 0–25  pts  (M15/H1/H4 alignment)
    volume_participation 0–20 pts  (VolumeSense participation score)
    context_quality     0–15  pts  (spread, data age, session)
- A calibration policy table defines exact mappings (v1_fixed_table).
  These can be learned from outcome data in a future calibration pass.
- Full breakdown dict is always included in the output.
- No LLM call in this path.

Calibration policy: v1_fixed_table
-----------------------------------
| Component            | Weight | Max |
|----------------------|--------|-----|
| base_technical       | 40%    | 40  |
| timeframe_agreement  | 25%    | 25  |
| volume_participation | 20%    | 20  |
| context_quality      | 15%    | 15  |
| TOTAL                | 100%   | 100 |

Base technical (40 pts max):
  tech_quality score (0–1) × 40

Timeframe agreement (25 pts max):
  tf_agreement_score (0–1) × 25

Volume participation (20 pts max):
  participation_score (0–1) × 20
  If regime=LOW and pair is forex: capped at 8 pts (40% of max)
  If regime=LOW and pair is metal: full participation score used (volume = context only)

Context quality (15 pts max):
  spread OK:    +7 pts
  data fresh:   +5 pts
  session open: +3 pts
"""
from __future__ import annotations
import logging
from typing import Optional

from domain.models import ConfidenceBreakdown, VolumeRegime

logger = logging.getLogger("premium.confidence")

CALIBRATION_POLICY = "v1_fixed_table"

# Component maximums — must sum to 100
MAX_BASE_TECHNICAL = 40.0
MAX_TF_AGREEMENT = 25.0
MAX_VOLUME_PARTICIPATION = 20.0
MAX_CONTEXT_QUALITY = 15.0

# Low-volume volume cap for forex (% of max_volume_participation)
FOREX_LOW_VOL_CAP_FRACTION = 0.40  # 40% of 20 = 8 pts max


class ConfidenceCalibrator:
    """
    Produces a calibrated ConfidenceBreakdown from an MTF snapshot.

    Parameters
    ----------
    policy : str
        Name of the calibration table in use.
    """

    def __init__(self, policy: str = CALIBRATION_POLICY):
        self.policy = policy

    def score(
        self,
        snapshot: dict,
        spread_ok: bool = True,
        data_fresh: bool = True,
        session_open: bool = True,
    ) -> ConfidenceBreakdown:
        """
        Compute a full ConfidenceBreakdown from a MultiTimeframe snapshot.

        Parameters
        ----------
        snapshot : dict
            Output of MultiTimeframeAnalyser.analyse_pair().
        spread_ok : bool
            Whether the spread is within acceptable limits.
        data_fresh : bool
            Whether the data is fresh enough (not stale).
        session_open : bool
            Whether we are inside the Dubai entry window.
        """
        primary = snapshot.get("primary") or {}
        confluence = snapshot.get("confluence") or {}
        volume_result = snapshot.get("volume")
        is_metal = snapshot.get("is_metal", False)

        # ── Component 1: Base technical quality ───────────────────────────
        tech_quality = float(primary.get("tech_quality", 0.0))
        base_technical = min(MAX_BASE_TECHNICAL, tech_quality * MAX_BASE_TECHNICAL)

        # ── Component 2: Timeframe agreement ─────────────────────────────
        tf_agreement_score = float(confluence.get("tf_agreement_score", 0.0))
        timeframe_agreement = min(MAX_TF_AGREEMENT, tf_agreement_score * MAX_TF_AGREEMENT)

        # ── Component 3: Volume participation ────────────────────────────
        volume_regime = VolumeRegime.UNKNOWN
        volume_score = 0.5  # neutral default when no volume data
        volume_reason = "Volume data unavailable"

        if volume_result is not None:
            regime_str = (
                volume_result.regime.value
                if hasattr(volume_result, "regime")
                else volume_result.get("regime", "unknown")
            )
            volume_regime = VolumeRegime(regime_str)
            participation = (
                float(volume_result.participation_score)
                if hasattr(volume_result, "participation_score")
                else float(volume_result.get("participation_score", 0.5))
            )
            volume_score = participation
            volume_reason = (
                volume_result.reason
                if hasattr(volume_result, "reason")
                else volume_result.get("reason", "")
            )
        else:
            participation = 0.5

        raw_vol_pts = participation * MAX_VOLUME_PARTICIPATION

        if volume_regime == VolumeRegime.LOW and not is_metal:
            # Forex: cap at FOREX_LOW_VOL_CAP_FRACTION of max
            cap = MAX_VOLUME_PARTICIPATION * FOREX_LOW_VOL_CAP_FRACTION
            volume_participation = min(raw_vol_pts, cap)
        else:
            # Metals and normal/high: use full score
            volume_participation = min(MAX_VOLUME_PARTICIPATION, raw_vol_pts)

        # ── Component 4: Context quality ──────────────────────────────────
        context_pts = 0.0
        context_parts = []
        if spread_ok:
            context_pts += 7.0
            context_parts.append("spread OK (+7)")
        else:
            context_parts.append("spread elevated (+0)")
        if data_fresh:
            context_pts += 5.0
            context_parts.append("data fresh (+5)")
        else:
            context_parts.append("data stale (+0)")
        if session_open:
            context_pts += 3.0
            context_parts.append("session open (+3)")
        else:
            context_parts.append("session closed (+0)")
        context_quality = min(MAX_CONTEXT_QUALITY, context_pts)

        # ── Assemble breakdown ────────────────────────────────────────────
        bd = ConfidenceBreakdown(
            base_technical=round(base_technical, 2),
            timeframe_agreement=round(timeframe_agreement, 2),
            volume_participation=round(volume_participation, 2),
            context_quality=round(context_quality, 2),
            volume_regime=volume_regime,
            volume_score=round(volume_score, 4),
            volume_reason=volume_reason,
            calibration_policy=self.policy,
        )
        total = bd.compute_total()

        # Build human-readable reason
        direction = snapshot.get("direction", "HOLD")
        agreements = confluence.get("agreements", 0)
        total_tfs = confluence.get("total_tfs", 0)
        confluence_summary = confluence.get("summary", "")

        reason_lines = [
            f"Direction: {direction}",
            f"Technical quality: {tech_quality:.3f} → base={base_technical:.1f}/{MAX_BASE_TECHNICAL:.0f}",
            f"TF agreement: {agreements}/{total_tfs} ({confluence_summary}) → {timeframe_agreement:.1f}/{MAX_TF_AGREEMENT:.0f}",
            f"Volume: {volume_regime.value} (score={volume_score:.3f}) → {volume_participation:.1f}/{MAX_VOLUME_PARTICIPATION:.0f}",
            f"Context: {', '.join(context_parts)} → {context_quality:.1f}/{MAX_CONTEXT_QUALITY:.0f}",
            f"TOTAL: {total:.1f}/100 [policy={self.policy}]",
        ]
        bd.reason = " | ".join(reason_lines)

        logger.debug(
            f"[Confidence] {snapshot.get('pair', '?')} → {total:.1f} "
            f"(tech={base_technical:.1f} tf={timeframe_agreement:.1f} "
            f"vol={volume_participation:.1f} ctx={context_quality:.1f})"
        )
        return bd
