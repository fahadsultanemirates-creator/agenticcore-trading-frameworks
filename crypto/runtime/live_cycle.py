"""Explicit, bounded multi-position MEXC Futures execution for Tier 1."""

from __future__ import annotations

import json
import os
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterable

from adapters.mexc_execution import MEXC_FUTURES_EXECUTION_BASE, MexcExecutionClient
from adapters.mexc_private import MexcPrivateClient, OpenPosition
from adapters.mexc_public import MexcPublicClient
from config.settings import CryptoSettings
from domain.models import Candidate
from runtime.live_canary import (
    LiveCanaryError,
    _all_open_orders,
    _find_position,
    _find_protection,
    _trading_credentials,
)
from storage.state import now_iso, write_json_atomic


def _state_path(settings: CryptoSettings) -> str:
    return os.path.join(settings.runtime_dir, "live_cycle.json")


def _write(settings: CryptoSettings, state: Dict[str, Any]) -> None:
    state["updated_at"] = now_iso()
    write_json_atomic(_state_path(settings), state)


@contextmanager
def _cycle_lock(settings: CryptoSettings):
    lock_path = f"{_state_path(settings)}.lock"
    os.makedirs(settings.runtime_dir, exist_ok=True)
    try:
        descriptor = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError as exc:
        raise LiveCanaryError(
            "A live-cycle lock already exists. Do not launch a second live cycle."
        ) from exc
    try:
        os.write(descriptor, str(os.getpid()).encode("utf-8"))
        yield
    finally:
        os.close(descriptor)
        try:
            os.remove(lock_path)
        except FileNotFoundError:
            pass


def _require_live_cycle(settings: CryptoSettings) -> None:
    if not settings.live_cycle_enabled:
        raise LiveCanaryError(
            "Live cycle is disabled. Set CRYPTO_LIVE_CYCLE_ENABLED=true only after "
            "the tested worker is installed and the canary has been reviewed."
        )
    if settings.position_notional_usdt != 50.0 or settings.leverage_max != 20:
        raise LiveCanaryError("Live cycle only permits the fixed $50 / 20x profile.")


def _eligible(candidate: Candidate) -> bool:
    return bool(
        candidate.planned_side in {"long", "short"}
        and candidate.planned_quantity is not None
        and candidate.planned_quantity > 0
        and candidate.last_price is not None
        and candidate.last_price > 0
        and candidate.planned_margin_usdt is not None
        and candidate.planned_margin_usdt <= 2.5
        and candidate.planned_stop_price is not None
        and candidate.planned_take_profit_price is not None
        and candidate.profit_lock_trigger_price is not None
        and candidate.profit_lock_stop_price is not None
        and candidate.contract_size is not None
        and candidate.contract_size > 0
        and candidate.correlation_status == "clear"
    )


def _select_candidates(
    candidates: Iterable[Candidate], maximum: int
) -> list[Candidate]:
    selected: list[Candidate] = []
    symbols: set[str] = set()
    for candidate in candidates:
        if candidate.symbol in symbols or not _eligible(candidate):
            continue
        notional = (
            float(candidate.last_price)
            * float(candidate.planned_quantity)
            * float(candidate.contract_size)
        )
        if notional > 50.0 + 1e-7:
            continue
        selected.append(candidate)
        symbols.add(candidate.symbol)
        if len(selected) == maximum:
            break
    if not selected:
        raise LiveCanaryError(
            "No fully protected, correlation-clear live plans are available."
        )
    return selected


def _reader(settings: CryptoSettings) -> MexcPrivateClient:
    access_key, secret_key = _trading_credentials()
    return MexcPrivateClient(
        access_key,
        secret_key,
        timeout=settings.request_timeout_seconds,
        base_url=MEXC_FUTURES_EXECUTION_BASE,
    )


def run_live_cycle_preflight(settings: CryptoSettings) -> Dict[str, Any]:
    """Verify the trading key and clean account without submitting an order."""
    _require_live_cycle(settings)
    reader = _reader(settings)
    positions = reader.get_open_positions()
    orders = _all_open_orders(reader)
    plans = reader.get_open_tpsl_orders()
    return {
        "ready": not positions and not orders and not plans,
        "open_positions": len(positions),
        "open_orders": len(orders),
        "open_tpsl_plans": len(plans),
        "maximum_positions": settings.live_cycle_max_positions,
        "message": (
            "Trading key verified and account is clean."
            if not positions and not orders and not plans
            else "Account is not clean; the live cycle remains blocked."
        ),
    }


def _entry_record(candidate: Candidate, external_oid: str) -> Dict[str, Any]:
    return {
        "status": "entry_pending",
        "symbol": candidate.symbol,
        "side": candidate.planned_side,
        "entry_reference_price": candidate.last_price,
        "quantity": candidate.planned_quantity,
        "contract_size": candidate.contract_size,
        "estimated_notional_usdt": (
            float(candidate.last_price)
            * float(candidate.planned_quantity)
            * float(candidate.contract_size)
        ),
        "estimated_initial_margin_usdt": candidate.planned_margin_usdt,
        "stop_loss_price": candidate.planned_stop_price,
        "take_profit_price": candidate.planned_take_profit_price,
        "profit_lock_trigger_price": candidate.profit_lock_trigger_price,
        "profit_lock_stop_price": candidate.profit_lock_stop_price,
        "entry_external_oid": external_oid,
        "profit_lock_applied": False,
    }


