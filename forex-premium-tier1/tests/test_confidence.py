"""
Tests for ConfidenceCalibrator.

Validates:
- Score is always 0–100 (calibration bounds)
- Each component contributes correctly
- Volume regime low for forex caps the volume component
- Metals are not capped by low volume
- Full breakdown dict is present and correct
- Reason string is human-readable
- No component exceeds its defined maximum
"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from unittest.mock import MagicMock

from analysis.confidence import (
    ConfidenceCalibrator,
    MAX_BASE_TECHNICAL, MAX_TF_AGREEMENT, MAX_VOLUME_PARTICIPATION,
    MAX_CONTEXT_QUALITY, FOREX_LOW_VOL_CAP_FRACTION,
)
from analysis.volume_sense import VolumeClassification
from domain.models import VolumeRegime, ConfidenceBreakdown


def make_snapshot(
    pair="EURUSD",
    direction="BUY",
    tech_quality=0.5,
    agreements=2,
    total_tfs=3,
    volume_regime=VolumeRegime.NORMAL,
    participation_score=0.6,
    is_metal=False,
):
    """Build a minimal snapshot dict for confidence tests."""
    tf_agreement = agreements / total_tfs if total_tfs > 0 else 0.0
    # Map to _tf_agreement_score logic
    from analysis.multitf import _tf_agreement_score
    score = _tf_agreement_score(agreements, total_tfs)

    vol_result = VolumeClassification(
        pair=pair,
        regime=volume_regime,
        participation_score=participation_score,
        current_volume=500,
        median_volume=500,
        mean_volume=500,
        ratio_to_median=1.0,
        is_metal=is_metal,
        reason="test volume reason",
        window_used=50,
    )

    return {
        "pair": pair,
        "direction": direction,
        "is_metal": is_metal,
        "primary": {
            "tech_quality": tech_quality,
            "bullish_signals": 4,
            "bearish_signals": 1,
        },
        "confluence": {
            "agreements": agreements,
            "total_tfs": total_tfs,
            "tf_agreement_score": score,
            "summary": f"{agreements}/{total_tfs}",
        },
        "volume": vol_result,
    }


class TestConfidenceBounds:
    """Confidence must always stay in [0, 100]."""

    def test_perfect_conditions_max_100(self):
        cal = ConfidenceCalibrator()
        snap = make_snapshot(
            tech_quality=1.0,
            agreements=3,
            total_tfs=3,
            volume_regime=VolumeRegime.HIGH,
            participation_score=1.0,
        )
        bd = cal.score(snap, spread_ok=True, data_fresh=True, session_open=True)
        assert 0.0 <= bd.total <= 100.0

    def test_worst_conditions_min_0(self):
        cal = ConfidenceCalibrator()
        snap = make_snapshot(
            tech_quality=0.0,
            agreements=0,
            total_tfs=3,
            volume_regime=VolumeRegime.LOW,
            participation_score=0.0,
        )
        bd = cal.score(snap, spread_ok=False, data_fresh=False, session_open=False)
        assert 0.0 <= bd.total <= 100.0

    def test_score_never_exceeds_100(self):
        """Even with all maximums, score stays at 100."""
        cal = ConfidenceCalibrator()
        snap = make_snapshot(tech_quality=1.0, agreements=3, total_tfs=3,
                             participation_score=1.0)
        bd = cal.score(snap, spread_ok=True, data_fresh=True, session_open=True)
        assert bd.total <= 100.0

    def test_component_maximums_not_exceeded(self):
        cal = ConfidenceCalibrator()
        snap = make_snapshot(tech_quality=1.0, agreements=3, total_tfs=3,
                             participation_score=1.0)
        bd = cal.score(snap, spread_ok=True, data_fresh=True, session_open=True)
        assert bd.base_technical <= MAX_BASE_TECHNICAL
        assert bd.timeframe_agreement <= MAX_TF_AGREEMENT
        assert bd.volume_participation <= MAX_VOLUME_PARTICIPATION
        assert bd.context_quality <= MAX_CONTEXT_QUALITY


class TestVolumeComponentBehavior:
    """Volume component follows forex/metal rules."""

    def test_forex_low_volume_caps_component(self):
        cal = ConfidenceCalibrator()
        snap = make_snapshot(
            pair="EURUSD",
            is_metal=False,
            volume_regime=VolumeRegime.LOW,
            participation_score=0.3,
        )
        bd = cal.score(snap)
        cap = MAX_VOLUME_PARTICIPATION * FOREX_LOW_VOL_CAP_FRACTION
        assert bd.volume_participation <= cap + 0.01  # tiny float tolerance

    def test_metal_low_volume_not_capped(self):
        """Metal with low volume: participation score used in full (not capped)."""
        cal = ConfidenceCalibrator()
        snap_metal = make_snapshot(
            pair="XAUUSD",
            is_metal=True,
            volume_regime=VolumeRegime.LOW,
            participation_score=0.7,
        )
        snap_forex = make_snapshot(
            pair="EURUSD",
            is_metal=False,
            volume_regime=VolumeRegime.LOW,
            participation_score=0.7,
        )
        bd_metal = cal.score(snap_metal)
        bd_forex = cal.score(snap_forex)
        # Metal should have higher volume participation than forex at same score
        assert bd_metal.volume_participation > bd_forex.volume_participation

    def test_high_volume_full_contribution(self):
        cal = ConfidenceCalibrator()
        snap = make_snapshot(
            volume_regime=VolumeRegime.HIGH,
            participation_score=0.9,
        )
        bd = cal.score(snap, spread_ok=True, data_fresh=True, session_open=True)
        # High volume should give close to max points
        expected_min = 0.9 * MAX_VOLUME_PARTICIPATION * 0.8
        assert bd.volume_participation >= expected_min


class TestContextComponent:
    """Context component points correctly allocated."""

    def test_all_context_ok(self):
        cal = ConfidenceCalibrator()
        snap = make_snapshot()
        bd = cal.score(snap, spread_ok=True, data_fresh=True, session_open=True)
        assert bd.context_quality == MAX_CONTEXT_QUALITY

    def test_no_context_ok(self):
        cal = ConfidenceCalibrator()
        snap = make_snapshot()
        bd = cal.score(snap, spread_ok=False, data_fresh=False, session_open=False)
        assert bd.context_quality == 0.0

    def test_partial_context(self):
        cal = ConfidenceCalibrator()
        snap = make_snapshot()
        bd = cal.score(snap, spread_ok=True, data_fresh=False, session_open=False)
        # Only spread OK: 7 pts
        assert bd.context_quality == pytest.approx(7.0, abs=0.01)


class TestReasonString:
    def test_reason_contains_direction(self):
        cal = ConfidenceCalibrator()
        snap = make_snapshot(direction="BUY")
        bd = cal.score(snap)
        assert "BUY" in bd.reason

    def test_reason_contains_policy(self):
        cal = ConfidenceCalibrator()
        snap = make_snapshot()
        bd = cal.score(snap)
        assert "v1_fixed_table" in bd.reason

    def test_to_dict_complete(self):
        cal = ConfidenceCalibrator()
        snap = make_snapshot()
        bd = cal.score(snap)
        d = bd.to_dict()
        required_keys = [
            "total", "base_technical", "timeframe_agreement",
            "volume_participation", "context_quality",
            "volume_regime", "volume_score", "calibration_policy", "reason"
        ]
        for k in required_keys:
            assert k in d, f"Missing key: {k}"
