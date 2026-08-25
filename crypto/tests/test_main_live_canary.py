"""Regression tests for the explicit live-canary CLI path."""

from __future__ import annotations

import os
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import main as crypto_main
from config.settings import CryptoSettings


class TestLiveCanaryCommand(unittest.TestCase):
    def test_confirmed_canary_runs_execution_path(self) -> None:
        settings = object()
        state = SimpleNamespace(candidates=["qualified-candidate"])
        result = {
            "symbol": "TEST_USDT",
            "side": "open_long",
            "entry_order_id": "entry-123",
            "estimated_initial_margin_usdt": 2.5,
        }

        with (
            patch.object(sys, "argv", ["main.py", "--live-canary", "--confirm-live"]),
            patch.object(crypto_main, "load_settings", return_value=settings) as load_settings,
            patch.object(crypto_main, "run_cycle", return_value=state) as run_cycle,
            patch.object(
                crypto_main, "execute_live_canary", return_value=result
            ) as execute_live_canary,
        ):
            exit_code = crypto_main.main()

        self.assertEqual(exit_code, 0)
        load_settings.assert_called_once_with(include_private_dotenv=True)
        run_cycle.assert_called_once_with(settings, cycle_count=0)
        execute_live_canary.assert_called_once_with(settings, state.candidates)

    def test_confirmed_live_cycle_runs_batch_execution_path(self) -> None:
        settings = SimpleNamespace(live_cycle_enabled=True)
        state = SimpleNamespace(candidates=["qualified-candidate"])
        with (
            patch.object(sys, "argv", ["main.py", "--live-cycle", "--confirm-live"]),
            patch.object(crypto_main, "load_settings", return_value=settings),
            patch.object(crypto_main, "run_cycle", return_value=state) as run_cycle,
            patch.object(
                crypto_main,
                "execute_live_cycle",
                return_value={"protected_positions": 2, "requested_positions": 2},
            ) as execute_live_cycle,
        ):
            exit_code = crypto_main.main()

        self.assertEqual(exit_code, 0)
        run_cycle.assert_called_once_with(settings, cycle_count=0)
        execute_live_cycle.assert_called_once_with(settings, state.candidates)

    def test_confirmed_live_trial_uses_broader_explicit_execution_path(self) -> None:
        settings = CryptoSettings(live_trial_canary_enabled=True)
        state = SimpleNamespace(candidates=["trial-candidate"])
        result = {
            "symbol": "TRIAL_USDT",
            "side": "open_long",
            "entry_order_id": "trial-entry-123",
            "estimated_initial_margin_usdt": 2.5,
        }
        with (
            patch.object(sys, "argv", ["main.py", "--live-trial", "--confirm-live"]),
            patch.object(crypto_main, "load_settings", return_value=settings),
            patch.object(crypto_main, "run_cycle", return_value=state) as run_cycle,
            patch.object(
                crypto_main, "execute_live_canary", return_value=result
            ) as execute_live_canary,
        ):
            exit_code = crypto_main.main()

        self.assertEqual(exit_code, 0)
        trial_settings = run_cycle.call_args.args[0]
        self.assertTrue(trial_settings.trial_entry_mode)
        execute_live_canary.assert_called_once_with(trial_settings, state.candidates)

    def test_live_reconcile_uses_private_settings_and_never_runs_market_scan(self) -> None:
        settings = object()
        result = {
            "status": "entry_absent_reconciled",
            "cleared": True,
            "open_positions": 0,
            "open_orders": 0,
            "open_tpsl_plans": 0,
        }
        with (
            patch.object(sys, "argv", ["main.py", "--live-reconcile"]),
            patch.object(crypto_main, "load_settings", return_value=settings) as load_settings,
            patch.object(
                crypto_main, "reconcile_unconfirmed_live_entry", return_value=result
            ) as reconcile,
            patch.object(crypto_main, "run_cycle") as run_cycle,
        ):
            exit_code = crypto_main.main()

        self.assertEqual(exit_code, 0)
        load_settings.assert_called_once_with(include_private_dotenv=True)
        reconcile.assert_called_once_with(settings)
        run_cycle.assert_not_called()
