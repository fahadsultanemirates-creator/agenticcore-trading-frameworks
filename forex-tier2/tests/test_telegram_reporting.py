import asyncio
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from agents.portfolio_monitor import PortfolioMonitorAgent
from agents.reporter import DUBAI, ReportingAgent
from agents.trade_execution import TradeExecutionAgent
from core.state import SharedState


class Settings:
    reporting = {"daily_summary_time": "23:45"}

    def get(self, key, default=None):
        return default


class Broker:
    def __init__(self):
        self.position_reads = [[{
            "ticket": 42, "pair": "EURUSD", "direction": "BUY",
            "profit": 1.25,
        }], []]

    def get_account_info(self):
        return {"balance": 1000, "equity": 1000}

    def get_positions(self):
        return self.position_reads.pop(0) if self.position_reads else []

    def get_closed_deal(self, ticket):
        if not hasattr(self, "history_reads"):
            self.history_reads = 0
        self.history_reads += 1
        if self.history_reads == 1:
            return {}
        return {"ticket": ticket, "pair": "EURUSD", "profit": 6.5}


class ReporterSpy:
    def __init__(self):
        self.records = []

    async def log_trade(self, record, _summary, idempotency_key=""):
        self.records.append(record)

    async def check_and_send_reports(self, _state):
        return None


class ImmediateCloseBroker:
    def get_account_info(self):
        return {"balance": 1000, "equity": 1000}

    def get_positions(self):
        return []

    def get_closed_deal(self, ticket):
        return {"ticket": ticket, "pair": "EURUSD", "profit": 4.0}


class FrameworkCloseBroker:
    def close_position(self, _ticket):
        return {
            "success": True, "pair": "EURUSD", "direction": "BUY", "profit": 99.0,
        }


class ExecutionSettings:
    broker = {}


class ProfitLockSettings(Settings):
    def get(self, key, default=None):
        if key == "trailing_stop":
            return {
                "enabled": True,
                "activation_at_pct": 65,
                "lock_profit_at_pct": 35,
            }
        return default


class TickBridge:
    def __init__(self, tick):
        self.tick = tick

    def get_tick(self, _pair):
        return self.tick


class ExecutionSpy:
    def __init__(self):
        self.modifications = []

    async def modify_position(self, ticket, sl, tp):
        self.modifications.append((ticket, sl, tp))
        return {"success": True}


class TelegramReportingTests(unittest.TestCase):
    def test_daily_summary_waits_until_2345_dubai_and_sends_once(self):
        sent = []

        async def notify(message):
            sent.append(message)

        reporter = ReportingAgent(Settings(), notify)
        with tempfile.TemporaryDirectory() as temp_dir, patch(
            "agents.reporter.LOG_PATH", Path(temp_dir) / "trades.jsonl"
        ):
            reporter._now = lambda: datetime(2026, 8, 20, 23, 44, tzinfo=DUBAI)
            asyncio.run(reporter.check_and_send_reports(SharedState()))
            reporter._now = lambda: datetime(2026, 8, 20, 23, 45, tzinfo=DUBAI)
            asyncio.run(reporter.check_and_send_reports(SharedState()))
            asyncio.run(reporter.check_and_send_reports(SharedState()))

        self.assertEqual(len(sent), 1)
        self.assertIn("Daily (20 Aug 2026)", sent[0])

    def test_monitor_reports_broker_closed_position_once(self):
        notifications = []

        async def notify(message):
            notifications.append(message)

        state = SharedState()
        reporter = ReporterSpy()
        monitor = PortfolioMonitorAgent(
            Broker(), Settings(), execution_agent=None, state=state,
            notify_fn=notify, reporter=reporter,
        )
        asyncio.run(monitor.check_once())
        asyncio.run(monitor.check_once())
        asyncio.run(monitor.check_once())

        self.assertEqual(state.daily_pnl_usd, 6.5)
        self.assertEqual(len(reporter.records), 1)
        self.assertEqual(len(notifications), 2)
        self.assertIn("MT5 Position Closed", notifications[0])
        self.assertIn("MT5 Close Confirmed", notifications[1])

    def test_concurrent_daily_checks_send_only_one_summary(self):
        sent = []

        async def notify(message):
            sent.append(message)

        reporter = ReportingAgent(Settings(), notify)
        reporter._now = lambda: datetime(2026, 8, 20, 23, 45, tzinfo=DUBAI)

        async def run_checks():
            await asyncio.gather(
                reporter.check_and_send_reports(SharedState()),
                reporter.check_and_send_reports(SharedState()),
            )

        asyncio.run(run_checks())
        self.assertEqual(len(sent), 1)

    def test_concurrent_refreshes_confirm_a_close_once(self):
        notifications = []

        async def notify(message):
            notifications.append(message)

        state = SharedState()
        state.open_positions = [{"ticket": 7, "pair": "EURUSD", "direction": "BUY"}]
        reporter = ReporterSpy()
        monitor = PortfolioMonitorAgent(
            ImmediateCloseBroker(), Settings(), execution_agent=None, state=state,
            notify_fn=notify, reporter=reporter,
        )

        async def refresh_twice():
            await asyncio.gather(monitor.refresh_from_mt5(), monitor.refresh_from_mt5())

        asyncio.run(refresh_twice())
        self.assertEqual(state.daily_pnl_usd, 4.0)
        self.assertEqual(len(reporter.records), 1)
        self.assertEqual(len(notifications), 1)

    def test_framework_close_waits_for_broker_confirmed_pnl(self):
        notifications = []

        async def notify(message):
            notifications.append(message)

        state = SharedState()
        state.open_positions = [{"ticket": 9, "pair": "EURUSD", "direction": "BUY"}]
        execution = TradeExecutionAgent(FrameworkCloseBroker(), ExecutionSettings(), notify)
        asyncio.run(execution.close_position(9, state))

        self.assertEqual(state.daily_pnl_usd, 0)
        self.assertIn(9, state.pending_broker_closures)
        self.assertIn("Close Request Accepted", notifications[0])

        reporter = ReporterSpy()
        monitor = PortfolioMonitorAgent(
            ImmediateCloseBroker(), Settings(), execution, state,
            notify_fn=notify, reporter=reporter,
        )
        asyncio.run(monitor.refresh_from_mt5())
        self.assertEqual(state.daily_pnl_usd, 4.0)
        self.assertEqual(len(reporter.records), 1)
        self.assertIn("MT5 Close Confirmed", notifications[-1])

    def test_profit_lock_is_65_percent_activation_and_35_percent_protection(self):
        buy_execution = ExecutionSpy()
        buy_monitor = PortfolioMonitorAgent(
            TickBridge({"bid": 1.0065, "ask": 1.0066}), ProfitLockSettings(),
            buy_execution, SharedState(),
        )
        asyncio.run(buy_monitor._apply_trailing_stop({
            "ticket": 1, "pair": "EURUSD", "direction": "BUY",
            "open_price": 1.0000, "sl": 0.9900, "tp": 1.0100,
        }))
        self.assertEqual(buy_execution.modifications, [(1, 1.0035, 1.01)])

        sell_execution = ExecutionSpy()
        sell_monitor = PortfolioMonitorAgent(
            TickBridge({"bid": 0.9934, "ask": 0.9935}), ProfitLockSettings(),
            sell_execution, SharedState(),
        )
        asyncio.run(sell_monitor._apply_trailing_stop({
            "ticket": 2, "pair": "EURUSD", "direction": "SELL",
            "open_price": 1.0000, "sl": 1.0100, "tp": 0.9900,
        }))
        self.assertEqual(sell_execution.modifications, [(2, 0.9965, 0.99)])