def execute_live_cycle(
    settings: CryptoSettings, candidates: Iterable[Candidate]
) -> Dict[str, Any]:
    """Submit up to five independently protected entries, never naked orders."""
    _require_live_cycle(settings)
    selected = _select_candidates(candidates, settings.live_cycle_max_positions)
    with _cycle_lock(settings):
        path = Path(_state_path(settings))
        if path.is_file():
            try:
                existing = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise LiveCanaryError("Live-cycle record cannot be read safely.") from exc
            if existing.get("status") not in {"closed", "emergency_closed"}:
                raise LiveCanaryError(
                    "An existing live-cycle record is still active; reconcile it first."
                )
        reader = _reader(settings)
        if reader.get_open_positions() or _all_open_orders(reader) or reader.get_open_tpsl_orders():
            raise LiveCanaryError(
                "Live cycle requires a clean account with no positions, orders, or TP/SL plans."
            )
        access_key, secret_key = _trading_credentials()
        execution = MexcExecutionClient(
            access_key,
            secret_key,
            timeout=settings.request_timeout_seconds,
            base_url=MEXC_FUTURES_EXECUTION_BASE,
        )
        state: Dict[str, Any] = {
            "status": "submitting",
            "created_at": now_iso(),
            "requested_positions": len(selected),
            "entries": [],
        }
        _write(settings, state)
        for candidate in selected:
            external_oid = f"ac-cycle-{uuid.uuid4().hex[:20]}"
            entry = _entry_record(candidate, external_oid)
            state["entries"].append(entry)
            _write(settings, state)
            try:
                submitted = execution.submit_protected_market_entry(
                    symbol=candidate.symbol,
                    side=str(candidate.planned_side),
                    quantity=float(candidate.planned_quantity),
                    reference_price=float(candidate.last_price),
                    stop_loss_price=float(candidate.planned_stop_price),
                    take_profit_price=float(candidate.planned_take_profit_price),
                    external_oid=external_oid,
                )
            except Exception:
                entry["status"] = "entry_submission_unknown"
                state["status"] = "partial_reconciliation_required"
                _write(settings, state)
                raise
            entry["status"] = "entry_submitted"
            entry["entry_order_id"] = submitted.order_id
            entry["submitted_at_ms"] = submitted.submitted_at_ms
            _write(settings, state)

            position: OpenPosition | None = None
            protection: Dict[str, Any] | None = None
            for _ in range(5):
                time.sleep(1)
                position = _find_position(
                    reader.get_open_positions(), candidate.symbol, str(candidate.planned_side)
                )
                if position is not None:
                    protection = _find_protection(
                        reader.get_open_tpsl_orders(candidate.symbol),
                        position,
                        submitted.order_id,
                        float(candidate.planned_stop_price),
                        float(candidate.planned_take_profit_price),
                    )
                    if protection is not None:
                        break
            if position is None:
                entry["status"] = "entry_reconciliation_pending"
                state["status"] = "partial_reconciliation_required"
                _write(settings, state)
                raise LiveCanaryError(
                    f"{candidate.symbol} was submitted but its position was not confirmed. "
                    "Do not launch another cycle; inspect MEXC immediately."
                )
            if protection is None:
                entry["status"] = "emergency_close_pending"
                entry["position_id"] = position.position_id
                state["status"] = "partial_reconciliation_required"
                _write(settings, state)
                if not position.position_id:
                    entry["status"] = "manual_action_required"
                    _write(settings, state)
                    raise LiveCanaryError(
                        f"{candidate.symbol} has no exchange position ID; close it manually immediately."
                    )
                emergency = execution.close_market_position(
                    symbol=position.symbol,
                    position_id=str(position.position_id),
                    side=str(candidate.planned_side),
                    quantity=position.hold_vol or float(candidate.planned_quantity),
                    reference_price=position.mark_price or float(candidate.last_price),
                    external_oid=f"ac-cycle-panic-{uuid.uuid4().hex[:20]}",
                )
                entry["emergency_close_order_id"] = emergency.order_id
                for _ in range(5):
                    time.sleep(1)
                    if _find_position(
                        reader.get_open_positions(),
                        candidate.symbol,
                        str(candidate.planned_side),
                    ) is None:
                        entry["status"] = "emergency_closed"
                        _write(settings, state)
                        break
                if entry["status"] != "emergency_closed":
                    entry["status"] = "manual_action_required"
                    _write(settings, state)
                    raise LiveCanaryError(
                        f"{candidate.symbol} protection was not confirmed and emergency close "
                        "is not confirmed. Inspect MEXC immediately."
                    )
                raise LiveCanaryError(
                    f"{candidate.symbol} protection was not confirmed; the position was emergency-closed."
                )
            entry.update(
                {
                    "status": "protected_open",
                    "position_id": position.position_id,
                    "tpsl_plan_order_id": str(protection.get("id") or ""),
                    "opened_at": now_iso(),
                }
            )
            _write(settings, state)
        state["status"] = "protected_open"
        _write(settings, state)
        return {
            "requested_positions": len(selected),
            "protected_positions": len(state["entries"]),
            "entries": state["entries"],
        }


