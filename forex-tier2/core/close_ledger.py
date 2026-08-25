"""Durable, idempotent reconciliation ledger for broker position exits."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class CloseReconciliationLedger:
    """Persist exit requests before sending them, then record confirmed results once."""

    def __init__(self, path: Path | None = None):
        self.path = path or Path(__file__).parent.parent / "close_reconciliation.json"
        self._data = self._load()

    def _load(self) -> dict[str, dict]:
        try:
            data = json.loads(self.path.read_text())
            if isinstance(data, dict):
                data.setdefault("pending", {})
                data.setdefault("confirmed", {})
                for entry in data["pending"].values():
                    entry.setdefault("status", "submitted")
                for entry in data["confirmed"].values():
                    entry.setdefault("accounted", False)
                    entry.setdefault("effects", {})
                return data
        except (OSError, ValueError, TypeError):
            pass
        return {"pending": {}, "confirmed": {}}

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self._data, indent=2, default=str))
        temporary.replace(self.path)

    @staticmethod
    def _key(ticket: int) -> str:
        return str(int(ticket))

    def request(self, ticket: int, position: dict, reason: str,
                session_key: str | None = None) -> bool:
        """Durably register an exit before an order is sent; false means in-flight/done."""
        key = self._key(ticket)
        if key in self._data["confirmed"] or key in self._data["pending"]:
            return False
        self._data["pending"][key] = {
            "ticket": int(ticket),
            "position": dict(position),
            "reason": reason,
            "status": "prepared",
            "session_key": session_key,
        }
        self._save()
        return True

    def discard_request(self, ticket: int) -> None:
        """Remove an exit request rejected before any broker close occurred."""
        self._data["pending"].pop(self._key(ticket), None)
        self._save()

    def pending(self) -> list[dict[str, Any]]:
        return list(self._data["pending"].values())

    def get_pending(self, ticket: int) -> dict | None:
        return self._data["pending"].get(self._key(ticket))

    def mark_submitted(self, ticket: int) -> None:
        entry = self.get_pending(ticket)
        if entry:
            entry["status"] = "submitted"
            self._save()

    def prepared(self) -> list[dict[str, Any]]:
        return [entry for entry in self.pending() if entry.get("status") == "prepared"]

    def has_pending_reason(self, reason: str) -> bool:
        return any(entry.get("reason") == reason for entry in self.pending())

    def mark_confirmed(self, ticket: int, deal: dict) -> dict | None:
        """Move a pending exit to durable confirmation before accounting it."""
        key = self._key(ticket)
        entry = self._data["pending"].pop(key, None)
        if entry is None or key in self._data["confirmed"]:
            return None
        entry["deal"] = dict(deal)
        entry["accounted"] = False
        entry["effects"] = {}
        self._data["confirmed"][key] = entry
        self._save()
        return entry

    def unaccounted_confirmations(self) -> list[dict[str, Any]]:
        return [
            entry for entry in self._data["confirmed"].values()
            if not entry.get("accounted", False)
        ]

    def mark_accounted(self, ticket: int) -> None:
        entry = self._data["confirmed"].get(self._key(ticket))
        if entry:
            entry["accounted"] = True
            self._save()

    def effect_done(self, ticket: int, effect: str) -> bool:
        entry = self._data["confirmed"].get(self._key(ticket), {})
        return bool(entry.get("effects", {}).get(effect, False))

    def mark_effect_done(self, ticket: int, effect: str) -> None:
        entry = self._data["confirmed"].get(self._key(ticket))
        if entry:
            entry.setdefault("effects", {})[effect] = True
            self._save()

    def effects_complete(self, ticket: int) -> bool:
        return all(
            self.effect_done(ticket, effect)
            for effect in ("reporter", "memory", "notification")
        )