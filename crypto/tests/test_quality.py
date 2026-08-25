"""Tests for completed-candle quality calculations and correlation alignment."""

import os
import sys
import unittest
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from analysis.quality import (
    EntrySetup,
    PriceZone,
    evaluate_entry_setup,
    failed_breakout,
    return_correlation,
    structure_zones,
)
from config.settings import CryptoSettings
from domain.models import Candle, Candidate, MarketSnapshot
from runtime.orchestrator import _apply_portfolio_controls


def candles(start: int, direction: int) -> list[Candle]:
    result = []
    price = 100.0
    for index in range(8):
        price += direction * (1 + index * 0.1)
        result.append(
            Candle(
                symbol="TEST_USDT",
                interval="Min15",
                open_time=start + index * 900_000,
                open=price - 0.1,
                high=price + 0.2,
                low=price - 0.3,
                close=price,
                volume=1000,
                is_complete=True,
            )
        )
    return result


class TestReturnCorrelation(unittest.TestCase):
    def test_requires_time_aligned_returns(self):
        self.assertIsNone(return_correlation(candles(0, 1), candles(120_000, 1)))

    def test_aligned_series_returns_correlation(self):
        correlation = return_correlation(candles(0, 1), candles(0, 1))
        self.assertIsNotNone(correlation)
        self.assertGreater(correlation, 0.99)

    def test_existing_same_side_paper_exposure_blocks_correlated_plan(self):
        candidate = Candidate(
            rank=1,
            symbol="ETH_USDT",
            planned_side="long",
            planned_quantity=1.0,
            planned_margin_usdt=2.5,
        )
        snapshots = [
            MarketSnapshot("ETH_USDT", None, None, candles=candles(0, 1)),
            MarketSnapshot("BTC_USDT", None, None, candles=candles(0, 1)),
        ]
        _apply_portfolio_controls(
            [candidate],
            snapshots,
            CryptoSettings(),
            existing_positions=[{"symbol": "BTC_USDT", "side": "long", "status": "open"}],
        )
        self.assertIsNone(candidate.planned_quantity)
        self.assertEqual(candidate.correlation_status, "blocked")


class TestStructureZones(unittest.TestCase):
    def _range_candles(self) -> list[Candle]:
        closes = [101.0, 100.2, 101.0, 102.0, 100.1, 101.0, 102.0, 100.2, 101.0, 102.0, 101.5]
        return [
            Candle(
                symbol="TEST_USDT",
                interval="Min15",
                open_time=1_000_000_000 + index * 900_000,
                open=close - 0.1,
                high=close + 0.2,
                low=close - 0.2,
                close=close,
                volume=1_000,
                is_complete=True,
            )
            for index, close in enumerate(closes)
        ]

    def test_multi_touch_support_and_resistance_zones_surround_price(self):
        support, resistance = structure_zones(
            self._range_candles(), 100.3, lookback=20, zone_width_pct=0.004, minimum_touches=2
        )
        self.assertIsNotNone(support)
        self.assertIsNotNone(resistance)
        assert support is not None and resistance is not None
        self.assertLess(support.high, 100.4)
        self.assertGreater(resistance.low, 101.5)
        self.assertGreaterEqual(support.touches, 2)
        self.assertGreaterEqual(resistance.touches, 2)

    def test_failed_long_breakout_and_short_breakdown_are_rejected(self):
        long_bars = self._range_candles()
        long_bars[-1].high = 104.0
        long_bars[-1].close = 103.0
        short_bars = self._range_candles()
        short_bars[-1].low = 98.0
        short_bars[-1].close = 100.0
        support = PriceZone(low=99.5, high=100.5, touches=3, timeframe="Min15", structure_id="support")
        resistance = PriceZone(low=102.5, high=103.5, touches=3, timeframe="Min15", structure_id="resistance")
        self.assertTrue(failed_breakout(long_bars, "long", support, resistance))
        self.assertTrue(failed_breakout(short_bars, "short", support, resistance))

    def test_pending_setup_expires_without_a_retest(self):
        bars = self._range_candles()
        for index, bar in enumerate(bars):
            bar.open_time = 1_000_000_000 + index * 900_000
        zone = PriceZone(low=99.5, high=100.5, touches=3, timeframe="Min15", structure_id="support")
        setup = evaluate_entry_setup(
            bars,
            "long",
            zone,
            current_price=101.0,
            proximity_pct=0.004,
            expiry_candles=2,
        )
        self.assertEqual(setup.status, "expired")
        self.assertIsNotNone(setup.invalidation_price)
        self.assertIsNotNone(setup.expires_at)

    def test_short_zone_retest_requires_a_bearish_reclaim(self):
        bars = self._range_candles()
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        for index, bar in enumerate(bars):
            bar.open_time = now_ms - (len(bars) - index - 1) * 900_000
        bars[-1].open = 102.1
        bars[-1].high = 102.3
        bars[-1].low = 101.3
        bars[-1].close = 101.4
        zone = PriceZone(low=101.5, high=102.5, touches=3, timeframe="Min15", structure_id="resistance")
        setup = evaluate_entry_setup(
            bars,
            "short",
            zone,
            current_price=101.6,
            proximity_pct=0.004,
            expiry_candles=4,
        )
        self.assertEqual(setup.status, "confirmed")
        self.assertGreater(setup.invalidation_price or 0, zone.high)


if __name__ == "__main__":
    unittest.main()