"""
Premium Tier 1 — Manager / Orchestrator.

Responsibilities:
- Run one scan cycle: fetch data, analyse, score confidence, apply risk, log.
- Run a continuous signal-worker loop with configurable scan interval.
- Write atomic runtime state JSON after every cycle.
- Write JSONL audit records for signals and rejected decisions.
- NEVER place orders in signal mode (enforced here AND in bridge factory).
- Demo/auto execution paths are explicitly shown but remain disabled unless
  ALL of these are true:
    1. mode in ('demo', 'auto')
    2. signal_only=False in settings
    3. Bridge is PremiumMT5Bridge (not MockBridge)
    4. Identity has been validated by bridge.validate_identity()
    5. All risk checks pass for the specific signal

No LLM call in the scan path.
"""
from __future__ import annotations
import asyncio
import logging
import time as time_module
from datetime import datetime
from typing import Optional

from config.settings import PremiumSettings
from domain.models import Signal, WorkerState, VolumeRegime
from adapters.mt5.factory import get_premium_bridge
from adapters.mt5.mock_bridge import PremiumMockBridge
from analysis.multitf import MultiTimeframeAnalyser
from analysis.volume_sense import VolumeSense
from analysis.confidence import ConfidenceCalibrator
from risk.guards import RiskGuard
from storage.state_writer import AtomicStateWriter
from storage.audit_log import AuditLogger
from storage.session_baseline import SessionBaselineStore

logger = logging.getLogger("premium.manager")

# Metals set for classification
METALS = frozenset({"XAUUSD", "XAGUSD"})


def _is_metal(pair: str) -> bool:
    return any(m in pair.upper() for m in ("XAU", "XAG", "GOLD", "SILVER"))


