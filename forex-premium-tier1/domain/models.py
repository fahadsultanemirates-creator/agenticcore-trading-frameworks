"""
Premium Tier 1 — core domain models.

All internal fields use snake_case. The dashboard-facing dict output
may include camelCase keys for frontend compatibility.
"""
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional
from enum import Enum


class VolumeRegime(str, Enum):
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"
    UNKNOWN = "unknown"


@dataclass
class ConfidenceBreakdown:
    """
    Calibrated confidence score with transparent components.

    Components:
      base_technical  — quality of technical signals (0–40 pts)
      timeframe_agreement — M15/H1/H4 alignment (0–25 pts)
      volume_participation — VolumeSense score contribution (0–20 pts)
      context_quality — spread, data freshness, session (0–15 pts)

    Total is always clamped to [0, 100].
    The calibration_policy string references the table used.
    """
    base_technical: float = 0.0       # 0–40
    timeframe_agreement: float = 0.0  # 0–25
    volume_participation: float = 0.0 # 0–20
    context_quality: float = 0.0      # 0–15

    total: float = 0.0                # clamped sum
    volume_regime: VolumeRegime = VolumeRegime.UNKNOWN
    volume_score: float = 0.0         # raw participation score 0–1
    volume_reason: str = ""
    calibration_policy: str = "v1_fixed_table"
    reason: str = ""                  # human-readable breakdown

    def compute_total(self) -> float:
        raw = (
            self.base_technical
            + self.timeframe_agreement
            + self.volume_participation
            + self.context_quality
        )
        self.total = max(0.0, min(100.0, raw))
        return self.total

    def to_dict(self) -> dict:
        return {
            "total": round(self.total, 2),
            "base_technical": round(self.base_technical, 2),
            "timeframe_agreement": round(self.timeframe_agreement, 2),
            "volume_participation": round(self.volume_participation, 2),
            "context_quality": round(self.context_quality, 2),
            "volume_regime": self.volume_regime.value,
            "volume_score": round(self.volume_score, 3),
            "volume_reason": self.volume_reason,
            "calibration_policy": self.calibration_policy,
            "reason": self.reason,
        }


@dataclass
class Signal:
    """A candidate trading signal produced by analysis. Never implies an order."""
    pair: str
    direction: str        # "BUY" | "SELL" | "HOLD"
    confidence: float     # 0–100
    confidence_breakdown: Optional[ConfidenceBreakdown] = None
    entry_price: Optional[float] = None
    atr: float = 0.0
    spread_pips: float = 0.0
    timeframe_snapshots: dict = field(default_factory=dict)
    volume_regime: VolumeRegime = VolumeRegime.UNKNOWN
    generated_at: str = field(default_factory=lambda: datetime.utcnow().isoformat())
    is_metal: bool = False

    def to_dict(self) -> dict:
        return {
            "pair": self.pair,
            "direction": self.direction,
            "confidence": round(self.confidence, 2),
            "confidence_breakdown": (
                self.confidence_breakdown.to_dict()
                if self.confidence_breakdown else {}
            ),
            "entry_price": self.entry_price,
            "atr": round(self.atr, 6),
            "spread_pips": round(self.spread_pips, 5),
            "volume_regime": self.volume_regime.value,
            "generated_at": self.generated_at,
            "is_metal": self.is_metal,
        }


@dataclass
class RiskDecision:
    """Result of the risk module evaluation for a candidate signal."""
    allowed: bool
    reason: str = ""
    checks: dict = field(default_factory=dict)  # named check -> pass/fail

    def to_dict(self) -> dict:
        return {
            "allowed": self.allowed,
            "reason": self.reason,
            "checks": self.checks,
        }


@dataclass
class WorkerState:
    """
    Mutable runtime state written atomically to JSON.
    Internal snake_case; to_dashboard_dict() emits camelCase for the
    dashboard-facing JSON output.
    """
    mode: str = "mock"
    worker_name: str = "premium-tier1-worker-1"
    trading_active: bool = False
    circuit_breaker_active: bool = False
    kill_switch_active: bool = False
    last_updated: str = ""

    balance: float = 0.0
    equity: float = 0.0
    daily_pnl: float = 0.0
    total_trades: int = 0
    win_rate: float = 0.0
    open_positions: list = field(default_factory=list)

    session_key: str = ""
    session_start_equity: Optional[float] = None
    session_equity_pnl_pct: float = 0.0
    daily_entry_lock_reason: str = ""
    entry_block_reason: str = ""

    # Premium analysis fields
    volume_regimes: dict = field(default_factory=dict)       # pair -> regime
    last_signals: list = field(default_factory=list)          # last N signals
    entry_gate: str = "closed"                               # open/closed/locked
    worker_heartbeat: str = ""

    # Scan tracking
    scan_count: int = 0
    last_scan_at: str = ""
    last_error: str = ""

    def to_dashboard_dict(self) -> dict:
        """Emit camelCase dashboard-ready JSON."""
        return {
            "mode": self.mode,
            "workerName": self.worker_name,
            "tradingActive": self.trading_active,
            "circuitBreakerActive": self.circuit_breaker_active,
            "killSwitchActive": self.kill_switch_active,
            "lastUpdated": self.last_updated,
            "balance": round(self.balance, 2),
            "equity": round(self.equity, 2),
            "dailyPnl": round(self.daily_pnl, 2),
            "totalTrades": self.total_trades,
            "winRate": round(self.win_rate, 4),
            "openPositions": self.open_positions,
            "entryBlockReason": self.entry_block_reason,
            "premiumAnalysis": {
                "volumeRegimes": self.volume_regimes,
                "confidenceCalibration": "v1_fixed_table",
                "lastSignals": self.last_signals[-10:],
                "entryGate": self.entry_gate,
                "workerHeartbeat": self.worker_heartbeat,
                "scanCount": self.scan_count,
                "lastScanAt": self.last_scan_at,
            },
        }
