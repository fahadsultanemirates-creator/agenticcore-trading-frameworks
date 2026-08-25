"""Regression tests for local-only paper position lifecycle management."""

import os
import sys
import tempfile
import unittest
import json
from dataclasses import replace
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from config.settings import CryptoSettings
from domain.models import Candidate, EntryStatus, Ticker
from runtime.paper_positions import daily_realized_pnl, update_paper_positions


def ticker(price: float) -> Ticker:
    return Ticker(
        symbol="BTC_USDT",
        last_price=price,
        bid=price - 0.01,
        ask=price + 0.01,
        spread_pct=0.02,
        volume_24h=1_000_000,
        turnover_24h_usdt=20_000_000,
        change_pct_24h=1.0,
        fetched_at=datetime.now(timezone.utc).isoformat(),
    )


def candidate() -> Candidate:
    return Candidate(
        rank=1,
        symbol="BTC_USDT",
        planned_side="long",
        planned_quantity=1.0,
        planned_margin_usdt=2.5,
        last_price=100.0,
        contract_size=1.0,
        planned_stop_price=98.0,
        planned_take_profit_price=103.0,
        profit_lock_trigger_price=101.95,
        profit_lock_stop_price=101.05,
        correlation_status="clear",
        entry_status=EntryStatus.CONFIRMED,
    )


class TestPaperPositions(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.temp_dir.name, "paper_positions.json")
        self.settings = replace(
            CryptoSettings(),
            paper_trading_enabled=True,
            runtime_dir=self.temp_dir.name,
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_opens_only_local_paper_position(self):
        positions, summary, events = update_paper_positions(
            self.path, [candidate()], {"BTC_USDT": ticker(100.0)}, self.settings, allow_new_positions=True
        )
        self.assertEqual(len(positions), 1)
        self.assertEqual(positions[0]["status"], "open")
        self.assertEqual(summary["open_count"], 1)
        self.assertEqual(events[0]["event"], "paper_position_opened")

    def test_profit_lock_moves_stop_then_closes(self):
        update_paper_positions(
            self.path, [candidate()], {"BTC_USDT": ticker(100.0)}, self.settings, allow_new_positions=True
        )
        positions, _, events = update_paper_positions(
            self.path, [], {"BTC_USDT": ticker(102.0)}, self.settings
        )
        self.assertTrue(positions[0]["profit_lock_applied"])
        self.assertEqual(positions[0]["stop_price"], 101.05)
        self.assertTrue(any(event["event"] == "paper_profit_lock" for event in events))

        positions, summary, _ = update_paper_positions(
            self.path, [], {"BTC_USDT": ticker(101.0)}, self.settings
        )
        self.assertEqual(positions[0]["status"], "closed")
        self.assertEqual(positions[0]["close_reason"], "profit_lock")
        self.assertEqual(summary["open_count"], 0)
        self.assertGreater(positions[0]["net_pnl_usdt"], 0)

    def test_disabled_mode_never_creates_position(self):
        disabled = replace(self.settings, paper_trading_enabled=False)
        positions, summary, events = update_paper_positions(
            self.path, [candidate()], {"BTC_USDT": ticker(100.0)}, disabled, allow_new_positions=True
        )
        self.assertEqual(positions, [])
        self.assertFalse(summary["enabled"])
        self.assertEqual(events, [])

    def test_pending_entry_never_opens_a_paper_position(self):
        pending = candidate()
        pending.entry_status = EntryStatus.PENDING
        positions, _, events = update_paper_positions(
            self.path, [pending], {"BTC_USDT": ticker(100.0)}, self.settings, allow_new_positions=True
        )
        self.assertEqual(positions, [])
        self.assertEqual(events, [])

    def test_daily_pnl_excludes_closed_records_from_prior_days(self):
        positions = [
            {
                "status": "closed",
                "closed_at": datetime.now(timezone.utc).isoformat(),
                "net_pnl_usdt": -21.0,
            },
            {
                "status": "closed",
                "closed_at": "2020-01-01T00:00:00+00:00",
                "net_pnl_usdt": -999.0,
            },
        ]
        self.assertEqual(daily_realized_pnl(positions), -21.0)

    def _write_open_basket(self, positions):
        with open(self.path, "w", encoding="utf-8") as file:
            json.dump({"positions": positions, "notification_outbox": []}, file)

    @staticmethod
    def _open_position(symbol, entry_price):
        return {
            "id": symbol,
            "symbol": symbol,
            "side": "long",
            "status": "open",
            "entry_price": entry_price,
            "quantity": 1.0,
            "contract_size": 1.0,
            "stop_price": entry_price - 10,
            "take_profit_price": entry_price + 10,
            "profit_lock_trigger_price": entry_price + 7,
            "profit_lock_stop_price": entry_price + 3,
            "profit_lock_applied": False,
            "entry_fee_usdt": 0.0,
        }

    def test_basket_stays_open_below_five_dollars(self):
        self._write_open_basket([self._open_position("BTC_USDT", 100.0)])
        positions, summary, events = update_paper_positions(
            self.path,
            [],
            {"BTC_USDT": ticker(104.0)},
            self.settings,
        )
        self.assertEqual(positions[0]["status"], "open")
        self.assertFalse(summary["basket_close_triggered"])
        self.assertLess(summary["basket_marked_net_pnl_usdt"], 5.0)
        self.assertEqual(events, [])

    def test_five_dollar_basket_closes_every_open_position(self):
        self._write_open_basket(
            [
                self._open_position("BTC_USDT", 100.0),
                self._open_position("ETH_USDT", 200.0),
            ]
        )
        positions, summary, events = update_paper_positions(
            self.path,
            [],
            {"BTC_USDT": ticker(102.5), "ETH_USDT": ticker(202.7)},
            self.settings,
        )
        self.assertTrue(summary["basket_close_triggered"])
        self.assertGreaterEqual(summary["basket_marked_net_pnl_usdt"], 5.0)
        self.assertEqual(summary["open_count"], 0)
        self.assertEqual(len([p for p in positions if p["status"] == "closed"]), 2)
        self.assertEqual(
            {position["close_reason"] for position in positions},
            {"basket_profit_target"},
        )
        self.assertEqual(
            len([event for event in events if event["event"] == "paper_position_closed"]),
            2,
        )