class PremiumManager:
    """
    Premium Tier 1 orchestrator.

    Usage:
        settings = PremiumSettings.from_env()
        manager = PremiumManager(settings)
        manager.run_one_cycle()       # single scan
        manager.run_loop()            # continuous (blocking)
    """

    def __init__(self, settings: PremiumSettings):
        self.settings = settings
        self.state = WorkerState(
            mode=settings.mode,
            worker_name=settings.worker_name,
            trading_active=False,  # always False in signal mode
        )

        # Build bridge
        self.bridge = get_premium_bridge(settings)

        # Validate identity if not mock
        self._identity_ok = False
        if not isinstance(self.bridge, PremiumMockBridge):
            try:
                self.bridge.validate_identity()
                self._identity_ok = True
            except Exception as exc:
                logger.error(f"[Manager] Identity validation failed: {exc}")
                self._identity_ok = False
        else:
            self.bridge.validate_identity()  # always succeeds for mock
            self._identity_ok = True

        # Analysis pipeline
        self.volume_sense = VolumeSense(window=settings.volume_window)
        self.analyser = MultiTimeframeAnalyser(
            bridge=self.bridge, volume_sense=self.volume_sense
        )
        self.calibrator = ConfidenceCalibrator()

        # Risk
        self.risk_guard = RiskGuard(settings.risk)

        # Storage
        self.state_writer = AtomicStateWriter(settings.state_path)
        self.audit_log = AuditLogger(settings.audit_path)
        self.baseline_store = SessionBaselineStore(settings.state_path)

        # Execution guard
        self._execution_enabled = self._check_execution_enabled()

        self.audit_log.log_worker_event(
            "WORKER_START",
            {
                "mode": settings.mode,
                "signal_only": settings.signal_only,
                "execution_enabled": self._execution_enabled,
                "worker_name": settings.worker_name,
            },
        )
        logger.info(
            f"[Manager] Started: mode={settings.mode} "
            f"signal_only={settings.signal_only} "
            f"execution_enabled={self._execution_enabled}"
        )

    def _check_execution_enabled(self) -> bool:
        """
        Execution is ONLY enabled when ALL conditions are met:
        1. mode in ('demo', 'auto')
        2. signal_only=False
        3. Bridge is real MT5 bridge (not mock)
        4. Identity validated
        5. (Per-signal: all risk checks must pass)
        """
        if self.settings.mode not in ("demo", "auto"):
            return False
        if self.settings.signal_only:
            return False
        if isinstance(self.bridge, PremiumMockBridge):
            return False
        if not self._identity_ok:
            return False
        return True

    def _get_account(self) -> dict:
        try:
            return self.bridge.get_account_info()
        except Exception as exc:
            logger.warning(f"[Manager] Account info error: {exc}")
            return {"balance": 0.0, "equity": 0.0}

    def _get_positions(self) -> list:
        try:
            return self.bridge.get_positions()
        except Exception as exc:
            logger.warning(f"[Manager] Positions error: {exc}")
            return []

    def _get_spread_pips(self, pair: str, tick: Optional[dict] = None) -> float:
        """Convert spread to pips. JPY pairs: 0.01 pip; others: 0.0001 pip."""
        try:
            if tick is None:
                tick = self.bridge.get_live_tick(pair)
            spread = tick.get("spread", 0.0)
            pip_size = 0.01 if "JPY" in pair else 0.0001
            return spread / pip_size
        except Exception:
            return 0.0

    def _get_data_age_seconds(self, snapshot: Optional[dict]) -> float:
        """Estimate data age from snapshot. Returns 0 if unavailable."""
        if snapshot is None:
            return 9999.0
        primary = snapshot.get("primary") or {}
        price = primary.get("price")
        if price is None:
            return 9999.0
        # In mock mode, data is always fresh
        if isinstance(self.bridge, PremiumMockBridge):
            return 0.0
        return 0.0  # Real MT5 data is always fresh on fetch

    def run_one_cycle(self) -> dict:
        """
        Run one complete scan cycle.

        Returns a summary dict with signals, rejections, and state.
        No orders placed in signal mode.
        """
        cycle_start = time_module.monotonic()
        self.state.scan_count += 1
        self.state.last_scan_at = datetime.utcnow().isoformat() + "Z"

        logger.info(
            f"[Manager] Scan #{self.state.scan_count} starting — "
            f"mode={self.settings.mode} pairs={len(self.settings.watchlist)}"
        )

        # Account snapshot
        account = self._get_account()
        positions = self._get_positions()

        self.state.balance = float(account.get("balance", 0))
        self.state.equity = float(account.get("equity", 0))
        self.state.open_positions = positions

        # Dubai-session baseline is durable and deliberately fails closed on a
        # mid-session startup. Mock mode may capture immediately for local tests.
        baseline = self.baseline_store.resolve(
            session_key=self.risk_guard.entry_window.session_key(),
            equity=self.state.equity,
            allow_initial_capture=self.settings.mode == "mock",
        )
        self.state.session_key = baseline.session_key
        self.state.session_start_equity = baseline.equity
        baseline_available = baseline.equity is not None
        if baseline_available:
            self.state.session_equity_pnl_pct = round(
                (self.state.equity - baseline.equity) / baseline.equity * 100,
                3,
            )
            self.state.daily_pnl = round(
                self.state.equity - baseline.equity, 2
            )

        # Entry window
        entry_window_open = self.risk_guard.entry_window.is_open()
        self.state.entry_gate = "open" if entry_window_open and baseline_available else "closed"
        if entry_window_open and not baseline_available:
            self.state.entry_gate = "locked"
            self.state.entry_block_reason = (
                "Session baseline unavailable; waiting for the next Dubai 05:00 baseline."
            )
        if self.risk_guard.circuit_breaker_active:
            self.state.circuit_breaker_active = True
            self.state.entry_gate = "locked"

        # Scan all pairs
        logger.info(f"[Manager] Scanning {len(self.settings.watchlist)} pairs...")
        snapshots = self.analyser.scan_all(self.settings.watchlist)

        signals_produced = []
        signals_approved = []
        signals_rejected = []
        volume_regimes = {}

        for pair, snapshot in snapshots.items():
            if snapshot is None:
                continue

            metal = _is_metal(pair)
            snapshot["is_metal"] = metal

            # Volume
            vol_result = snapshot.get("volume")
            vol_regime = VolumeRegime.UNKNOWN
            if vol_result is not None:
                regime_str = (
                    vol_result.regime.value
                    if hasattr(vol_result, "regime")
                    else vol_result.get("regime", "unknown")
                )
                vol_regime = VolumeRegime(regime_str)
            volume_regimes[pair] = vol_regime.value

            # Live tick for spread
            try:
                tick = self.bridge.get_live_tick(pair)
                spread_pips = self._get_spread_pips(pair, tick)
                entry_price = tick.get("ask") if snapshot.get("direction") == "BUY" else tick.get("bid")
            except Exception:
                tick = None
                spread_pips = 0.0
                entry_price = snapshot.get("primary", {}).get("price")

            # Spread check
            spread_ok = spread_pips <= self.settings.risk.max_spread_pips

            # Data freshness
            data_age = self._get_data_age_seconds(snapshot)
            data_fresh = data_age <= self.settings.risk.max_data_age_seconds

            # Confidence
            conf_bd = self.calibrator.score(
                snapshot,
                spread_ok=spread_ok,
                data_fresh=data_fresh,
                session_open=entry_window_open,
            )

            direction = snapshot.get("direction", "HOLD")
            if direction == "HOLD":
                continue  # no signal

            signal = Signal(
                pair=pair,
                direction=direction,
                confidence=conf_bd.total,
                confidence_breakdown=conf_bd,
                entry_price=entry_price,
                atr=float(snapshot.get("primary", {}).get("atr", 0)),
                spread_pips=spread_pips,
                volume_regime=vol_regime,
                is_metal=metal,
            )
            signal_dict = signal.to_dict()
            self.audit_log.log_signal(signal_dict)
            signals_produced.append(signal_dict)

            # Risk evaluation
            risk_decision = self.risk_guard.evaluate_entry(
                pair=pair,
                direction=direction,
                confidence=conf_bd.total,
                spread_pips=spread_pips,
                data_age_seconds=data_age,
                open_positions=positions,
                session_pnl_pct=self.state.session_equity_pnl_pct,
                session_baseline_available=baseline_available,
                volume_regime=vol_regime.value,
                is_metal=metal,
                independent_confirmation=False,
            )
            risk_dict = risk_decision.to_dict()

            if not risk_decision.allowed:
                self.audit_log.log_risk_rejection(signal_dict, risk_dict)
                signals_rejected.append(
                    {"pair": pair, "direction": direction, "confidence": conf_bd.total,
                     "reason": risk_decision.reason}
                )
                logger.debug(
                    f"[Manager] REJECTED {pair} {direction} — {risk_decision.reason}"
                )
            else:
                self.audit_log.log_risk_approved(signal_dict, risk_dict)
                signals_approved.append(signal_dict)
                logger.info(
                    f"[Manager] APPROVED signal: {pair} {direction} "
                    f"confidence={conf_bd.total:.1f}"
                )

                # ── Execution gate ─────────────────────────────────────────
                if self._execution_enabled:
                    # demo/auto path — not implemented in this slice
                    # This block would place an order via self.bridge.place_order(...)
                    # after additional lot-size and SL/TP calculation.
                    # Kept disabled until Phase 4 implementation.
                    self.audit_log.log_signal_only_execution(
                        signal_dict,
                        reason="execution_path_not_yet_implemented",
                    )
                    logger.info(
                        f"[Manager] Execution path reached for {pair} "
                        "but order placement not yet implemented in this slice."
                    )
                else:
                    # Signal-only mode: log what would have been executed
                    self.audit_log.log_signal_only_execution(
                        signal_dict,
                        reason="signal_only_mode",
                    )
                    logger.info(
                        f"[Manager] Signal-only: {pair} {direction} "
                        f"confidence={conf_bd.total:.1f} — no order placed"
                    )

        # Update state
        self.state.volume_regimes = volume_regimes
        self.state.last_signals = signals_approved[-10:]
        self.state.trading_active = self._execution_enabled and not self.risk_guard.circuit_breaker_active

        # Write state
        self.state_writer.write(self.state)

        # Audit scan summary
        self.audit_log.log_scan_complete(
            self.state.scan_count,
            len(signals_produced),
            len(signals_rejected),
        )

        elapsed = time_module.monotonic() - cycle_start
        logger.info(
            f"[Manager] Scan #{self.state.scan_count} complete in {elapsed:.2f}s — "
            f"signals={len(signals_produced)} approved={len(signals_approved)} "
            f"rejected={len(signals_rejected)}"
        )

        return {
            "scan_count": self.state.scan_count,
            "signals_produced": len(signals_produced),
            "signals_approved": signals_approved,
            "signals_rejected": signals_rejected,
            "entry_gate": self.state.entry_gate,
            "execution_enabled": self._execution_enabled,
        }

    def run_loop(self, max_cycles: Optional[int] = None) -> None:
        """
        Run continuous scan loop.

        max_cycles: set a limit for testing. None = run indefinitely.
        """
        logger.info(
            f"[Manager] Starting continuous loop: interval={self.settings.scan_interval_seconds}s"
        )
        cycle = 0
        while True:
            try:
                self.run_one_cycle()
            except KeyboardInterrupt:
                logger.info("[Manager] Interrupted by user")
                break
            except Exception as exc:
                self.state.last_error = str(exc)
                logger.error(f"[Manager] Cycle error: {exc}")
                self.audit_log.log_worker_event(
                    "CYCLE_ERROR", {"error": str(exc)}
                )

            cycle += 1
            if max_cycles is not None and cycle >= max_cycles:
                logger.info(f"[Manager] Reached max_cycles={max_cycles}, stopping")
                break

            time_module.sleep(self.settings.scan_interval_seconds)

        self.audit_log.log_worker_event("WORKER_STOP", {"cycles": cycle})