def reconcile_live_cycle_profit_locks(settings: CryptoSettings) -> Dict[str, Any]:
    """Apply each recorded 65% profit lock without altering targets."""
    _require_live_cycle(settings)
    with _cycle_lock(settings):
        path = Path(_state_path(settings))
        if not path.is_file():
            raise LiveCanaryError("No active live-cycle record exists.")
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise LiveCanaryError("Live-cycle record cannot be read safely.") from exc
        entries = [item for item in state.get("entries", []) if isinstance(item, dict)]
        if not entries:
            raise LiveCanaryError("Live-cycle record has no entries.")
        reader = _reader(settings)
        positions = reader.get_open_positions()
        tickers = {
            ticker.symbol: ticker
            for ticker in MexcPublicClient(timeout=settings.request_timeout_seconds).get_all_tickers()
        }
        access_key, secret_key = _trading_credentials()
        execution = MexcExecutionClient(
            access_key,
            secret_key,
            timeout=settings.request_timeout_seconds,
            base_url=MEXC_FUTURES_EXECUTION_BASE,
        )
        active: list[tuple[Dict[str, Any], OpenPosition]] = []
        for entry in entries:
            if entry.get("status") != "protected_open":
                continue
            position = _find_position(
                positions, str(entry.get("symbol")), str(entry.get("side"))
            )
            if position is None:
                entry["status"] = "closed"
                entry["reconciled_at"] = now_iso()
            else:
                active.append((entry, position))

        # The paper engine's fixed basket target also applies to live positions.
        # Never infer a total when MEXC has not supplied every position PnL.
        if active and all(position.unrealised_pnl is not None for _, position in active):
            basket_pnl = sum(float(position.unrealised_pnl) for _, position in active)
            if basket_pnl >= settings.basket_profit_target_usdt:
                state["status"] = "basket_close_submitted"
                for entry, position in active:
                    closed = execution.close_market_position(
                        symbol=position.symbol,
                        position_id=str(position.position_id),
                        side=str(entry["side"]),
                        quantity=position.hold_vol or float(entry["quantity"]),
                        reference_price=position.mark_price or float(entry["entry_reference_price"]),
                        external_oid=f"ac-basket-{uuid.uuid4().hex[:20]}",
                    )
                    entry["status"] = "basket_close_submitted"
                    entry["basket_close_order_id"] = closed.order_id
                _write(settings, state)
                for _ in range(5):
                    time.sleep(1)
                    remaining = reader.get_open_positions()
                    if not any(
                        _find_position(remaining, str(entry["symbol"]), str(entry["side"]))
                        for entry, _ in active
                    ):
                        for entry, _ in active:
                            entry["status"] = "closed"
                            entry["closed_reason"] = "basket_profit_target"
                        state["status"] = "closed"
                        _write(settings, state)
                        return {
                            "status": "closed",
                            "changed": len(active),
                            "open_positions": 0,
                            "basket_closed": True,
                        }
                state["status"] = "partial_reconciliation_required"
                _write(settings, state)
                raise LiveCanaryError(
                    "Basket close was submitted but is not confirmed flat. Inspect MEXC immediately."
                )

        changed = 0
        open_count = 0
        for entry, position in active:
            symbol, side = str(entry.get("symbol")), str(entry.get("side"))
            open_count += 1
            if entry.get("profit_lock_applied"):
                continue
            ticker = tickers.get(symbol)
            if ticker is None or ticker.last_price is None:
                continue
            reached = (
                ticker.last_price >= float(entry["profit_lock_trigger_price"])
                if side == "long"
                else ticker.last_price <= float(entry["profit_lock_trigger_price"])
            )
            if not reached:
                continue
            execution.change_tpsl_plan(
                stop_plan_order_id=str(entry["tpsl_plan_order_id"]),
                stop_loss_price=float(entry["profit_lock_stop_price"]),
                take_profit_price=float(entry["take_profit_price"]),
            )
            protection = _find_protection(
                reader.get_open_tpsl_orders(symbol),
                position,
                str(entry.get("entry_order_id") or ""),
                float(entry["profit_lock_stop_price"]),
                float(entry["take_profit_price"]),
            )
            if protection is None:
                entry["status"] = "profit_lock_reconciliation_pending"
                state["status"] = "partial_reconciliation_required"
                _write(settings, state)
                raise LiveCanaryError(
                    f"{symbol} profit-lock update was sent but not confirmed. Inspect MEXC "
                    "before any further action."
                )
            entry["profit_lock_applied"] = True
            entry["profit_lock_applied_at"] = now_iso()
            changed += 1
        state["status"] = "protected_open" if open_count else "closed"
        _write(settings, state)
        return {"status": state["status"], "changed": changed, "open_positions": open_count}