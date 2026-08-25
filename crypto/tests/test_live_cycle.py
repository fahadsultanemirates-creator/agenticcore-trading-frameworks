"""Tests for the bounded multi-position live execution path."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from dataclasses import replace
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from adapters.mexc_private import OpenPosition
from config.settings import CryptoSettings
from domain.models import Candidate, EntryStatus
from runtime.live_cycle import execute_live_cycle


def _candidate(symbol: str, rank: int) -> Candidate:
    return Candidate(
        rank=rank,
        symbol=symbol,
        planned_side="long",
        planned_quantity=10.0,
        planned_margin_usdt=2.5,
        last_price=5.0,
        contract_size=1.0,
        planned_stop_price=4.8,
        planned_take_profit_price=5.3,
        profit_lock_trigger_price=5.195,
        profit_lock_stop_price=5.105,
        correlation_status="clear",
        entry_status=EntryStatus.CONFIRMED,
    )


class _Reader:
    def __init__(self, *_args, **_kwargs):
        self.position_calls = 0
        self.plan_calls = 0

    def get_open_positions(self):
        self.position_calls += 1
        if self.position_calls == 1:
            return []
        return [
            OpenPosition("1", "AAA_USDT", "1", 10.0, 5.0, 5.0, 0.0, 20, 1, 2.5, "now"),
            OpenPosition("2", "BBB_USDT", "1", 10.0, 5.0, 5.0, 0.0, 20, 1, 2.5, "now"),
        ]

    def get_open_orders(self, **_kwargs):
        return []

    def get_open_tpsl_orders(self, _symbol=None):
        self.plan_calls += 1
        if self.plan_calls == 1:
            return []
        return [
            {
                "id": "plan-a",
                "positionId": "1",
                "orderId": "entry-1",
                "state": 1,
                "stopLossPrice": 4.8,
                "takeProfitPrice": 5.3,
            },
            {
                "id": "plan-b",
                "positionId": "2",
                "orderId": "entry-2",
                "state": 1,
                "stopLossPrice": 4.8,
                "takeProfitPrice": 5.3,
            },
        ]


class _Execution:
    calls = 0

    def __init__(self, *_args, **_kwargs):
        pass

    def submit_protected_market_entry(self, **_kwargs):
        type(self).calls += 1
        number = type(self).calls
        return type("Submitted", (), {
            "order_id": f"entry-{number}",
            "external_oid": f"external-{number}",
            "submitted_at_ms": number,
        })()


class TestLiveCycle(unittest.TestCase):
    def test_submits_each_selected_position_with_confirmed_protection(self):
        _Execution.calls = 0
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ,
            {"MEXC_TRADING_API_KEY": "test-key", "MEXC_TRADING_API_SECRET": "test-secret"},
            clear=False,
        ):
            settings = replace(
                CryptoSettings(),
                live_cycle_enabled=True,
                live_cycle_max_positions=2,
                runtime_dir=directory,
            )
            with (
                patch("runtime.live_cycle.MexcPrivateClient", _Reader),
                patch("runtime.live_cycle.MexcExecutionClient", _Execution),
                patch("runtime.live_cycle.time.sleep"),
            ):
                result = execute_live_cycle(
                    settings, [_candidate("AAA_USDT", 1), _candidate("BBB_USDT", 2)]
                )

        self.assertEqual(result["requested_positions"], 2)
        self.assertEqual(result["protected_positions"], 2)
        self.assertEqual(_Execution.calls, 2)
        self.assertTrue(all(entry["status"] == "protected_open" for entry in result["entries"]))
