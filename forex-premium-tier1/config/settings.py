"""
Premium Tier 1 — Settings loader.

Environment variables use the PREMIUM_MT5_* namespace exclusively.
Never reuse Tier 2 variable names.

Default mode: mock + signal-only. No trades can be placed without
explicit configuration of mode='demo' or mode='auto' AND all
identity/risk checks passing.
"""
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
from dotenv import load_dotenv

load_dotenv()

PREMIUM_DIR = Path(__file__).resolve().parents[1]


# ── Magic number unique to Premium Tier 1 ─────────────────────────────────
PREMIUM_MAGIC_NUMBER = 20260101


@dataclass
class MT5Config:
    """MT5 connection settings read from PREMIUM_MT5_* env vars."""
    terminal_path: Optional[str] = None   # PREMIUM_MT5_TERMINAL_PATH
    login: Optional[int] = None           # PREMIUM_MT5_LOGIN
    server: Optional[str] = None          # PREMIUM_MT5_SERVER
    expected_broker: Optional[str] = None # PREMIUM_MT5_EXPECTED_BROKER
    expected_account_type: Optional[str] = None  # PREMIUM_MT5_ACCOUNT_TYPE
    symbol_suffix: str = ""               # PREMIUM_MT5_SYMBOL_SUFFIX  e.g. ".m"

    @classmethod
    def from_env(cls) -> "MT5Config":
        login_raw = os.environ.get("PREMIUM_MT5_LOGIN")
        return cls(
            terminal_path=os.environ.get("PREMIUM_MT5_TERMINAL_PATH"),
            login=int(login_raw) if login_raw else None,
            server=os.environ.get("PREMIUM_MT5_SERVER"),
            expected_broker=os.environ.get("PREMIUM_MT5_EXPECTED_BROKER"),
            expected_account_type=os.environ.get("PREMIUM_MT5_ACCOUNT_TYPE", "DEMO"),
            symbol_suffix=os.environ.get("PREMIUM_MT5_SYMBOL_SUFFIX", ""),
        )


@dataclass
class RiskConfig:
    """Hard risk limits applied by the risk module."""
    # Entry window (Dubai / Asia/Dubai timezone)
    entry_window_start: str = "05:00"   # PREMIUM_RISK_ENTRY_START
    entry_window_stop: str = "23:29"    # PREMIUM_RISK_ENTRY_STOP

    # Portfolio limits
    max_portfolio_positions: int = 7    # PREMIUM_RISK_MAX_POSITIONS
    max_positions_per_pair: int = 3     # PREMIUM_RISK_MAX_PER_PAIR
    max_currency_exposure: int = 3      # PREMIUM_RISK_MAX_CURRENCY_EXPOSURE
    max_correlated_positions: int = 2   # PREMIUM_RISK_MAX_CORRELATED_POSITIONS

    # Daily P&L guards (% of session-start equity)
    daily_loss_limit_pct: float = 15.0  # PREMIUM_RISK_DAILY_LOSS_PCT
    daily_profit_limit_pct: float = 20.0  # PREMIUM_RISK_DAILY_PROFIT_PCT

    # Spread guard (pips)
    max_spread_pips: float = 5.0        # PREMIUM_RISK_MAX_SPREAD_PIPS

    # Stale data guard (seconds)
    max_data_age_seconds: float = 300.0  # PREMIUM_RISK_MAX_DATA_AGE_S

    # Confidence threshold for new entries
    min_confidence: float = 60.0        # PREMIUM_RISK_MIN_CONFIDENCE

    # Volume gate: gate forex entries on low volume; metals use volume as context
    gate_forex_on_low_volume: bool = True   # PREMIUM_RISK_GATE_FOREX_LOW_VOL
    gate_metals_on_low_volume: bool = False  # PREMIUM_RISK_GATE_METALS_LOW_VOL

    @classmethod
    def from_env(cls) -> "RiskConfig":
        def _float(key, default):
            raw = os.environ.get(key)
            return float(raw) if raw else default

        def _int(key, default):
            raw = os.environ.get(key)
            return int(raw) if raw else default

        def _bool(key, default):
            raw = os.environ.get(key)
            if raw is None:
                return default
            return raw.strip().lower() in ("1", "true", "yes")

        return cls(
            entry_window_start=os.environ.get("PREMIUM_RISK_ENTRY_START", "05:00"),
            entry_window_stop=os.environ.get("PREMIUM_RISK_ENTRY_STOP", "23:29"),
            max_portfolio_positions=_int("PREMIUM_RISK_MAX_POSITIONS", 7),
            max_positions_per_pair=_int("PREMIUM_RISK_MAX_PER_PAIR", 3),
            max_currency_exposure=_int("PREMIUM_RISK_MAX_CURRENCY_EXPOSURE", 3),
            max_correlated_positions=_int("PREMIUM_RISK_MAX_CORRELATED_POSITIONS", 2),
            daily_loss_limit_pct=_float("PREMIUM_RISK_DAILY_LOSS_PCT", 15.0),
            daily_profit_limit_pct=_float("PREMIUM_RISK_DAILY_PROFIT_PCT", 20.0),
            max_spread_pips=_float("PREMIUM_RISK_MAX_SPREAD_PIPS", 5.0),
            max_data_age_seconds=_float("PREMIUM_RISK_MAX_DATA_AGE_S", 300.0),
            min_confidence=_float("PREMIUM_RISK_MIN_CONFIDENCE", 60.0),
            gate_forex_on_low_volume=_bool("PREMIUM_RISK_GATE_FOREX_LOW_VOL", True),
            gate_metals_on_low_volume=_bool("PREMIUM_RISK_GATE_METALS_LOW_VOL", False),
        )


