"""Tests for the deterministic daily P&L guardrails."""

import os
import sys
import tempfile
import unittest
from dataclasses import replace

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from config.settings import CryptoSettings
from domain.models import DailyGuardStatus
from risk.daily_guard import evaluate_daily_guard, is_daily_guard_halted
from runtime.orchestrator import run_cycle


class FailingClient:
    """Fails if a halted cycle attempts to fetch public market data."""

    def get_contract_list(self):
        raise AssertionError("daily guard should halt before market-data calls")


class TestDailyGuard(unittest.TestCase):
    def setUp(self):
        self.settings = CryptoSettings(
            daily_loss_limit_usdt=20.0,
            daily_profit_target_usdt=40.0,
        )

    def test_unknown_pnl_does_not_invent_account_telemetry(self):
        self.assertEqual(
            evaluate_daily_guard(None, self.settings),
            DailyGuardStatus.UNKNOWN,
        )

    def test_loss_boundary_halts(self):
        status = evaluate_daily_guard(-20.0, self.settings)
        self.assertEqual(status, DailyGuardStatus.LOSS_LIMIT_REACHED)
        self.assertTrue(is_daily_guard_halted(status))

    def test_profit_boundary_halts(self):
        status = evaluate_daily_guard(40.0, self.settings)
        self.assertEqual(status, DailyGuardStatus.PROFIT_TARGET_REACHED)
        self.assertTrue(is_daily_guard_halted(status))

    def test_between_boundaries_is_active(self):
        self.assertEqual(
            evaluate_daily_guard(-19.99, self.settings),
            DailyGuardStatus.ACTIVE,
        )
        self.assertEqual(
            evaluate_daily_guard(39.99, self.settings),
            DailyGuardStatus.ACTIVE,
        )

    def test_non_finite_pnl_is_rejected(self):
        with self.assertRaises(ValueError):
            evaluate_daily_guard(float("nan"), self.settings)

    def test_reached_boundary_halts_cycle_before_market_data(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = replace(
                self.settings,
                runtime_dir=os.path.join(temp_dir, "runtime"),
                log_dir=os.path.join(temp_dir, "logs"),
            )
            state = run_cycle(
                settings,
                client=FailingClient(),
                daily_pnl_usdt=-20.0,
            )

        self.assertEqual(state.daily_guard_status, DailyGuardStatus.LOSS_LIMIT_REACHED)
        self.assertEqual(state.candidates, [])
        self.assertIn("Daily guard halted", state.last_error or "")

    def test_altered_fixed_profile_is_rejected_before_market_data(self):
        settings = replace(self.settings, position_notional_usdt=50.01)
        with self.assertRaises(ValueError):
            run_cycle(settings, client=FailingClient())


if __name__ == "__main__":
    unittest.main()