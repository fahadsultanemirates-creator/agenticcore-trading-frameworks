"""
Tests for RiskGuard and EntryWindowGuard.

Validates:
- Dubai entry window: blocks outside 05:00–23:29
- Daily loss limit blocks new entries
- Daily profit limit blocks new entries
- Max portfolio positions hard block
- Max positions per pair hard block
- Spread hard block
- Stale data hard block
- Kill switch blocks everything
- Minimum confidence rejects weak signals
- Volume gate: forex blocked on LOW volume (configurable)
- Metals NOT blocked by volume gate by default
- All checks pass when conditions are correct
"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from datetime import datetime
from zoneinfo import ZoneInfo

from config.settings import RiskConfig
from risk.guards import RiskGuard, EntryWindowGuard

DUBAI_TZ = ZoneInfo("Asia/Dubai")


def make_risk_cfg(**overrides) -> RiskConfig:
    cfg = RiskConfig(
        entry_window_start="05:00",
        entry_window_stop="23:29",
        max_portfolio_positions=7,
        max_positions_per_pair=3,
        daily_loss_limit_pct=15.0,
        daily_profit_limit_pct=20.0,
        max_spread_pips=5.0,
        max_data_age_seconds=300.0,
        min_confidence=60.0,
        gate_forex_on_low_volume=True,
        gate_metals_on_low_volume=False,
    )
    for k, v in overrides.items():
        setattr(cfg, k, v)
    return cfg


def make_guard(**cfg_overrides) -> RiskGuard:
    return RiskGuard(make_risk_cfg(**cfg_overrides))


def _dubai_time(hour: int, minute: int) -> datetime:
    return datetime(2024, 6, 15, hour, minute, 0, tzinfo=DUBAI_TZ)


def base_evaluate(guard, **overrides):
    """Call evaluate_entry with safe defaults, allowing overrides."""
    kwargs = dict(
        pair="EURUSD",
        direction="BUY",
        confidence=70.0,
        spread_pips=2.0,
        data_age_seconds=10.0,
        open_positions=[],
        session_pnl_pct=0.0,
        volume_regime="normal",
        is_metal=False,
        now=_dubai_time(10, 0),  # inside window
    )
    kwargs.update(overrides)
    return guard.evaluate_entry(**kwargs)


class TestEntryWindowGuard:
    def test_inside_window_allowed(self):
        g = EntryWindowGuard("05:00", "23:29")
        assert g.is_open(_dubai_time(10, 0)) is True
        assert g.is_open(_dubai_time(5, 0)) is True
        assert g.is_open(_dubai_time(23, 29)) is True

    def test_before_window_blocked(self):
        g = EntryWindowGuard("05:00", "23:29")
        assert g.is_open(_dubai_time(4, 59)) is False
        assert g.is_open(_dubai_time(0, 0)) is False

    def test_after_window_blocked(self):
        g = EntryWindowGuard("05:00", "23:29")
        assert g.is_open(_dubai_time(23, 30)) is False
        assert g.is_open(_dubai_time(23, 59)) is False

    def test_session_key_advances_at_midnight(self):
        g = EntryWindowGuard("05:00", "23:29")
        # Before 05:00 → previous day's session key
        t = _dubai_time(3, 0)
        key = g.session_key(t)
        assert key == "2024-06-14"

        # After 05:00 → current day's key
        t2 = _dubai_time(6, 0)
        key2 = g.session_key(t2)
        assert key2 == "2024-06-15"


class TestRiskGuardEntryWindow:
    def test_outside_window_rejected(self):
        guard = make_guard()
        result = base_evaluate(guard, now=_dubai_time(23, 30))
        assert result.allowed is False
        assert "entry window" in result.reason.lower() or "23:29" in result.reason

    def test_inside_window_not_blocked_by_window(self):
        guard = make_guard()
        result = base_evaluate(guard, now=_dubai_time(12, 0))
        assert result.allowed is True


class TestDailyLimits:
    def test_daily_loss_limit_blocks(self):
        guard = make_guard()
        result = base_evaluate(guard, session_pnl_pct=-15.1)
        assert result.allowed is False
        assert "loss" in result.reason.lower()

    def test_daily_profit_limit_blocks(self):
        guard = make_guard()
        result = base_evaluate(guard, session_pnl_pct=20.1)
        assert result.allowed is False
        assert "profit" in result.reason.lower()

    def test_within_limits_not_blocked(self):
        guard = make_guard()
        result = base_evaluate(guard, session_pnl_pct=5.0)
        assert result.allowed is True

    def test_exactly_at_loss_limit_blocked(self):
        guard = make_guard()
        result = base_evaluate(guard, session_pnl_pct=-15.0)
        assert result.allowed is False


class TestPositionLimits:
    def _make_positions(self, n, pair="DIFFERENT"):
        return [{"pair": pair, "direction": "BUY"} for _ in range(n)]

    def test_max_portfolio_blocks(self):
        guard = make_guard(max_portfolio_positions=3)
        result = base_evaluate(
            guard,
            open_positions=self._make_positions(3, "GBPUSD"),
        )
        assert result.allowed is False
        assert "portfolio" in result.reason.lower() or "position" in result.reason.lower()

    def test_max_pair_positions_blocks(self):
        guard = make_guard(max_positions_per_pair=3)
        positions = [{"pair": "EURUSD", "direction": "BUY"} for _ in range(3)]
        result = base_evaluate(
            guard,
            pair="EURUSD",
            open_positions=positions,
        )
        assert result.allowed is False
        assert "EURUSD" in result.reason or "pair" in result.reason.lower()

    def test_below_limits_allowed(self):
        guard = make_guard()
        result = base_evaluate(
            guard,
            open_positions=self._make_positions(2, "USDJPY"),
        )
        assert result.allowed is True

    def test_same_pair_scale_in_requires_independent_confirmation(self):
        guard = make_guard()
        result = base_evaluate(
            guard,
            pair="EURUSD",
            open_positions=[{"pair": "EURUSD", "direction": "BUY"}],
        )
        assert result.allowed is False
        assert "independent confirmation" in result.reason.lower()

    def test_same_pair_scale_in_can_pass_with_explicit_confirmation(self):
        guard = make_guard()
        result = base_evaluate(
            guard,
            pair="EURUSD",
            open_positions=[{"pair": "EURUSD", "direction": "BUY"}],
            independent_confirmation=True,
        )
        assert result.allowed is True

    def test_currency_exposure_blocks_stacked_currency(self):
        guard = make_guard(max_currency_exposure=2)
        result = base_evaluate(
            guard,
            pair="EURUSD",
            open_positions=[
                {"pair": "GBPUSD", "direction": "BUY"},
                {"pair": "AUDUSD", "direction": "BUY"},
            ],
        )
        assert result.allowed is False
        assert "currency exposure" in result.reason.lower()

    def test_correlation_exposure_blocks_cluster(self):
        guard = make_guard(max_correlated_positions=2, max_currency_exposure=5)
        result = base_evaluate(
            guard,
            pair="EURUSD",
            open_positions=[
                {"pair": "GBPUSD", "direction": "BUY"},
                {"pair": "AUDUSD", "direction": "BUY"},
            ],
        )
        assert result.allowed is False
        assert "correlated exposure" in result.reason.lower()


class TestSpreadAndData:
    def test_wide_spread_blocks(self):
        guard = make_guard(max_spread_pips=5.0)
        result = base_evaluate(guard, spread_pips=6.0)
        assert result.allowed is False
        assert "spread" in result.reason.lower()

    def test_acceptable_spread_passes(self):
        guard = make_guard(max_spread_pips=5.0)
        result = base_evaluate(guard, spread_pips=3.0)
        assert result.allowed is True

    def test_stale_data_blocks(self):
        guard = make_guard(max_data_age_seconds=300.0)
        result = base_evaluate(guard, data_age_seconds=400.0)
        assert result.allowed is False
        assert "stale" in result.reason.lower()

    def test_fresh_data_passes(self):
        guard = make_guard()
        result = base_evaluate(guard, data_age_seconds=5.0)
        assert result.allowed is True


class TestKillSwitch:
    def test_kill_switch_blocks_all(self):
        guard = make_guard()
        guard.set_kill_switch(True, "test")
        result = base_evaluate(guard)
        assert result.allowed is False
        assert "kill" in result.reason.lower()

    def test_kill_switch_deactivated_allows(self):
        guard = make_guard()
        guard.set_kill_switch(True, "test")
        guard.set_kill_switch(False)
        result = base_evaluate(guard)
        assert result.allowed is True

    def test_pause_blocks(self):
        guard = make_guard()
        guard.set_pause(True, "test pause")
        result = base_evaluate(guard)
        assert result.allowed is False
        assert "pause" in result.reason.lower()


class TestConfidenceGate:
    def test_low_confidence_blocks(self):
        guard = make_guard(min_confidence=60.0)
        result = base_evaluate(guard, confidence=55.0)
        assert result.allowed is False
        assert "confidence" in result.reason.lower()

    def test_sufficient_confidence_passes(self):
        guard = make_guard(min_confidence=60.0)
        result = base_evaluate(guard, confidence=65.0)
        assert result.allowed is True


class TestVolumeGate:
    def test_forex_low_volume_gated(self):
        guard = make_guard(gate_forex_on_low_volume=True)
        result = base_evaluate(guard, pair="EURUSD", is_metal=False, volume_regime="low")
        assert result.allowed is False
        assert "low volume" in result.reason.lower() or "volume" in result.reason.lower()

    def test_forex_low_volume_not_gated_when_disabled(self):
        guard = make_guard(gate_forex_on_low_volume=False)
        result = base_evaluate(guard, pair="EURUSD", is_metal=False, volume_regime="low")
        assert result.allowed is True

    def test_metal_low_volume_not_gated_by_default(self):
        """Metals are NOT gated by low volume when gate_metals_on_low_volume=False."""
        guard = make_guard(gate_forex_on_low_volume=True, gate_metals_on_low_volume=False)
        result = base_evaluate(guard, pair="XAUUSD", is_metal=True, volume_regime="low")
        assert result.allowed is True

    def test_metal_low_volume_gated_when_configured(self):
        guard = make_guard(gate_metals_on_low_volume=True)
        result = base_evaluate(guard, pair="XAUUSD", is_metal=True, volume_regime="low")
        assert result.allowed is False

    def test_normal_volume_passes_volume_gate(self):
        guard = make_guard(gate_forex_on_low_volume=True)
        result = base_evaluate(guard, volume_regime="normal")
        assert result.allowed is True


class TestChecksDict:
    def test_approved_decision_has_all_checks_true(self):
        guard = make_guard()
        result = base_evaluate(guard)
        assert result.allowed is True
        assert result.checks.get("kill_switch") is True
        assert result.checks.get("spread") is True
        assert result.checks.get("entry_window") is True
        assert result.checks.get("min_confidence") is True

    def test_rejected_decision_has_failing_check(self):
        guard = make_guard()
        guard.set_kill_switch(True, "test")
        result = base_evaluate(guard)
        assert result.allowed is False
        assert result.checks.get("kill_switch") is False
