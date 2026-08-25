"""Entry rechecks must fail closed rather than paper-trading a stale plan."""

import os
import sys
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from config.settings import CryptoSettings
from domain.models import Candidate, EntryStatus, Ticker
from runtime.orchestrator import _revalidate_paper_entries


def candidate() -> Candidate:
    return Candidate(
        rank=1,
        symbol="BTC_USDT",
        planned_side="long",
        planned_quantity=1.0,
        last_price=100.0,
        contract_size=1.0,
        planned_stop_price=98.0,
        planned_take_profit_price=103.0,
        entry_status=EntryStatus.CONFIRMED,
        entry_zone_low=99.5,
        entry_zone_high=100.5,
        entry_invalidation_price=98.5,
        note="confirmed retest",
    )


def ticker(price: float) -> Ticker:
    return Ticker(
        symbol="BTC_USDT",
        last_price=price,
        bid=price - 0.01,
        ask=price + 0.01,
        spread_pct=0.02,
        volume_24h=1000.0,
        turnover_24h_usdt=1_000_000.0,
        change_pct_24h=0.0,
        fetched_at=datetime.now(timezone.utc).isoformat(),
    )


class FakeMexcClient:
    def __init__(self, latest: Ticker | None = None, fail: bool = False):
        self.latest = latest
        self.fail = fail

    def get_all_tickers(self):
        if self.fail:
            raise RuntimeError("MEXC unavailable")
        return [self.latest] if self.latest else []


class TestEntryRevalidation(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.settings = replace(CryptoSettings(), max_open_positions=5)
        self.audit_path = os.path.join(self.temp_dir.name, "audit.jsonl")

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_accepts_a_fresh_price_still_inside_confirmed_zone(self):
        setup = candidate()
        accepted = _revalidate_paper_entries(
            FakeMexcClient(ticker(100.1)),
            [setup],
            [],
            self.settings,
            self.audit_path,
        )
        self.assertIn("BTC_USDT", accepted)
        self.assertEqual(setup.entry_status, EntryStatus.CONFIRMED)
        self.assertEqual(setup.last_price, 100.1)

    def test_defers_when_price_has_left_entry_zone(self):
        setup = candidate()
        accepted = _revalidate_paper_entries(
            FakeMexcClient(ticker(101.0)),
            [setup],
            [],
            self.settings,
            self.audit_path,
        )
        self.assertEqual(accepted, {})
        self.assertEqual(setup.entry_status, EntryStatus.PENDING)
        self.assertIsNone(setup.planned_quantity)
        self.assertIn("left confirmed entry zone", setup.note)

    def test_defers_when_final_ticker_fetch_fails(self):
        setup = candidate()
        accepted = _revalidate_paper_entries(
            FakeMexcClient(fail=True),
            [setup],
            [],
            self.settings,
            self.audit_path,
        )
        self.assertEqual(accepted, {})
        self.assertEqual(setup.entry_status, EntryStatus.PENDING)
        self.assertIn("recheck unavailable", setup.note)


if __name__ == "__main__":
    unittest.main()