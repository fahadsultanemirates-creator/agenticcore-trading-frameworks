#!/usr/bin/env python3
"""
main.py – Crypto Standard Tier 1 entry point.

Default: one-shot signal cycle.
Set CRYPTO_RUN_FOREVER=true or pass --forever for a continuous loop.

Normal operation remains signal/paper mode. A separately confirmed live canary
uses a different CLI path and a dedicated trading API key.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import traceback
from dataclasses import replace

# Ensure the package root is on the path when run directly
import os
sys.path.insert(0, os.path.dirname(__file__))

from config.settings import load_settings
from runtime.orchestrator import run_cycle
from runtime.account_check import run_readonly_account_check
from runtime.live_canary import (
    LiveCanaryError,
    execute_live_canary,
    reconcile_unconfirmed_live_entry,
    reconcile_live_profit_lock,
    run_live_canary_preflight,
)
from runtime.live_cycle import (
    execute_live_cycle,
    reconcile_live_cycle_profit_locks,
    run_live_cycle_preflight,
)
from runtime.notifications import notify_cycle, notify_startup
from runtime.preflight import run_preflight
from storage.memory import CoinMemoryStore


def record_failure_heartbeat(settings, cycle_count: int, error: Exception) -> None:
    """Leave a durable recovery signal even when a full market cycle crashes."""
    try:
        with CoinMemoryStore(settings.memory_db_path) as memory:
            memory.record_heartbeat(
                cycle_count,
                "error",
                0,
                0,
                0,
                0,
                error=str(error)[:500],
            )
            memory.write_snapshot(settings.memory_snapshot_path, settings.radar_limit)
    except Exception as memory_error:
        sys.stderr.write(
            f"[main] Could not persist failure heartbeat safely: {memory_error}\n"
        )


def candidate_diagnostics(candidates) -> str:
    """Summarize the final gates without exposing market/account credentials."""
    items = list(candidates or [])
    directional = sum(
        1 for candidate in items if getattr(candidate, "planned_side", None) in {"long", "short"}
    )
    sized = sum(1 for candidate in items if getattr(candidate, "planned_quantity", None))
    protected = sum(
        1
        for candidate in items
        if getattr(candidate, "planned_stop_price", None)
        and getattr(candidate, "planned_take_profit_price", None)
    )
    clear = sum(
        1 for candidate in items if getattr(candidate, "correlation_status", None) == "clear"
    )
    return (
        f"analyzed={len(items)} directional={directional} sized={sized} "
        f"protected={protected} correlation_clear={clear}"
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Crypto Standard Tier 1 – signal/paper mode market scanner"
    )
    parser.add_argument(
        "--forever",
        action="store_true",
        help="Run in a continuous loop (also enabled by CRYPTO_RUN_FOREVER=true)",
    )
    parser.add_argument(
        "--account-check",
        action="store_true",
        help="Run the laptop-only signed READ-ONLY MEXC account inspection, then exit.",
    )
    parser.add_argument(
        "--preflight",
        action="store_true",
        help="Verify local paths and safe configuration without making any network request.",
    )
    parser.add_argument(
        "--live-canary",
        action="store_true",
        help="Submit exactly one protected $50 / 20x isolated live canary from a qualified plan.",
    )
    parser.add_argument(
        "--live-trial",
        action="store_true",
        help="Submit one explicitly enabled broader, protected trial canary.",
    )
    parser.add_argument(
        "--live-cycle",
        action="store_true",
        help="Submit up to five protected, correlation-clear $50 / 20x isolated entries.",
    )
    parser.add_argument(
        "--live-preflight",
        action="store_true",
        help="Validate the dedicated trading key and clean-account gate without submitting an order.",
    )
    parser.add_argument(
        "--live-reconcile",
        action="store_true",
        help="Safely resolve an unconfirmed live-entry record after checking MEXC is flat.",
    )
    parser.add_argument(
        "--live-monitor",
        action="store_true",
        help="Reconcile the active live canary and apply its recorded profit lock if earned.",
    )
    parser.add_argument(
        "--confirm-live",
        action="store_true",
        help="Required alongside --live-canary; prevents accidental live submission.",
    )
    args = parser.parse_args()

    try:
        settings = load_settings(
            include_private_dotenv=(
                args.account_check or args.live_canary or args.live_cycle or args.live_monitor
                or args.live_preflight or args.live_reconcile or args.live_trial
            )
        )
    except ValueError as exc:
        sys.stderr.write(f"[main] Configuration error: {exc}\n")
        return 1

    if args.preflight:
        print(json.dumps(run_preflight(settings), indent=2, sort_keys=True))
        return 0

    if args.account_check:
        try:
            result = run_readonly_account_check(settings)
            print(
                "[main] Read-only account check complete | "
                f"assets={result['asset_count']} positions={result['position_count']} "
                f"equity={result['equity']}"
            )
            print("[main] No trading, cancellation, leverage, or withdrawal call exists.")
            return 0
        except Exception as exc:
            sys.stderr.write(f"[main] Read-only account check failed safely: {exc}\n")
            return 1

    if args.live_canary:
        if not args.confirm_live:
            sys.stderr.write(
                "[main] Live canary not submitted: add --confirm-live after "
                "reviewing the exact plan and MEXC account.\n"
            )
            return 1
    if args.live_trial:
        if not args.confirm_live:
            sys.stderr.write(
                "[main] Live trial not submitted: add --confirm-live after reviewing "
                "the broader trial profile and MEXC account.\n"
            )
            return 1
        if not settings.live_trial_canary_enabled:
            sys.stderr.write(
                "[main] Live trial is disabled. Set CRYPTO_LIVE_TRIAL_CANARY_ENABLED=true "
                "only for the supervised validation run.\n"
            )
            return 1
    if args.live_cycle and not args.confirm_live:
        sys.stderr.write(
            "[main] Live cycle not submitted: add --confirm-live after reviewing "
            "the MEXC account and the five-position maximum.\n"
        )
        return 1

    if args.live_preflight:
        try:
            result = (
                run_live_cycle_preflight(settings)
                if settings.live_cycle_enabled
                else run_live_canary_preflight(settings)
            )
            print(
                "[main] Live canary preflight | "
                f"ready={result['ready']} positions={result['open_positions']} "
                f"orders={result['open_orders']} tpsl={result['open_tpsl_plans']} "
                f"| {result['message']}"
            )
            return 0 if result["ready"] else 1
        except LiveCanaryError as exc:
            sys.stderr.write(f"[main] Live canary preflight stopped safely: {exc}\n")
            return 1

    if args.live_reconcile:
        try:
            result = reconcile_unconfirmed_live_entry(settings)
            print(
                "[main] Live entry reconciled | "
                f"status={result['status']} cleared={result['cleared']} "
                f"positions={result['open_positions']} orders={result['open_orders']} "
                f"tpsl={result['open_tpsl_plans']}"
            )
            return 0
        except LiveCanaryError as exc:
            sys.stderr.write(f"[main] Live entry reconciliation stopped safely: {exc}\n")
            return 1

    if args.live_canary:
        state = None
        try:
            state = run_cycle(settings, cycle_count=0)
            result = execute_live_canary(settings, state.candidates)
            print(
                "[main] Protected live canary confirmed | "
                f"symbol={result['symbol']} side={result['side']} "
                f"order_id={result['entry_order_id']} "
                f"estimated_margin={result['estimated_initial_margin_usdt']:.4f}"
            )
            return 0
        except LiveCanaryError as exc:
            sys.stderr.write(f"[main] Live canary stopped safely: {exc}\n")
            if state is not None:
                sys.stderr.write(
                    f"[main] Canary diagnostics | {candidate_diagnostics(state.candidates)}\n"
                )
            return 1
        except Exception as exc:
            sys.stderr.write(f"[main] Live canary failed: {exc}\n")
            return 1
    if args.live_trial:
        state = None
        try:
            trial_settings = replace(settings, trial_entry_mode=True)
            state = run_cycle(trial_settings, cycle_count=0)
            result = execute_live_canary(trial_settings, state.candidates)
            print(
                "[main] Protected live trial confirmed | "
                f"symbol={result['symbol']} side={result['side']} "
                f"order_id={result['entry_order_id']} "
                f"estimated_margin={result['estimated_initial_margin_usdt']:.4f}"
            )
            return 0
        except LiveCanaryError as exc:
            sys.stderr.write(f"[main] Live trial stopped safely: {exc}\n")
            if state is not None:
                sys.stderr.write(
                    f"[main] Trial diagnostics | {candidate_diagnostics(state.candidates)}\n"
                )
            return 1
        except Exception as exc:
            sys.stderr.write(f"[main] Live trial failed: {exc}\n")
            return 1

    if args.live_cycle:
        try:
            state = run_cycle(settings, cycle_count=0)
            result = execute_live_cycle(settings, state.candidates)
            print(
                "[main] Protected live cycle confirmed | "
                f"positions={result['protected_positions']} "
                f"requested={result['requested_positions']}"
            )
            if not args.forever:
                return 0
            while True:
                monitored = reconcile_live_cycle_profit_locks(settings)
                print(
                    "[main] Live cycle monitor | "
                    f"status={monitored.get('status')} changed={monitored.get('changed')} "
                    f"open_positions={monitored.get('open_positions')}"
                )
                if monitored.get("status") == "closed":
                    return 0
                time.sleep(settings.cycle_interval_seconds)
        except LiveCanaryError as exc:
            sys.stderr.write(f"[main] Live cycle stopped safely: {exc}\n")
            return 1
        except Exception as exc:
            sys.stderr.write(f"[main] Live cycle failed: {exc}\n")
            return 1

    if args.live_monitor:
        try:
            while True:
                result = (
                    reconcile_live_cycle_profit_locks(settings)
                    if settings.live_cycle_enabled
                    else reconcile_live_profit_lock(settings)
                )
                print(
                    "[main] Live canary monitor complete | "
                    f"status={result.get('status')} changed={result.get('changed')}"
                )
                if not args.forever or result.get("status") in {"closed", "position_not_open"}:
                    return 0
                time.sleep(settings.cycle_interval_seconds)
        except LiveCanaryError as exc:
            sys.stderr.write(f"[main] Live canary monitor stopped safely: {exc}\n")
            return 1

    run_forever = args.forever or settings.run_forever
    cycle_count = 0

    print(
        f"[main] Crypto Standard Tier 1 starting | "
        f"mode=signal | forever={run_forever} | "
        f"exchange=MEXC Futures"
    )
    notify_startup(settings, continuous=run_forever)
    consecutive_failures = 0

    while True:
        try:
            state = run_cycle(settings, cycle_count=cycle_count)
            cycle_count += 1
            consecutive_failures = 0
            candidates_summary = ", ".join(
                f"{c.symbol}(conf={c.confidence})" for c in state.candidates
            )
            print(
                f"[main] Cycle {cycle_count} complete | "
                f"market_data={state.market_data_status} | "
                f"candidates={len(state.candidates)} | {candidates_summary}"
            )
            notify_cycle(settings, state)
        except Exception as exc:
            consecutive_failures += 1
            sys.stderr.write(
                f"[main] Cycle {cycle_count} failed: {exc}\n"
                f"{traceback.format_exc()}"
            )
            record_failure_heartbeat(settings, cycle_count, exc)
            notify_cycle(settings, None, error=f"cycle {cycle_count} failed: {exc}")
            cycle_count += 1

        if not run_forever:
            break

        delay = settings.cycle_interval_seconds
        if consecutive_failures:
            delay = min(
                settings.cycle_interval_seconds * (2 ** min(consecutive_failures, 4)),
                300,
            )
        print(f"[main] Sleeping {delay}s …")
        time.sleep(delay)

    return 0


if __name__ == "__main__":
    sys.exit(main())
