import os
import sys
import time
import unittest
from dataclasses import replace

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from adapters.market_context import _cross_market_summary, _provider_status
from analysis.cross_market import apply_cross_market_confirmation
from config.settings import CryptoSettings
from domain.models import Candidate, EntryStatus


def candidate(confidence=72):
    return Candidate(
        rank=1,
        symbol="BTC_USDT",
        confidence=confidence,
        planned_side="long",
        entry_status=EntryStatus.CONFIRMED,
        planned_quantity=1.0,
        planned_stop_price=99.0,
        planned_take_profit_price=103.0,
        profit_lock_trigger_price=102.0,
        profit_lock_stop_price=101.0,
    )


class TestCrossMarketConfirmation(unittest.TestCase):
    def test_agreement_is_capped_and_cannot_exceed_100(self):
        item = candidate(98)
        result = apply_cross_market_confirmation(
            item,
            {"status": "live", "agreement": "long", "live_provider_count": 2},
            CryptoSettings(),
        )
        self.assertEqual(result.cross_market_adjustment, 6)
        self.assertEqual(result.confidence, 100)
        self.assertEqual(result.planned_side, "long")

    def test_conflict_withholds_plan_below_existing_confidence_floor(self):
        item = candidate(72)
        result = apply_cross_market_confirmation(
            item,
            {"status": "live", "agreement": "short", "live_provider_count": 2},
            CryptoSettings(),
        )
        self.assertEqual(result.confidence, 66)
        self.assertIsNone(result.planned_quantity)
        self.assertEqual(result.planned_side, "long")
        self.assertEqual(result.entry_status, EntryStatus.CONFIRMED)

    def test_cross_market_cannot_create_direction_or_plan(self):
        item = Candidate(rank=1, symbol="BTC_USDT", confidence=60)
        result = apply_cross_market_confirmation(
            item,
            {"status": "live", "agreement": "long", "live_provider_count": 2},
            CryptoSettings(),
        )
        self.assertIsNone(result.planned_side)
        self.assertIsNone(result.planned_quantity)
        self.assertEqual(result.cross_market_adjustment, 0)

    def test_provider_disagreement_is_explicit(self):
        summary = _cross_market_summary(
            "BTC_USDT",
            [
                {
                    "provider": "binance_futures",
                    "status": "live",
                    "buy_pressure_pct": 66,
                    "order_book_imbalance_pct": 20,
                },
                {
                    "provider": "bybit_linear",
                    "status": "live",
                    "buy_pressure_pct": 32,
                    "order_book_imbalance_pct": -20,
                },
            ],
        )
        self.assertEqual(summary["agreement"], "mixed")
        self.assertTrue(summary["disagreement"])
        self.assertEqual(summary["live_provider_count"], 2)

    def test_stale_or_non_directional_provider_cannot_be_live(self):
        now = int(time.time() * 1000)
        self.assertEqual(
            _provider_status(
                {
                    "buy_pressure_pct": 70,
                    "buy_pressure_timestamp_ms": now - (21 * 60 * 1000),
                    "funding_rate": 0.001,
                },
                now_ms=now,
            ),
            "unavailable",
        )
        self.assertEqual(
            _provider_status({"funding_rate": 0.001, "open_interest": 1000}, now_ms=now),
            "unavailable",
        )

    def test_unavailable_provider_cannot_influence_a_fresh_provider_vote(self):
        summary = _cross_market_summary(
            "BTC_USDT",
            [
                {
                    "provider": "binance_futures",
                    "status": "unavailable",
                    "buy_pressure_pct": 70,
                },
                {
                    "provider": "bybit_linear",
                    "status": "live",
                    "buy_pressure_pct": 50,
                },
            ],
        )
        self.assertEqual(summary["agreement"], "neutral")
        self.assertEqual(summary["aggregate_buy_pressure_pct"], 50)
        self.assertEqual(summary["live_provider_count"], 1)


if __name__ == "__main__":
    unittest.main()