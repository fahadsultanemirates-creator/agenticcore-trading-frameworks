"""Tests for live-canary duplicate and protection reconciliation guards."""

from __future__ import annotations

import os
import json
import sys
import tempfile
import unittest
from dataclasses import replace
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from adapters.mexc_private import OpenPosition
from config.settings import CryptoSettings
from domain.models import Candidate, EntryStatus
from runtime.live_canary import (
    LiveCanaryError,
    execute_live_canary,
    reconcile_unconfirmed_live_entry,
)


def _candidate() -> Candidate:
    return Candidate(
        rank=1,
        symbol="XRP_USDT",
        planned_side="long",
        planned_quantity=335.0,
        planned_margin_usdt=2.497425,
        last_price=1.491,
        contract_size=0.1,
        planned_stop_price=1.47,
        planned_take_profit_price=1.58,
        profit_lock_trigger_price=1.5485,
        profit_lock_stop_price=1.5225,
        correlation_status="clear",
        entry_status=EntryStatus.CONFIRMED,
    )


def _position() -> OpenPosition:
    return OpenPosition(
        position_id="99",
        symbol="XRP_USDT",
        side="1",
        hold_vol=335.0,
        open_price=1.491,
        mark_price=1.492,
        unrealised_pnl=0.0,
        leverage=20,
        margin_type=1,
        margin=2.497425,
        fetched_at="2026-01-01T00:00:00+00:00",
    )


class _Reader:
    def __init__(self, *_args, **_kwargs):
        self.calls = 0
        self.tpsl_calls = 0

    def get_open_positions(self):
        self.calls += 1
        return [] if self.calls == 1 else [_position()]

    def get_open_orders(self, **_kwargs):
        return []

    def get_open_tpsl_orders(self, _symbol=None):
        self.tpsl_calls += 1
        if self.tpsl_calls == 1:
            return []
        return [{
            "id": "88",
            "positionId": "99",
            "orderId": "entry-1",
            "state": 1,
            "stopLossPrice": 1.47,
            "takeProfitPrice": 1.58,
        }]


class _Execution:
    def __init__(self, *_args, **_kwargs):
        pass

    def submit_protected_market_entry(self, **_kwargs):
        return type(
            "Submitted",
            (),
            {"order_id": "entry-1", "external_oid": "external-1", "submitted_at_ms": 1},
        )()


class TestLiveCanary(unittest.TestCase):
    def test_records_only_after_position_and_protection_confirm(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ,
            {"MEXC_TRADING_API_KEY": "test-key", "MEXC_TRADING_API_SECRET": "test-secret"},
            clear=False,
        ):
            settings = replace(
                CryptoSettings(),
                live_canary_enabled=True,
                runtime_dir=directory,
            )
            with (
                patch("runtime.live_canary.MexcPrivateClient", _Reader),
                patch("runtime.live_canary.MexcExecutionClient", _Execution),
                patch("runtime.live_canary.time.sleep"),
            ):
                result = execute_live_canary(settings, [_candidate()])

            self.assertEqual(result["status"], "protected_open")
            self.assertEqual(result["entry_order_id"], "entry-1")
            self.assertEqual(result["tpsl_plan_order_id"], "88")

    def test_rejects_existing_manual_position_before_submission(self):
        class ReaderWithPosition(_Reader):
            def get_open_positions(self):
                return [_position()]

        with patch.dict(
            os.environ,
            {"MEXC_TRADING_API_KEY": "test-key", "MEXC_TRADING_API_SECRET": "test-secret"},
            clear=False,
        ):
            settings = replace(CryptoSettings(), live_canary_enabled=True)
            with patch("runtime.live_canary.MexcPrivateClient", ReaderWithPosition):
                with self.assertRaises(LiveCanaryError):
                    execute_live_canary(settings, [_candidate()])

    def test_reconciles_unknown_entry_only_when_exchange_is_flat(self):
        class FlatReader:
            def __init__(self, *_args, **_kwargs):
                pass

            def get_open_positions(self):
                return []

            def get_open_orders(self, **_kwargs):
                return []

            def get_open_tpsl_orders(self, _symbol=None):
                return []

        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ,
            {"MEXC_TRADING_API_KEY": "test-key", "MEXC_TRADING_API_SECRET": "test-secret"},
            clear=False,
        ):
            settings = replace(
                CryptoSettings(),
                live_canary_enabled=True,
                runtime_dir=directory,
            )
            with open(os.path.join(directory, "live_canary.json"), "w", encoding="utf-8") as handle:
                json.dump({"status": "entry_submission_unknown"}, handle)
            with patch("runtime.live_canary.MexcPrivateClient", FlatReader):
                result = reconcile_unconfirmed_live_entry(settings)

            self.assertTrue(result["cleared"])
            self.assertEqual(result["status"], "entry_absent_reconciled")
            with open(os.path.join(directory, "live_canary.json"), encoding="utf-8") as handle:
                self.assertEqual(json.load(handle)["status"], "entry_absent_reconciled")

    def test_reconciliation_keeps_unknown_record_when_exchange_is_not_flat(self):
        class ReaderWithPosition:
            def __init__(self, *_args, **_kwargs):
                pass

            def get_open_positions(self):
                return [_position()]

            def get_open_orders(self, **_kwargs):
                return []

            def get_open_tpsl_orders(self, _symbol=None):
                return []

        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ,
            {"MEXC_TRADING_API_KEY": "test-key", "MEXC_TRADING_API_SECRET": "test-secret"},
            clear=False,
        ):
            settings = replace(
                CryptoSettings(),
                live_canary_enabled=True,
                runtime_dir=directory,
            )
            state_path = os.path.join(directory, "live_canary.json")
            with open(state_path, "w", encoding="utf-8") as handle:
                json.dump({"status": "entry_submission_unknown"}, handle)
            with patch("runtime.live_canary.MexcPrivateClient", ReaderWithPosition):
                with self.assertRaises(LiveCanaryError):
                    reconcile_unconfirmed_live_entry(settings)

            with open(state_path, encoding="utf-8") as handle:
                self.assertEqual(json.load(handle)["status"], "entry_submission_unknown")

if __name__ == "__main__":
    unittest.main()