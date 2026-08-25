"""Explicit, single-position live-canary execution and protection reconciliation."""

from __future__ import annotations

import json
import os
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

from adapters.mexc_execution import MEXC_FUTURES_EXECUTION_BASE, MexcExecutionClient
from adapters.mexc_private import MexcPrivateClient, MexcPrivateError, OpenPosition
from adapters.mexc_public import MexcPublicClient
from config.settings import CryptoSettings
from domain.models import Candidate
from storage.state import now_iso, write_json_atomic


class LiveCanaryError(RuntimeError):
    """Raised when a live canary cannot be safely submitted or reconciled."""


def _state_path(settings: CryptoSettings) -> str:
    return os.path.join(settings.runtime_dir, "live_canary.json")


def _load_state(settings: CryptoSettings) -> Dict[str, Any]:
    path = Path(_state_path(settings))
    if not path.is_file():
        raise LiveCanaryError("No active live-canary record exists.")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise LiveCanaryError("Live-canary record cannot be read safely.") from exc
    if not isinstance(data, dict):
        raise LiveCanaryError("Live-canary record has an invalid format.")
    return data


@contextmanager
def _canary_lock(settings: CryptoSettings):
    """Refuse concurrent canary processes; never silently clear a stale lock."""
    lock_path = f"{_state_path(settings)}.lock"
    os.makedirs(settings.runtime_dir, exist_ok=True)
    try:
        descriptor = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError as exc:
        raise LiveCanaryError(
            "A live-canary lock already exists. Do not launch a second process; "
            "reconcile the existing canary record first."
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


def _trading_credentials() -> tuple[str, str]:
    access_key = os.environ.get("MEXC_TRADING_API_KEY", "").strip()
    secret_key = os.environ.get("MEXC_TRADING_API_SECRET", "").strip()
    if not access_key or not secret_key:
        raise LiveCanaryError(
            "MEXC_TRADING_API_KEY and MEXC_TRADING_API_SECRET are required. "
            "Keep them in the VPS .env only; never paste them into chat."
        )
    return access_key, secret_key


def _is_position_side(position: OpenPosition, side: str) -> bool:
    value = str(position.side or "").lower()
    return value in ({"1", "long", "long_open"} if side == "long" else {"2", "short", "short_open"})


def _find_position(
    positions: Iterable[OpenPosition],
    symbol: str,
    side: str,
) -> Optional[OpenPosition]:
    for position in positions:
        if (
            position.symbol == symbol
            and _is_position_side(position, side)
            and (position.hold_vol or 0) > 0
        ):
            return position
    return None


def _select_candidate(candidates: Iterable[Candidate]) -> Candidate:
    for candidate in candidates:
        if (
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
            and candidate.correlation_status == "clear"
        ):
            return candidate
    raise LiveCanaryError("No fully protected, correlation-clear trade plan is available.")


def _find_protection(
    plans: Iterable[Dict[str, Any]],
    position: OpenPosition,
    order_id: str,
    stop_loss_price: float,
    take_profit_price: float,
) -> Optional[Dict[str, Any]]:
    for plan in plans:
        linked = (
            str(plan.get("positionId") or "") == str(position.position_id or "")
            or str(plan.get("orderId") or "") == order_id
        )
        if not linked or str(plan.get("state") or "1") not in {"1", "untriggered"}:
            continue
        try:
            actual_stop = float(plan.get("stopLossPrice"))
            actual_target = float(plan.get("takeProfitPrice"))
        except (TypeError, ValueError):
            continue
        tolerance = max(1e-8, abs(stop_loss_price) * 1e-8, abs(take_profit_price) * 1e-8)
        if (
            abs(actual_stop - stop_loss_price) <= tolerance
            and abs(actual_target - take_profit_price) <= tolerance
            and plan.get("id") is not None
        ):
            return plan
    return None


def _require_canary_enabled(settings: CryptoSettings) -> None:
    if not (settings.live_canary_enabled or settings.live_trial_canary_enabled):
        raise LiveCanaryError(
            "Live canary is disabled. Enable the standard or trial canary only "
            "after installing the tested worker on the VPS."
        )
    if settings.position_notional_usdt != 50.0 or settings.leverage_max != 20:
        raise LiveCanaryError("Live canary only permits the fixed $50 / 20x profile.")


def _all_open_orders(reader: MexcPrivateClient) -> list[Any]:
    """Read every current normal order page before allowing a new live entry."""
    orders: list[Any] = []
    for page in range(1, 101):
        batch = reader.get_open_orders(page_num=page, page_size=100)
        orders.extend(batch)
        if len(batch) < 100:
            return orders
    raise LiveCanaryError("Too many open orders to safely inspect for a canary entry.")


def run_live_canary_preflight(settings: CryptoSettings) -> Dict[str, Any]:
    """Validate the dedicated trading key and clean-account requirement without a trade."""
    _require_canary_enabled(settings)
    access_key, secret_key = _trading_credentials()
    reader = MexcPrivateClient(
        access_key,
        secret_key,
        timeout=settings.request_timeout_seconds,
        base_url=MEXC_FUTURES_EXECUTION_BASE,
    )
    positions = reader.get_open_positions()
    orders = _all_open_orders(reader)
    tpsl_plans = reader.get_open_tpsl_orders()
    return {
        "ready": not positions and not orders and not tpsl_plans,
        "execution_host": MEXC_FUTURES_EXECUTION_BASE,
        "open_positions": len(positions),
        "open_orders": len(orders),
        "open_tpsl_plans": len(tpsl_plans),
        "message": (
            "Trading key verified and account is clean."
            if not positions and not orders and not tpsl_plans
            else "Account is not clean; live canary will remain blocked."
        ),
    }


def reconcile_unconfirmed_live_entry(settings: CryptoSettings) -> Dict[str, Any]:
    """
    Resolve a locally uncertain entry only after re-reading every live exchange
    surface. This never submits, cancels, or changes an exchange order.
    """
    _require_canary_enabled(settings)
    with _canary_lock(settings):
        record = _load_state(settings)
        status = str(record.get("status") or "")
        if status not in {"entry_submission_unknown", "entry_reconciliation_pending"}:
            raise LiveCanaryError(
                "Only an unconfirmed entry may be reconciled with this command; "
                f"current record is {status!r}."
            )

        access_key, secret_key = _trading_credentials()
        reader = MexcPrivateClient(
            access_key,
            secret_key,
            timeout=settings.request_timeout_seconds,
            base_url=MEXC_FUTURES_EXECUTION_BASE,
        )
        positions = reader.get_open_positions()
        orders = _all_open_orders(reader)
        tpsl_plans = reader.get_open_tpsl_orders()
        if positions or orders or tpsl_plans:
            raise LiveCanaryError(
                "Exchange reconciliation found live positions, orders, or TP/SL plans. "
                "Inspect MEXC and resolve them manually; the canary record remains locked."
            )

        record.update(
            {
                "status": "entry_absent_reconciled",
                "reconciled_at": now_iso(),
                "reconciliation": "No MEXC position, normal order, or TP/SL plan existed.",
            }
        )
        write_json_atomic(_state_path(settings), record)
        return {
            "status": "entry_absent_reconciled",
            "cleared": True,
            "open_positions": 0,
            "open_orders": 0,
            "open_tpsl_plans": 0,
        }


def execute_live_canary(settings: CryptoSettings, candidates: Iterable[Candidate]) -> Dict[str, Any]:
    """
    Submit one protected live market order only after exhaustive account checks.

    This function never runs in the normal scanner loop. It is called only by
    the `--live-canary --confirm-live` CLI path.
    """
    _require_canary_enabled(settings)
    with _canary_lock(settings):
        if Path(_state_path(settings)).is_file():
            existing = _load_state(settings)
            if existing.get("status") not in {
                "closed",
                "emergency_closed",
                "entry_absent_reconciled",
            }:
                raise LiveCanaryError(
                    f"Existing live-canary record is {existing.get('status')!r}; "
                    "reconcile it before another entry."
                )

        candidate = _select_candidate(candidates)
        access_key, secret_key = _trading_credentials()
        reader = MexcPrivateClient(
            access_key,
            secret_key,
            timeout=settings.request_timeout_seconds,
            base_url=MEXC_FUTURES_EXECUTION_BASE,
        )
        if reader.get_open_positions() or _all_open_orders(reader) or reader.get_open_tpsl_orders():
            raise LiveCanaryError(
                "Live canary requires a clean account: no existing positions, normal "
                "orders, or TP/SL plans. Close or reconcile manual MEXC trades first."
            )

        estimated_notional = (
            candidate.last_price * candidate.planned_quantity * candidate.contract_size
        )
        if estimated_notional > settings.position_notional_usdt + 1e-7:
            raise LiveCanaryError("Candidate exceeds the fixed $50 notional cap.")
        if candidate.planned_margin_usdt > settings.max_isolated_margin_per_position_usdt + 1e-7:
            raise LiveCanaryError("Candidate exceeds the $2.50 isolated-margin cap.")

        external_oid = f"ac-canary-{uuid.uuid4().hex[:20]}"
        record = {
            "status": "entry_pending",
            "created_at": now_iso(),
            "symbol": candidate.symbol,
            "side": candidate.planned_side,
            "entry_reference_price": candidate.last_price,
            "quantity": candidate.planned_quantity,
            "contract_size": candidate.contract_size,
            "estimated_notional_usdt": estimated_notional,
            "estimated_initial_margin_usdt": candidate.planned_margin_usdt,
            "stop_loss_price": candidate.planned_stop_price,
            "take_profit_price": candidate.planned_take_profit_price,
            "profit_lock_trigger_price": candidate.profit_lock_trigger_price,
            "profit_lock_stop_price": candidate.profit_lock_stop_price,
            "entry_external_oid": external_oid,
            "profit_lock_applied": False,
        }
        write_json_atomic(_state_path(settings), record)
        execution = MexcExecutionClient(
            access_key,
            secret_key,
            timeout=settings.request_timeout_seconds,
            base_url=MEXC_FUTURES_EXECUTION_BASE,
        )
        try:
            submitted = execution.submit_protected_market_entry(
                symbol=candidate.symbol,
                side=candidate.planned_side,
                quantity=candidate.planned_quantity,
                reference_price=candidate.last_price,
                stop_loss_price=candidate.planned_stop_price,
                take_profit_price=candidate.planned_take_profit_price,
                external_oid=external_oid,
            )
        except Exception:
            record["status"] = "entry_submission_unknown"
            record["updated_at"] = now_iso()
            write_json_atomic(_state_path(settings), record)
            raise

        record["status"] = "entry_submitted"
        record["entry_order_id"] = submitted.order_id
        record["submitted_at_ms"] = submitted.submitted_at_ms
        record["updated_at"] = now_iso()
        write_json_atomic(_state_path(settings), record)

        position: Optional[OpenPosition] = None
        protection: Optional[Dict[str, Any]] = None
        for _ in range(5):
            time.sleep(1)
            position = _find_position(
                reader.get_open_positions(),
                candidate.symbol,
                candidate.planned_side,
            )
            if position is not None:
                protection = _find_protection(
                    reader.get_open_tpsl_orders(candidate.symbol),
                    position,
                    submitted.order_id,
                    candidate.planned_stop_price,
                    candidate.planned_take_profit_price,
                )
                if protection is not None:
                    break

        if position is None:
            record["status"] = "entry_reconciliation_pending"
            record["updated_at"] = now_iso()
            write_json_atomic(_state_path(settings), record)
            raise LiveCanaryError(
                f"Order {submitted.order_id} is pending reconciliation. Inspect MEXC; "
                "the durable canary record blocks a second entry."
            )
        if protection is None:
            record["status"] = "emergency_close_pending"
            record["position_id"] = position.position_id
            record["updated_at"] = now_iso()
            write_json_atomic(_state_path(settings), record)
            if not position.position_id:
                record["status"] = "manual_action_required"
                write_json_atomic(_state_path(settings), record)
                raise LiveCanaryError("Position has no exchange ID; close it manually immediately.")
            try:
                emergency = execution.close_market_position(
                    symbol=position.symbol,
                    position_id=str(position.position_id),
                    side=candidate.planned_side,
                    quantity=position.hold_vol or candidate.planned_quantity,
                    reference_price=position.mark_price or candidate.last_price,
                    external_oid=f"ac-panic-{uuid.uuid4().hex[:20]}",
                )
                record["status"] = "emergency_close_submitted"
                record["emergency_close_order_id"] = emergency.order_id
                record["updated_at"] = now_iso()
                write_json_atomic(_state_path(settings), record)
            except Exception:
                record["status"] = "manual_action_required"
                record["updated_at"] = now_iso()
                write_json_atomic(_state_path(settings), record)
                raise
            for _ in range(5):
                time.sleep(1)
                if _find_position(reader.get_open_positions(), candidate.symbol, candidate.planned_side) is None:
                    record["status"] = "emergency_closed"
                    record["updated_at"] = now_iso()
                    write_json_atomic(_state_path(settings), record)
                    break
            if record["status"] != "emergency_closed":
                raise LiveCanaryError(
                    "Emergency close was submitted but is not yet confirmed flat. "
                    "Inspect MEXC immediately; no further entry is permitted."
                )
            raise LiveCanaryError(
                "Protection was not confirmed; the canary was emergency-closed."
            )

        record.update(
            {
                "status": "protected_open",
                "opened_at": now_iso(),
                "position_id": position.position_id,
                "tpsl_plan_order_id": str(protection.get("id") or ""),
            }
        )
        write_json_atomic(_state_path(settings), record)
        return record


def reconcile_live_profit_lock(settings: CryptoSettings) -> Dict[str, Any]:
    """Move the exchange TP/SL plan to the recorded 35%-protected stop once earned."""
    _require_canary_enabled(settings)
    with _canary_lock(settings):
        record = _load_state(settings)
        if record.get("status") != "protected_open":
            return {"status": record.get("status"), "changed": False}
        if record.get("profit_lock_applied"):
            return {"status": "protected_open", "changed": False, "reason": "already_applied"}

        access_key, secret_key = _trading_credentials()
        reader = MexcPrivateClient(
            access_key,
            secret_key,
            timeout=settings.request_timeout_seconds,
            base_url=MEXC_FUTURES_EXECUTION_BASE,
        )
        side = str(record.get("side") or "")
        symbol = str(record.get("symbol") or "")
        position = _find_position(reader.get_open_positions(), symbol, side)
        if position is None:
            record["status"] = "position_not_open"
            record["reconciled_at"] = now_iso()
            write_json_atomic(_state_path(settings), record)
            return {"status": "position_not_open", "changed": False}

        ticker = next(
            (item for item in MexcPublicClient(timeout=settings.request_timeout_seconds).get_all_tickers()
             if item.symbol == symbol),
            None,
        )
        if ticker is None or ticker.last_price is None:
            raise LiveCanaryError("Cannot reconcile profit lock without a current MEXC price.")
        trigger = float(record["profit_lock_trigger_price"])
        reached = ticker.last_price >= trigger if side == "long" else ticker.last_price <= trigger
        if not reached:
            return {"status": "protected_open", "changed": False, "current_price": ticker.last_price}

        plan_id = str(record.get("tpsl_plan_order_id") or "")
        if not plan_id:
            raise LiveCanaryError("Cannot move profit lock: TP/SL plan ID is missing.")
        record["profit_lock_pending"] = True
        record["updated_at"] = now_iso()
        write_json_atomic(_state_path(settings), record)
        execution = MexcExecutionClient(
            access_key,
            secret_key,
            timeout=settings.request_timeout_seconds,
            base_url=MEXC_FUTURES_EXECUTION_BASE,
        )
        execution.change_tpsl_plan(
            stop_plan_order_id=plan_id,
            stop_loss_price=float(record["profit_lock_stop_price"]),
            take_profit_price=float(record["take_profit_price"]),
        )
        protection = _find_protection(
            reader.get_open_tpsl_orders(symbol),
            position,
            str(record.get("entry_order_id") or ""),
            float(record["profit_lock_stop_price"]),
            float(record["take_profit_price"]),
        )
        if protection is None:
            record["status"] = "profit_lock_reconciliation_pending"
            record["updated_at"] = now_iso()
            write_json_atomic(_state_path(settings), record)
            raise LiveCanaryError(
                "Profit-lock update was sent but not confirmed; existing protection "
                "must be inspected on MEXC before another update."
            )
        record["profit_lock_applied"] = True
        record["profit_lock_pending"] = False
        record["profit_lock_applied_at"] = now_iso()
        record["profit_lock_price"] = ticker.last_price
        write_json_atomic(_state_path(settings), record)
        return {"status": "protected_open", "changed": True, "current_price": ticker.last_price}