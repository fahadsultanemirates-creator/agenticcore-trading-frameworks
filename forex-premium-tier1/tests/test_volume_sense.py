"""
Tests for VolumeSense module.

Validates:
- Classification of LOW / NORMAL / HIGH volume correctly
- Participation score in expected ranges per regime
- Metal symbols are NOT hard-blocked (no forced rejection)
- Forex symbols classified correctly
- Reason string is informative
- Insufficient data returns UNKNOWN
"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
import numpy as np
import pandas as pd

from analysis.volume_sense import VolumeSense, VolumeClassification
from domain.models import VolumeRegime
from tests.conftest import make_candles, make_low_volume_candles, make_high_volume_candles


@pytest.fixture
def vs():
    return VolumeSense(window=50, low_threshold=0.65, high_threshold=1.40)


class TestVolumeClassification:
    def test_normal_volume(self, vs):
        df = make_candles("EURUSD", bars=100, vol_multiplier=1.0)
        # Set last bar to exactly median level
        median = df["tick_volume"].iloc[:-1].median()
        df.loc[df.index[-1], "tick_volume"] = int(median)
        result = vs.classify("EURUSD", df)

        assert result.regime == VolumeRegime.NORMAL
        assert 0.35 <= result.participation_score <= 0.70
        assert result.is_metal is False

    def test_low_volume_forex(self, vs):
        df = make_low_volume_candles("EURUSD", bars=100)
        result = vs.classify("EURUSD", df)

        assert result.regime == VolumeRegime.LOW
        assert result.participation_score < 0.35
        assert result.is_metal is False
        assert "low volume" in result.reason.lower() or "LOW" in result.reason

    def test_high_volume_forex(self, vs):
        df = make_high_volume_candles("EURUSD", bars=100)
        result = vs.classify("EURUSD", df)

        assert result.regime == VolumeRegime.HIGH
        assert result.participation_score > 0.65
        assert result.is_metal is False

    def test_metal_xauusd_classified_not_blocked(self, vs):
        """Gold volume LOW → is_metal=True, no forced rejection (handled by risk guard)."""
        df = make_low_volume_candles("XAUUSD", bars=100)
        result = vs.classify("XAUUSD", df)

        assert result.is_metal is True
        assert result.regime == VolumeRegime.LOW
        # Verify no hard block signal in the classification itself
        assert "not a rejection" in result.reason.lower() or "context" in result.reason.lower()

    def test_metal_xagusd_is_metal(self, vs):
        df = make_candles("XAGUSD", bars=100)
        result = vs.classify("XAGUSD", df)
        assert result.is_metal is True

    def test_participation_score_range(self, vs):
        """Score must always be in [0, 1]."""
        for vol_mult in [0.05, 0.3, 1.0, 2.0, 5.0]:
            df = make_candles("GBPUSD", bars=100, vol_multiplier=1.0)
            median = df["tick_volume"].iloc[:-1].median()
            df.loc[df.index[-1], "tick_volume"] = int(median * vol_mult)
            result = vs.classify("GBPUSD", df)
            assert 0.0 <= result.participation_score <= 1.0, (
                f"Score {result.participation_score} out of range at vol_mult={vol_mult}"
            )

    def test_insufficient_data_returns_unknown(self, vs):
        df = make_candles("EURUSD", bars=3)
        result = vs.classify("EURUSD", df)
        assert result.regime == VolumeRegime.UNKNOWN

    def test_none_data_returns_unknown(self, vs):
        result = vs.classify("EURUSD", None)
        assert result.regime == VolumeRegime.UNKNOWN

    def test_reason_string_present(self, vs):
        df = make_candles("USDJPY", bars=100)
        result = vs.classify("USDJPY", df)
        assert isinstance(result.reason, str)
        assert len(result.reason) > 20

    def test_to_dict_serializable(self, vs):
        df = make_candles("EURUSD", bars=100)
        result = vs.classify("EURUSD", df)
        d = result.to_dict()
        assert "regime" in d
        assert "participation_score" in d
        assert "reason" in d
        assert isinstance(d["participation_score"], float)

    def test_median_excludes_current_bar(self, vs):
        """Baseline median must not include the current bar."""
        df = make_candles("EURUSD", bars=60, vol_multiplier=1.0)
        # Set current bar to absurdly high volume
        df.loc[df.index[-1], "tick_volume"] = 999999
        result = vs.classify("EURUSD", df)
        # Ratio should be very high → HIGH regime
        assert result.regime == VolumeRegime.HIGH
        # But median should still be close to the baseline (not polluted)
        assert result.median_volume < 10000
