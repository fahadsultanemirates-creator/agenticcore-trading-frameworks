"""
Premium Tier 1 — Atomic runtime state writer.

Writes dashboard-ready JSON atomically (write-temp → rename).
Uses a separate state path from Tier 2 (PREMIUM_STATE_PATH env var).
"""
from __future__ import annotations
import json
import logging
from datetime import datetime
from pathlib import Path

from domain.models import WorkerState

logger = logging.getLogger("premium.storage.state")


class AtomicStateWriter:
    """
    Writes WorkerState to a JSON file atomically.

    Uses a .tmp intermediate file then os.rename (atomic on POSIX).
    On Windows, replace() is used which is also atomic.
    """

    def __init__(self, state_path: str):
        self.state_path = Path(state_path)
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        logger.info(f"[StateWriter] State path: {self.state_path}")

    def write(self, state: WorkerState) -> None:
        """Write state atomically. Silently logs on failure."""
        state.last_updated = datetime.utcnow().isoformat() + "Z"
        state.worker_heartbeat = state.last_updated

        data = state.to_dashboard_dict()
        tmp = self.state_path.with_suffix(".tmp")
        try:
            tmp.write_text(json.dumps(data, indent=2, default=str))
            tmp.replace(self.state_path)
        except OSError as exc:
            logger.error(f"[StateWriter] Failed to write state: {exc}")

    def read(self) -> dict:
        """Read the current state file. Returns empty dict on failure."""
        try:
            return json.loads(self.state_path.read_text())
        except (OSError, json.JSONDecodeError):
            return {}
