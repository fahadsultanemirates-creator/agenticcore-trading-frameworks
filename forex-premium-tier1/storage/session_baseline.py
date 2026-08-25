"""Durable Dubai-session equity baseline for Premium entry limits."""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo


DUBAI_TZ = ZoneInfo("Asia/Dubai")


@dataclass(frozen=True)
class SessionBaseline:
    session_key: str
    equity: float | None


class SessionBaselineStore:
    """
    Persists the equity baseline for a 05:00 Dubai-to-05:00 Dubai session.

    A real worker deliberately does not capture a new baseline after a mid-session
    restart. It stays entry-locked until the next session boundary so loss/profit
    limits cannot silently reset. Mock mode can opt into immediate capture for
    deterministic local tests.
    """

    def __init__(self, state_path: str):
        path = Path(state_path)
        self.path = path.parent / "session_baseline.json"
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def _load(self) -> SessionBaseline | None:
        try:
            raw = json.loads(self.path.read_text())
            equity = float(raw["equity"])
            session_key = str(raw["session_key"])
            return SessionBaseline(session_key=session_key, equity=equity)
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
            return None

    def _save(self, baseline: SessionBaseline) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(
                {
                    "session_key": baseline.session_key,
                    "equity": baseline.equity,
                    "captured_at": datetime.now(DUBAI_TZ).isoformat(),
                },
                indent=2,
            )
        )
        tmp.replace(self.path)

    def resolve(
        self,
        session_key: str,
        equity: float,
        allow_initial_capture: bool = False,
        now: datetime | None = None,
    ) -> SessionBaseline:
        current = (now or datetime.now(DUBAI_TZ)).astimezone(DUBAI_TZ)
        existing = self._load()
        if existing and existing.session_key == session_key:
            return existing

        within_capture_window = time(5, 0) <= current.time().replace(tzinfo=None) < time(5, 5)
        if equity > 0 and (allow_initial_capture or within_capture_window):
            baseline = SessionBaseline(session_key=session_key, equity=equity)
            self._save(baseline)
            return baseline

        return SessionBaseline(session_key=session_key, equity=None)