@dataclass
class PremiumSettings:
    """
    Top-level settings container.

    mode options:
      'mock'   — no MT5 connection, purely synthetic data (DEFAULT)
      'signal' — real MT5 data, no execution; signals logged only
      'demo'   — real MT5 demo account, orders placed if ALL guards pass
      'auto'   — real MT5 live account, orders placed if ALL guards pass

    signal_only=True is enforced unless mode in ('demo','auto') AND the
    broker/account identity has been explicitly verified.
    """
    worker_name: str = "premium-tier1-worker-1"   # PREMIUM_WORKER_NAME
    mode: str = "mock"                             # PREMIUM_MODE
    signal_only: bool = True                       # always True unless overridden below

    scan_interval_seconds: float = 60.0            # PREMIUM_SCAN_INTERVAL_S
    watchlist: list = field(default_factory=lambda: [
        "EURUSD", "GBPUSD", "USDJPY", "USDCHF",
        "AUDUSD", "NZDUSD", "USDCAD",
        "XAUUSD", "XAGUSD",
    ])

    mt5: MT5Config = field(default_factory=MT5Config)
    risk: RiskConfig = field(default_factory=RiskConfig)

    # Volume sense window
    volume_window: int = 50   # PREMIUM_VOL_WINDOW

    # State / log paths (use separate namespace from Tier 2)
    state_path: str = str(PREMIUM_DIR / "runtime" / "state.json")
    audit_path: str = str(PREMIUM_DIR / "logs" / "audit.jsonl")
    log_dir: str = str(PREMIUM_DIR / "logs")

    @classmethod
    def from_env(cls) -> "PremiumSettings":
        raw_mode = os.environ.get("PREMIUM_MODE", "mock").lower()
        allowed_modes = {"mock", "signal", "demo", "auto"}
        if raw_mode not in allowed_modes:
            raise ValueError(f"PREMIUM_MODE must be one of {allowed_modes}, got '{raw_mode}'")

        # signal_only is False ONLY for demo/auto, and only when explicitly set
        signal_only_env = os.environ.get("PREMIUM_SIGNAL_ONLY", "").strip().lower()
        if signal_only_env in ("0", "false", "no") and raw_mode in ("demo", "auto"):
            signal_only = False
        else:
            signal_only = True  # DEFAULT and SAFE

        watchlist_raw = os.environ.get("PREMIUM_WATCHLIST", "")
        watchlist = (
            [s.strip() for s in watchlist_raw.split(",") if s.strip()]
            if watchlist_raw
            else [
                "EURUSD", "GBPUSD", "USDJPY", "USDCHF",
                "AUDUSD", "NZDUSD", "USDCAD",
                "XAUUSD", "XAGUSD",
            ]
        )

        scan_interval_raw = os.environ.get("PREMIUM_SCAN_INTERVAL_S", "60")
        volume_window_raw = os.environ.get("PREMIUM_VOL_WINDOW", "50")

        return cls(
            worker_name=os.environ.get("PREMIUM_WORKER_NAME", "premium-tier1-worker-1"),
            mode=raw_mode,
            signal_only=signal_only,
            scan_interval_seconds=float(scan_interval_raw),
            watchlist=watchlist,
            mt5=MT5Config.from_env(),
            risk=RiskConfig.from_env(),
            volume_window=int(volume_window_raw),
            state_path=os.environ.get(
                "PREMIUM_STATE_PATH",
                str(PREMIUM_DIR / "runtime" / "state.json")
            ),
            audit_path=os.environ.get(
                "PREMIUM_AUDIT_PATH",
                str(PREMIUM_DIR / "logs" / "audit.jsonl")
            ),
            log_dir=os.environ.get(
                "PREMIUM_LOG_DIR",
                str(PREMIUM_DIR / "logs")
            ),
        )
