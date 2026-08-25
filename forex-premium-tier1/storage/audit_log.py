"""
Premium Tier 1 — JSONL Audit Logger.

Appends structured audit records to a JSONL file.
Records every signal (including rejected ones) and every risk decision.
Uses a separate audit path from Tier 2 (PREMIUM_AUDIT_PATH env var).

Record types:
  SIGNAL          — a candidate signal produced by analysis
  RISK_REJECTION  — a signal rejected by the risk module
  RISK_APPROVED   — a signal approved by risk (but still signal-only mode)
  EXECUTION       — would-be order (only logged, never placed in signal mode)
  WORKER_EVENT    — scan start/stop, heartbeat, mode change, error
"""
from __future__ import annotations
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger("premium.storage.audit")


class AuditLogger:
    """
    Appends records to a JSONL audit file.

    Thread-safe for single-process use (GIL-protected file append).
    For multi-process, use a lock or a log aggregator.
    """

    def __init__(self, audit_path: str):
        self.audit_path = Path(audit_path)
        self.audit_path.parent.mkdir(parents=True, exist_ok=True)
        logger.info(f"[AuditLog] Audit path: {self.audit_path}")

    def _record(self, record_type: str, data: dict) -> None:
        entry = {
            "ts": datetime.utcnow().isoformat() + "Z",
            "type": record_type,
            **data,
        }
        line = json.dumps(entry, default=str) + "\n"
        try:
            with self.audit_path.open("a", encoding="utf-8") as f:
                f.write(line)
        except OSError as exc:
            logger.error(f"[AuditLog] Write failed: {exc}")

    def log_signal(self, signal_dict: dict) -> None:
        self._record("SIGNAL", {"signal": signal_dict})

    def log_risk_rejection(
        self, signal_dict: dict, risk_decision_dict: dict
    ) -> None:
        self._record(
            "RISK_REJECTION",
            {"signal": signal_dict, "risk": risk_decision_dict},
        )

    def log_risk_approved(
        self, signal_dict: dict, risk_decision_dict: dict
    ) -> None:
        self._record(
            "RISK_APPROVED",
            {"signal": signal_dict, "risk": risk_decision_dict},
        )

    def log_signal_only_execution(
        self, signal_dict: dict, reason: str = "signal_only_mode"
    ) -> None:
        """
        Log what WOULD have been an execution in demo/auto mode.
        No order is ever placed from this method.
        """
        self._record(
            "EXECUTION_SIGNAL_ONLY",
            {
                "signal": signal_dict,
                "reason": reason,
                "note": "No order placed. signal_only=True.",
            },
        )

    def log_worker_event(self, event: str, details: Optional[dict] = None) -> None:
        self._record("WORKER_EVENT", {"event": event, "details": details or {}})

    def log_scan_complete(self, scan_count: int, signals_found: int, rejected: int) -> None:
        self._record(
            "WORKER_EVENT",
            {
                "event": "SCAN_COMPLETE",
                "details": {
                    "scan_count": scan_count,
                    "signals_found": signals_found,
                    "rejected": rejected,
                },
            },
        )
