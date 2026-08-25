"""
storage/state.py – Atomic JSON and JSONL write helpers.

All writes use a write-to-temp-then-rename pattern to prevent corrupt state
if the process is interrupted mid-write.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from typing import Any, Dict


def _ensure_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)


def write_json_atomic(path: str, data: Dict[str, Any], indent: int = 2) -> None:
    """
    Write `data` as JSON to `path` atomically.
    Creates parent directories if needed.
    """
    _ensure_dir(os.path.dirname(os.path.abspath(path)))
    dir_name = os.path.dirname(os.path.abspath(path))
    fd, tmp_path = tempfile.mkstemp(dir=dir_name, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=indent, default=str)
            f.write("\n")
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def append_jsonl(path: str, record: Dict[str, Any]) -> None:
    """
    Append a single JSON record as one line to a JSONL file.
    Creates parent directories if needed.
    Thread-safety: not guaranteed; use single-process only.
    """
    _ensure_dir(os.path.dirname(os.path.abspath(path)))
    line = json.dumps(record, default=str) + "\n"
    with open(path, "a", encoding="utf-8") as f:
        f.write(line)


def read_json_safe(path: str) -> Dict[str, Any]:
    """
    Read and parse a JSON file. Returns an empty dict on any error.
    """
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
