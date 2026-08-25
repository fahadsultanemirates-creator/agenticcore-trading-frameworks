import asyncio
import tempfile
import unittest
from pathlib import Path

from agents.portfolio_monitor import PortfolioMonitorAgent
from agents.trade_execution import TradeExecutionAgent
from core.close_ledger import CloseReconciliationLedger
from core.state import SharedState


class Broker:
    def get_account_info(self):
        return {"balance": 1000, "equity": 1000}

    def get_positions(self):
        return []

    def get_closed_deal(self, ticket):
        return {"ticket": ticket, "pair": "EURUSD", "profit": 3.5}


class PreparedCloseBroker(Broker):
    def __init__(self):
        self.positions = [{"ticket": 56, "pair": "EURUSD", "direction": "BUY"}]
        self.close_calls = 0

    def get_positions(self):
        return list(self.positions)

    def close_position(self, _ticket):
        self.close_calls += 1
        self.positions = []
        return {"success": True, "pair": "EURUSD", "direction": "BUY"}


class Settings:
    monitor_interval_seconds = 15
    broker = {}

    def get(self, _key, default=None):
        return default


class CloseLedgerTests(unittest.TestCase):
    def test_pending_close_survives_restart_and_is_accounted_once(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "close-ledger.json"
            first = CloseReconciliationLedger(path)
            self.assertTrue(
                first.request(
                    55,
                    {"ticket": 55, "pair": "EURUSD", "direction": "BUY"},
                    "session_profit",
                )
            )

            restarted = CloseReconciliationLedger(path)
            state = SharedState()
            monitor = PortfolioMonitorAgent(
                Broker(), Settings(), execution_agent=None, state=state,
                close_ledger=restarted,
            )
            asyncio.run(monitor.refresh_from_mt5())

            self.assertEqual(state.daily_pnl_usd, 3.5)
            self.assertEqual(restarted.pending(), [])
            self.assertFalse(restarted.request(55, {}, "session_profit"))

    def test_prepared_close_is_resumed_after_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = CloseReconciliationLedger(Path(directory) / "close-ledger.json")
            ledger.request(
                56, {"ticket": 56, "pair": "EURUSD", "direction": "BUY"},
                "session_profit",
            )
            bridge = PreparedCloseBroker()
            state = SharedState()
            execution = TradeExecutionAgent(bridge, Settings(), close_ledger=ledger)
            monitor = PortfolioMonitorAgent(
                bridge, Settings(), execution, state, close_ledger=ledger
            )

            asyncio.run(monitor.refresh_from_mt5())
            self.assertEqual(bridge.close_calls, 1)
            asyncio.run(monitor.refresh_from_mt5())
            self.assertEqual(state.daily_pnl_usd, 3.5)

    def test_confirmed_unaccounted_close_is_resumed_exactly_once(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "close-ledger.json"
            ledger = CloseReconciliationLedger(path)
            ledger.request(
                57, {"ticket": 57, "pair": "EURUSD", "direction": "BUY"},
                "session_profit",
            )
            ledger.mark_submitted(57)
            ledger.mark_confirmed(57, {"ticket": 57, "pair": "EURUSD", "profit": 4.25})

            restarted = CloseReconciliationLedger(path)
            state = SharedState()
            monitor = PortfolioMonitorAgent(
                Broker(), Settings(), execution_agent=None, state=state,
                close_ledger=restarted,
            )
            asyncio.run(monitor.refresh_from_mt5())
            asyncio.run(monitor.refresh_from_mt5())

            self.assertEqual(state.daily_pnl_usd, 4.25)
            self.assertEqual(restarted.unaccounted_confirmations(), [])


if __name__ == "__main__":
    unittest.main()