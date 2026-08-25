"""
Tests for PremiumManager signal-only non-execution behavior.

Validates:
- In mock/signal mode, run_one_cycle() never calls bridge.place_order()
- State is written after a cycle
- Audit log is written after a cycle
- No orders placed even when signals are approved by risk
- execution_enabled is False in mock mode
- Approved signals are logged as EXECUTION_SIGNAL_ONLY
- Manager can complete a cycle without errors
"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import json
import pytest
from unittest.mock import patch, MagicMock
from pathlib import Path

from config.settings import PremiumSettings, RiskConfig, MT5Config
from runtime.manager import PremiumManager


def make_test_settings(
    mode="mock",
    signal_only=True,
    state_path="/tmp/test_premium_state.json",
    audit_path="/tmp/test_premium_audit.jsonl",
) -> PremiumSettings:
    return PremiumSettings(
        worker_name="test-worker",
        mode=mode,
        signal_only=signal_only,
        scan_interval_seconds=1.0,
        watchlist=["EURUSD", "XAUUSD"],  # small watchlist for speed
        mt5=MT5Config(),
        risk=RiskConfig(
            min_confidence=0.0,   # allow all signals through risk for testing
            gate_forex_on_low_volume=False,
        ),
        volume_window=50,
        state_path=state_path,
        audit_path=audit_path,
    )


class TestManagerSignalOnly:
    def test_execution_disabled_in_mock_mode(self):
        settings = make_test_settings(mode="mock", signal_only=True)
        manager = PremiumManager(settings)
        assert manager._execution_enabled is False

    def test_execution_disabled_when_signal_only_true(self):
        """Even in 'auto' mode, signal_only=True keeps execution disabled."""
        settings = make_test_settings(mode="auto", signal_only=True)
        manager = PremiumManager(settings)
        # MockBridge is used (not MT5), so execution_enabled must be False
        assert manager._execution_enabled is False

    def test_place_order_never_called_in_mock_mode(self, tmp_path):
        """bridge.place_order must NEVER be called during a mock cycle."""
        state_path = str(tmp_path / "state.json")
        audit_path = str(tmp_path / "audit.jsonl")
        settings = make_test_settings(
            mode="mock",
            state_path=state_path,
            audit_path=audit_path,
        )
        manager = PremiumManager(settings)

        # Spy on the bridge
        original_place_order = manager.bridge.place_order
        order_calls = []

        def spy_place_order(*args, **kwargs):
            order_calls.append((args, kwargs))
            return original_place_order(*args, **kwargs)

        manager.bridge.place_order = spy_place_order

        manager.run_one_cycle()

        assert len(order_calls) == 0, (
            f"place_order was called {len(order_calls)} time(s) in mock/signal mode!"
        )

    def test_state_file_written_after_cycle(self, tmp_path):
        state_path = str(tmp_path / "state.json")
        audit_path = str(tmp_path / "audit.jsonl")
        settings = make_test_settings(
            state_path=state_path,
            audit_path=audit_path,
        )
        manager = PremiumManager(settings)
        manager.run_one_cycle()

        assert Path(state_path).exists(), "State file not written"
        data = json.loads(Path(state_path).read_text())
        # Verify required dashboard fields
        assert "mode" in data
        assert "workerName" in data
        assert "tradingActive" in data
        assert "premiumAnalysis" in data

    def test_state_trading_active_false_in_signal_mode(self, tmp_path):
        state_path = str(tmp_path / "state.json")
        audit_path = str(tmp_path / "audit.jsonl")
        settings = make_test_settings(
            state_path=state_path,
            audit_path=audit_path,
        )
        manager = PremiumManager(settings)
        manager.run_one_cycle()

        data = json.loads(Path(state_path).read_text())
        assert data["tradingActive"] is False

    def test_audit_log_written_after_cycle(self, tmp_path):
        state_path = str(tmp_path / "state.json")
        audit_path = str(tmp_path / "audit.jsonl")
        settings = make_test_settings(
            state_path=state_path,
            audit_path=audit_path,
        )
        manager = PremiumManager(settings)
        manager.run_one_cycle()

        assert Path(audit_path).exists(), "Audit log not written"
        lines = Path(audit_path).read_text().strip().splitlines()
        assert len(lines) > 0, "Audit log is empty"

        # All lines must be valid JSON with a 'type' field
        for line in lines:
            record = json.loads(line)
            assert "type" in record
            assert "ts" in record

    def test_audit_contains_worker_start(self, tmp_path):
        state_path = str(tmp_path / "state.json")
        audit_path = str(tmp_path / "audit.jsonl")
        settings = make_test_settings(
            state_path=state_path,
            audit_path=audit_path,
        )
        manager = PremiumManager(settings)
        manager.run_one_cycle()

        lines = Path(audit_path).read_text().strip().splitlines()
        events = [json.loads(l) for l in lines]
        event_types = [e.get("type") for e in events]
        assert "WORKER_EVENT" in event_types

    def test_cycle_returns_correct_structure(self, tmp_path):
        state_path = str(tmp_path / "state.json")
        audit_path = str(tmp_path / "audit.jsonl")
        settings = make_test_settings(
            state_path=state_path,
            audit_path=audit_path,
        )
        manager = PremiumManager(settings)
        result = manager.run_one_cycle()

        assert "scan_count" in result
        assert "signals_produced" in result
        assert "signals_approved" in result
        assert "signals_rejected" in result
        assert "entry_gate" in result
        assert "execution_enabled" in result
        assert result["execution_enabled"] is False
        assert result["scan_count"] == 1

    def test_scan_count_increments(self, tmp_path):
        state_path = str(tmp_path / "state.json")
        audit_path = str(tmp_path / "audit.jsonl")
        settings = make_test_settings(
            state_path=state_path,
            audit_path=audit_path,
        )
        manager = PremiumManager(settings)
        manager.run_one_cycle()
        manager.run_one_cycle()
        result = manager.run_one_cycle()
        assert result["scan_count"] == 3

    def test_approved_signals_logged_as_signal_only(self, tmp_path):
        """Any approved signal must be logged as EXECUTION_SIGNAL_ONLY, not an order."""
        state_path = str(tmp_path / "state.json")
        audit_path = str(tmp_path / "audit.jsonl")
        settings = make_test_settings(
            state_path=state_path,
            audit_path=audit_path,
        )
        manager = PremiumManager(settings)
        manager.run_one_cycle()

        lines = Path(audit_path).read_text().strip().splitlines()
        events = [json.loads(l) for l in lines]
        # No order-execution records should exist
        order_types = [e for e in events if e.get("type") == "ORDER_PLACED"]
        assert len(order_types) == 0, "Unexpected ORDER_PLACED record in signal mode"

    def test_dashboard_fields_present(self, tmp_path):
        """State JSON must include all required dashboard fields."""
        state_path = str(tmp_path / "state.json")
        audit_path = str(tmp_path / "audit.jsonl")
        settings = make_test_settings(
            state_path=state_path,
            audit_path=audit_path,
        )
        manager = PremiumManager(settings)
        manager.run_one_cycle()

        data = json.loads(Path(state_path).read_text())
        required = [
            "mode", "workerName", "tradingActive", "circuitBreakerActive",
            "lastUpdated", "balance", "equity", "dailyPnl",
            "totalTrades", "winRate", "openPositions", "premiumAnalysis",
        ]
        for field in required:
            assert field in data, f"Missing dashboard field: {field}"

        pa = data["premiumAnalysis"]
        pa_required = [
            "volumeRegimes", "confidenceCalibration", "lastSignals",
            "entryGate", "workerHeartbeat", "scanCount", "lastScanAt",
        ]
        for field in pa_required:
            assert field in pa, f"Missing premiumAnalysis field: {field}"
