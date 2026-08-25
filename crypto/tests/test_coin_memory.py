"""Regression coverage for durable per-coin intelligence and paper outcomes."""

import json
import os
import sys
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from config.settings import CryptoSettings
from domain.models import Candidate, ContractDetail, EntryStatus, Ticker
from runtime.paper_positions import update_paper_positions
from storage.memory import CoinMemoryStore


def contract(symbol: str, active: bool = True) -> ContractDetail:
    base = symbol.split("_", 1)[0]
    return ContractDetail(
        symbol=symbol,
        display_name=symbol.replace("_", ""),
        base_coin=base,
        quote_coin="USDT",
        contract_size=1.0,
        volume_step=1.0,
        min_quantity=1.0,
        max_quantity=1_000_000.0,
        price_precision=2,
        quantity_precision=0,
        is_active=active,
        fetched_at=datetime.now(timezone.utc).isoformat(),
        contract_type=1,
        concept_plates=["crypto"],
        price_increment=0.01,
    )


def ticker(symbol: str, price: float) -> Ticker:
    return Ticker(
        symbol=symbol,
        last_price=price,
        bid=price - 0.01,
        ask=price + 0.01,
        spread_pct=0.02,
        volume_24h=20_000,
        turnover_24h_usdt=20_000_000,
        change_pct_24h=4.0,
        fetched_at=datetime.now(timezone.utc).isoformat(),
    )


def candidate(symbol: str) -> Candidate:
    return Candidate(
        rank=1,
        symbol=symbol,
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
        confidence=85,
        note="completed-candle reclaim; relative volume=1.4x",
        price_action_15m_pct=1.2,
        price_action_1h_pct=2.1,
        relative_volume=1.4,
    )


class TestCoinMemory(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp_dir.name, "coin_memory.sqlite3")
        self.snapshot_path = os.path.join(self.temp_dir.name, "memory_snapshot.json")

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_retains_symbol_history_radar_and_trade_outcome_across_reopen(self):
        btc = contract("BTC_USDT")
        eth = contract("ETH_USDT")
        with CoinMemoryStore(self.db_path) as store:
            store.record_universe(
                1,
                [btc, eth],
                {"BTC_USDT": ticker("BTC_USDT", 100.0), "ETH_USDT": ticker("ETH_USDT", 50.0)},
                ["BTC_USDT"],
                ["BTC_USDT"],
                observed_at="2026-01-01T00:00:00+00:00",
            )
            stored_candidate = candidate("BTC_USDT")
            store.record_candidates(1, [stored_candidate], ["BTC_USDT"], "2026-01-01T00:01:00+00:00")
            opened = {
                "event": "paper_position_opened",
                "id": "paper_position_opened:one",
                "symbol": "BTC_USDT",
                "position": {
                    "id": "one",
                    "symbol": "BTC_USDT",
                    "side": "long",
                    "opened_at": "2026-01-01T00:02:00+00:00",
                    "entry_evidence": {"confidence": 85},
                },
            }
            closed = {
                "event": "paper_position_closed",
                "id": "paper_position_closed:one",
                "symbol": "BTC_USDT",
                "position": {
                    "id": "one",
                    "symbol": "BTC_USDT",
                    "side": "long",
                    "closed_at": "2026-01-01T00:03:00+00:00",
                    "close_reason": "target",
                    "net_pnl_usdt": 2.4,
                    "entry_evidence": {"confidence": 85},
                    "close_evidence": {"last_price": 103.0},
                },
            }
            store.record_trade_events([opened, closed])
            store.record_heartbeat(1, "healthy", 1, 1, 1, 0)
            snapshot = store.write_snapshot(self.snapshot_path, radar_limit=10)
            self.assertEqual(snapshot["summary"]["known_coins"], 2)
            self.assertEqual(snapshot["coins"]["BTC_USDT"]["profile"]["radar_state"], "selected")
            self.assertEqual(snapshot["coins"]["BTC_USDT"]["profile"]["wins"], 1)
            self.assertEqual(len(snapshot["coins"]["BTC_USDT"]["trade_events"]), 2)

        with CoinMemoryStore(self.db_path) as reopened:
            coin = reopened.get_coin("BTC_USDT")
            self.assertEqual(coin["profile"]["total_scans"], 1)
            self.assertEqual(coin["trade_events"][0]["outcome"], "win")

    def test_marks_removed_or_inactive_listing_without_erasing_history(self):
        with CoinMemoryStore(self.db_path) as store:
            store.record_universe(
                1,
                [contract("BTC_USDT"), contract("OLD_USDT")],
                {"BTC_USDT": ticker("BTC_USDT", 100.0)},
                ["BTC_USDT"],
                [],
            )
            store.record_universe(
                2,
                [contract("BTC_USDT")],
                {"BTC_USDT": ticker("BTC_USDT", 101.0)},
                ["BTC_USDT"],
                [],
            )
            old = store.get_coin("OLD_USDT")
            self.assertIsNotNone(old)
            self.assertEqual(old["profile"]["is_active"], 0)
            self.assertEqual(old["profile"]["total_scans"], 1)

    def test_paper_position_captures_before_and_after_evidence(self):
        settings = replace(
            CryptoSettings(),
            paper_trading_enabled=True,
            runtime_dir=self.temp_dir.name,
        )
        paper_path = os.path.join(self.temp_dir.name, "paper_positions.json")
        positions, _, _ = update_paper_positions(
            paper_path,
            [candidate("BTC_USDT")],
            {"BTC_USDT": ticker("BTC_USDT", 100.0)},
            settings,
            allow_new_positions=True,
        )
        self.assertEqual(positions[0]["entry_evidence"]["confidence"], 85)
        positions, _, _ = update_paper_positions(
            paper_path,
            [],
            {"BTC_USDT": ticker("BTC_USDT", 103.0)},
            settings,
        )
        self.assertEqual(positions[0]["status"], "closed")
        self.assertEqual(positions[0]["close_evidence"]["last_price"], 103.0)
        with open(self.snapshot_path, "w", encoding="utf-8") as handle:
            json.dump({"ok": True}, handle)
        self.assertTrue(os.path.exists(self.snapshot_path))


if __name__ == "__main__":
    unittest.main()