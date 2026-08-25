"""
pending_setups.py – Durable, local-only expiry for patient Crypto Tier 1 entries.

This module persists observations only. It never calls an exchange and never
creates a position; it only ensures a stale zone cannot reappear as a new
pending trade plan every time a fresh candle arrives.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable

from config.settings import CryptoSettings
from domain.models import Candidate, EntryStatus
from storage.state import read_json_safe, write_json_atomic


def _now_iso(now: datetime) -> str:
    return now.astimezone(timezone.utc).isoformat()


def _parse_iso(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def _setup_key(candidate: Candidate) -> str | None:
    if (
        candidate.planned_side not in ("long", "short")
        or not candidate.entry_structure_id
    ):
        return None
    return f"{candidate.symbol}:{candidate.planned_side}:{candidate.entry_structure_id}"


def _clear_plan(candidate: Candidate) -> None:
    candidate.planned_quantity = None
    candidate.planned_margin_usdt = None
    candidate.planned_stop_price = None
    candidate.planned_take_profit_price = None
    candidate.profit_lock_trigger_price = None
    candidate.profit_lock_stop_price = None
    candidate.planned_target_profit_usdt = None


def apply_pending_setup_expiry(
    state_path: str,
    candidates: Iterable[Candidate],
    settings: CryptoSettings,
    now: datetime | None = None,
) -> None:
    """
    Keep a pending setup's original expiry fixed across cycles.

    The key contains the selected zone, so a newly formed zone is a fresh
    observation. An expired observation stays expired until that happens or a
    later directional confirmation arrives, at which point normal signal
    scoring has supplied a fresh, completed-candle reclaim.
    """
    reference = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    previous = read_json_safe(state_path)
    stored = previous.get("setups")
    setups: Dict[str, Dict[str, Any]] = (
        {str(key): dict(value) for key, value in stored.items() if isinstance(value, dict)}
        if isinstance(stored, dict)
        else {}
    )
    for candidate in candidates:
        key = _setup_key(candidate)
        if key is None:
            continue
        if candidate.entry_status == EntryStatus.CONFIRMED:
            setups.pop(key, None)
            continue
        if candidate.entry_status not in (EntryStatus.PENDING, EntryStatus.EXPIRED):
            setups.pop(key, None)
            continue

        record = setups.get(key)
        expires_at = _parse_iso(record.get("expires_at")) if record else None
        if expires_at is None:
            expires_at = (
                _parse_iso(candidate.entry_expires_at)
                or reference + timedelta(minutes=15 * settings.entry_setup_expiry_candles)
            )
            record = {
                "symbol": candidate.symbol,
                "side": candidate.planned_side,
                "entry_zone_low": candidate.entry_zone_low,
                "entry_zone_high": candidate.entry_zone_high,
                "created_at": _now_iso(reference),
                "expires_at": _now_iso(expires_at),
            }
            setups[key] = record

        candidate.entry_expires_at = _now_iso(expires_at)
        if reference >= expires_at:
            candidate.entry_status = EntryStatus.EXPIRED
            _clear_plan(candidate)
            candidate.note += "; pending setup expired"
        else:
            candidate.entry_status = EntryStatus.PENDING

    write_json_atomic(state_path, {"setups": setups})