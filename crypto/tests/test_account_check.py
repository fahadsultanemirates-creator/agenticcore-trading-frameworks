"""Tests for the explicit read-only MEXC account check."""

from __future__ import annotations

import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from adapters.mexc_private import AccountAsset, OpenOrder
from config.settings import CryptoSettings
from runtime.account_check import run_readonly_account_check


class _ReadOnlyClient:
    """Fake client proving the check uses supported GET-only operations."""

    def __init__(self, *_args, **_kwargs):
        pass

    def get_account_assets(self):
        return [
            AccountAsset(
                currency="USDT",
                position_margin=3.5,
                available_balance=96.5,
                cash_balance=100.0,
                unrealised_pnl=0.0,
                fetched_at="2026-01-01T00:00:00+00:00",
            )
        ]

    def get_open_positions(self):
        return []

    def get_open_orders(self):
        return [
            OpenOrder(
                order_id="order-1",
                symbol="BTC_USDT",
                side=1,
                price=100000.0,
                vol=1.0,
                deal_avg_price=None,
                deal_vol=0.0,
                order_type=1,
                state=2,
                create_time=None,
                fetched_at="2026-01-01T00:00:00+00:00",
            )
        ]


class TestReadOnlyAccountCheck(unittest.TestCase):
    def test_uses_assets_positions_and_open_orders(self):
        settings = CryptoSettings(private_readonly_enabled=True)
        with (
            patch.dict(
                os.environ,
                {"MEXC_API_KEY": "test-access", "MEXC_API_SECRET": "test-secret"},
                clear=False,
            ),
            patch("runtime.account_check.MexcPrivateClient", _ReadOnlyClient),
        ):
            result = run_readonly_account_check(settings)

        self.assertEqual(result["status"], "live")
        self.assertEqual(result["asset_count"], 1)
        self.assertEqual(result["position_count"], 0)
        self.assertEqual(result["open_order_count"], 1)
        self.assertEqual(result["usdt_available"], 96.5)
        self.assertEqual(result["equity"], 100.0)
        self.assertIsNone(result["can_trade"])


if __name__ == "__main__":
    unittest.main()