import asyncio
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from agents.portfolio_monitor import PortfolioMonitorAgent
from agents.risk_management import RiskManagementAgent
from core.state import SharedState
from core.trading_guards import TradingGuard


class Settings:
    def __init__(self):
        self._cfg = {
            "trading_guard": {
                "timezone": "Asia/Dubai",
                "entry_start": "05:00",
                "entry_stop": "23:30",
                "normal_max_open_positions": 5,
                "max_open_positions": 7,
                "exceptional_min_confidence": 80,
                "daily_loss_limit_pct": 15,
                "daily_profit_limit_pct": 20,
            },
            "fixed_lot_size": 0.05,
            "fixed_lot_sizes": {},
            "min_signal_confidence": 65,
            "pair_tp_usd": {},
            "basket_take_profit_usd": 30,
            "trailing_stop": {"enabled": True},
        }
        self.risk_per_trade_pct = 1
        self.default_sl_usd = 15
        self.default_tp_usd = 20
        self.monitor_interval_seconds = 15

    def get(self, key, default=None):
        return self._cfg.get(key, default)


class FakeBridge:
    def __init__(self, equity=1000, positions=None):
        self.account = {"balance": 1000, "equity": equity}
        self.positions = positions or []

    def get_account_info(self):
        return self.account

    def get_positions(self):
        return self.positions


class NoCloseExecution:
    async def close_all(self, state):
        raise AssertionError("Daily entry lock must not close open positions")


class CloseExecution:
    def __init__(self):
        self.calls = 0

    async def close_all(self, state, close_reason="manual"):
        self.calls += 1
        return [{"success": True, "profit": 12.0}]


def at(hour, minute, day=1):
    return datetime(2026, 8, day, hour, minute, tzinfo=ZoneInfo("Asia/Dubai"))


def signal(confidence):
    return {"direction": "BUY", "confidence": confidence, "summary": "test signal"}


class TradingGuardTests(unittest.TestCase):
    def setUp(self):
        self.settings = Settings()
        self.guard = TradingGuard(self.settings)
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.guard.state_path = Path(self.temp_dir.name) / "session.json"

    def test_dubai_window_and_session_boundary(self):
        state = SharedState()
        self.assertTrue(self.guard.evaluate(state, {"equity": 1000}, at(5, 0)).allowed)
        self.assertTrue(self.guard.evaluate(state, {"equity": 1000}, at(23, 29)).allowed)
        closed = self.guard.evaluate(state, {"equity": 1000}, at(23, 30))
        self.assertFalse(closed.allowed)
        self.assertIn("window is closed", closed.reason)
        self.assertEqual(self.guard.session_key(at(4, 59, day=2)), "2026-08-01")

    def test_daily_percentage_locks_are_sticky_until_next_dubai_session(self):
        state = SharedState()
        self.guard.evaluate(state, {"equity": 1000}, at(5, 0))
        profit = self.guard.evaluate(state, {"equity": 1200}, at(10, 0))
        self.assertFalse(profit.allowed)
        self.assertIn("profit", profit.reason.lower())
        self.assertFalse(self.guard.evaluate(state, {"equity": 1000}, at(11, 0)).allowed)
        self.assertTrue(self.guard.evaluate(state, {"equity": 1000}, at(5, 0, day=2)).allowed)

    def test_restart_restores_the_session_baseline_and_lock(self):
        first_state = SharedState()
        self.guard.evaluate(first_state, {"equity": 1000}, at(5, 0))
        self.assertFalse(self.guard.evaluate(first_state, {"equity": 1200}, at(10, 0)).allowed)

        restarted_guard = TradingGuard(self.settings)
        restarted_guard.state_path = self.guard.state_path
        restarted_state = SharedState()
        after_restart = restarted_guard.evaluate(restarted_state, {"equity": 1000}, at(11, 0))
        self.assertFalse(after_restart.allowed)
        self.assertIn("profit", after_restart.reason.lower())

    def test_mid_session_start_without_a_baseline_fails_closed(self):
        state = SharedState()
        decision = self.guard.evaluate(state, {"equity": 1000}, at(10, 0))
        self.assertFalse(decision.allowed)
        self.assertIn("baseline unavailable", decision.reason.lower())

    def test_reserved_slots_require_strictly_more_than_eighty_percent(self):
        risk = RiskManagementAgent(self.settings)
        state = SharedState()
        open_positions = [{"symbol": f"P{i}"} for i in range(5)]
        signals = {"eighty": signal(80), "eighty_one": signal(81), "ninety": signal(90)}
        approved = risk.run(signals, {"balance": 1000}, open_positions, state)
        self.assertEqual([trade["pair"] for trade in approved], ["ninety", "eighty_one"])

    def test_daily_lock_does_not_stop_position_monitoring(self):
        state = SharedState()
        self.guard.evaluate(state, {"equity": 1000}, at(5, 0))
        self.guard.evaluate(state, {"equity": 850}, at(10, 0))
        bridge = FakeBridge(equity=850, positions=[{"symbol": "EURUSD", "profit": 0}])
        monitor = PortfolioMonitorAgent(
            bridge, self.settings, NoCloseExecution(), state, entry_guard=self.guard
        )
        asyncio.run(monitor.check_once())
        self.assertTrue(state.trading_active)
        self.assertEqual(len(state.open_positions), 1)

    def test_session_profit_target_realizes_a_profitable_basket_once(self):
        self.settings._cfg["trading_guard"]["daily_profit_limit_pct"] = 5
        self.settings._cfg["trading_guard"]["realize_basket_on_session_profit_target"] = True
        self.settings._cfg["basket_take_profit_usd"] = 1
        state = SharedState()
        state.trading_session_key = self.guard.session_key()
        state.session_start_equity = 1000
        bridge = FakeBridge(
            equity=1050,
            positions=[{"ticket": 101, "pair": "EURUSD", "profit": 12.0}],
        )
        execution = CloseExecution()
        monitor = PortfolioMonitorAgent(
            bridge, self.settings, execution, state, entry_guard=self.guard
        )

        asyncio.run(monitor.check_once())
        self.assertEqual(execution.calls, 1)
        self.assertTrue(state.session_profit_basket_close_requested)
        self.assertFalse(state.session_profit_basket_close_attempted)
        self.assertIn("profit limit", state.daily_entry_lock_reason.lower())


if __name__ == "__main__":
    unittest.main()