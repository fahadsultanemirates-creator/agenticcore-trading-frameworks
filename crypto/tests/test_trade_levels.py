"""Tests for coin-specific stops, fixed targets, and margin estimates."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from domain.models import Candle, ContractDetail
from risk.sizing import SizingError
from risk.trade_levels import (
    calculate_estimated_isolated_margin,
    calculate_target_price,
    derive_volatility_stop_price,
    quantize_price,
)


def _contract() -> ContractDetail:
    return ContractDetail(
        symbol="TEST_USDT",
        display_name="TEST_USDT",
        base_coin="TEST",
        quote_coin="USDT",
        contract_size=1.0,
        volume_step=1.0,
        min_quantity=1.0,
        max_quantity=100000.0,
        price_precision=4,
        quantity_precision=0,
        is_active=True,
        fetched_at="2024-01-01T00:00:00+00:00",
        price_increment=0.1,
    )


def _candles(range_size: float) -> list[Candle]:
    return [
        Candle(
            symbol="TEST_USDT",
            interval="Min15",
            open_time=index,
            open=100.0,
            high=100.0 + range_size / 2,
            low=100.0 - range_size / 2,
            close=100.0,
            volume=1.0,
            is_complete=True,
        )
        for index in range(5)
    ]


class TestTradeLevels(unittest.TestCase):
    def test_stop_is_capped_at_maximum_percent_room(self):
        # ATR 5 * 1.5 would be 7.5%; cap at the configured 2%.
        stop = derive_volatility_stop_price(
            "long", 100.0, _candles(5.0), 3, 1.5, 0.002, 0.02
        )
        self.assertAlmostEqual(stop, 98.0)

    def test_stop_honors_minimum_percent_room(self):
        # ATR 0.04 * 1.5 is smaller than the configured 0.2% floor.
        stop = derive_volatility_stop_price(
            "short", 100.0, _candles(0.04), 3, 1.5, 0.002, 0.02
        )
        self.assertAlmostEqual(stop, 100.2)

    def test_fixed_three_usdt_target_and_margin(self):
        contract = _contract()
        self.assertAlmostEqual(
            calculate_target_price("long", 100.0, 1.0, contract, 3.0),
            103.0,
        )
        self.assertAlmostEqual(
            calculate_target_price("short", 100.0, 1.0, contract, 3.0),
            97.0,
        )
        self.assertAlmostEqual(
            calculate_estimated_isolated_margin(100.0, 1.0, contract, 20),
            5.0,
        )

    def test_tick_rounding_is_conservative_for_risk_and_target(self):
        contract = _contract()
        # Long stop rounds toward entry; long target rounds away from entry.
        self.assertAlmostEqual(quantize_price(98.05, contract, "up"), 98.1)
        self.assertAlmostEqual(quantize_price(103.01, contract, "up"), 103.1)
        # Short stop rounds toward entry; short target rounds away from entry.
        self.assertAlmostEqual(quantize_price(101.95, contract, "down"), 101.9)
        self.assertAlmostEqual(quantize_price(96.99, contract, "down"), 96.9)

    def test_missing_exchange_tick_fails_closed(self):
        contract = _contract()
        contract.price_increment = None
        with self.assertRaises(SizingError):
            quantize_price(100.01, contract, "up")


if __name__ == "__main__":
    unittest.main()