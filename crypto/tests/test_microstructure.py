"""Tests for conservative public order-book and recent-trade flow metrics."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from analysis.microstructure import analyze_microstructure
from domain.models import ContractDetail, OrderBook, RecentTrade


def contract() -> ContractDetail:
    return ContractDetail(
        symbol="BTC_USDT",
        display_name="BTCUSDT",
        base_coin="BTC",
        quote_coin="USDT",
        contract_size=0.1,
        volume_step=1,
        min_quantity=1,
        max_quantity=100000,
        price_precision=1,
        quantity_precision=0,
        is_active=True,
        fetched_at=None,
        contract_type=1,
        concept_plates=["crypto"],
        price_increment=0.1,
    )


class TestMicrostructure(unittest.TestCase):
    def test_calculates_depth_trade_pressure_and_large_print_proxy(self):
        book = OrderBook(
            symbol="BTC_USDT",
            bids=[(100.0, 70.0), (99.9, 30.0)],
            asks=[(100.1, 20.0), (100.2, 30.0)],
            fetched_at="2026-01-01T00:00:00+00:00",
        )
        trades = [
            RecentTrade("BTC_USDT", "buy", 100.0, 1000.0, 1),
            RecentTrade("BTC_USDT", "sell", 100.0, 10.0, 2),
        ]
        metrics = analyze_microstructure(contract(), book, trades)
        self.assertAlmostEqual(metrics.order_book_imbalance_pct, 33.33)
        self.assertAlmostEqual(metrics.buy_pressure_pct, 99.01)
        self.assertEqual(metrics.large_trade_count, 1)
        self.assertAlmostEqual(metrics.largest_trade_notional_usdt, 10000.0)

    def test_never_invents_large_trade_notional_without_contract_size(self):
        metrics = analyze_microstructure(
            None,
            OrderBook(symbol="X_USDT", bids=[(1.0, 5.0)], asks=[(1.1, 5.0)]),
            [RecentTrade("X_USDT", "buy", 1.0, 100000.0, 1)],
        )
        self.assertAlmostEqual(metrics.buy_pressure_pct, 100.0)
        self.assertEqual(metrics.order_book_imbalance_pct, 0.0)
        self.assertIsNone(metrics.large_trade_count)
        self.assertIsNone(metrics.largest_trade_notional_usdt)


if __name__ == "__main__":
    unittest.main()