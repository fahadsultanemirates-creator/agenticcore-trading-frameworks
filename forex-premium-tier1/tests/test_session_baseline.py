"""Regression tests for the durable Premium session-baseline safety guard."""
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from storage.session_baseline import SessionBaselineStore


DUBAI = ZoneInfo("Asia/Dubai")


def test_real_mid_session_start_fails_closed(tmp_path):
    store = SessionBaselineStore(str(tmp_path / "runtime" / "state.json"))
    result = store.resolve(
        session_key="2026-08-21",
        equity=10_000,
        now=datetime(2026, 8, 21, 14, 0, tzinfo=DUBAI),
    )
    assert result.equity is None


def test_baseline_captures_at_dubai_session_boundary(tmp_path):
    store = SessionBaselineStore(str(tmp_path / "runtime" / "state.json"))
    result = store.resolve(
        session_key="2026-08-21",
        equity=10_000,
        now=datetime(2026, 8, 21, 5, 2, tzinfo=DUBAI),
    )
    assert result.equity == 10_000


def test_existing_session_baseline_survives_restart(tmp_path):
    path = str(tmp_path / "runtime" / "state.json")
    initial = SessionBaselineStore(path)
    initial.resolve(
        session_key="2026-08-21",
        equity=10_000,
        now=datetime(2026, 8, 21, 5, 1, tzinfo=DUBAI),
    )
    reloaded = SessionBaselineStore(path)
    result = reloaded.resolve(
        session_key="2026-08-21",
        equity=10_250,
        now=datetime(2026, 8, 21, 14, 0, tzinfo=DUBAI),
    )
    assert result.equity == 10_000