"""
settings.py – Environment-driven configuration for Crypto Standard Tier 1.

All values have conservative defaults. No private credentials are parsed here;
signal/paper mode is the only mode in this version.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from math import isclose
from pathlib import Path
from typing import Optional

TIER1_POSITION_NOTIONAL_USDT = 50.0
TIER1_MAX_ISOLATED_MARGIN_PER_POSITION_USDT = 2.5
TIER1_MAX_TOTAL_ISOLATED_MARGIN_USDT = 12.5
TIER1_TAKE_PROFIT_USDT = 3.0
TIER1_BASKET_PROFIT_TARGET_USDT = 5.0
TIER1_LEVERAGE_MIN = 20
TIER1_LEVERAGE_MAX = 20
TIER1_MINIMUM_STOP_PCT = 0.002
TIER1_MAXIMUM_STOP_PCT = 0.02
TIER1_PROFIT_LOCK_ACTIVATION_PCT = 65
TIER1_PROFIT_LOCK_PROTECTION_PCT = 35

def _env_bool(key: str, default: bool = False) -> bool:
    val = os.environ.get(key, "").strip().lower()
    if val in ("1", "true", "yes"):
        return True
    if val in ("0", "false", "no"):
        return False
    return default


def _env_int(key: str, default: int) -> int:
    val = os.environ.get(key, "").strip()
    try:
        return int(val) if val else default
    except ValueError:
        return default


def _env_float(key: str, default: float) -> float:
    val = os.environ.get(key, "").strip()
    try:
        return float(val) if val else default
    except ValueError:
        return default


def _env_str(key: str, default: str) -> str:
    return os.environ.get(key, default).strip() or default


def _load_dotenv(include_private: bool = False) -> None:
    """
    Load a local .env file without adding a runtime dependency.

    Real environment variables win, so production/VPS service configuration
    cannot be overridden by a file copied beside the worker.
    """
    default_path = Path(__file__).resolve().parent.parent / ".env"
    dotenv_path = Path(os.environ.get("CRYPTO_ENV_FILE", str(default_path)))
    if not dotenv_path.is_file():
        return

    for raw_line in dotenv_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key in {
            "MEXC_API_KEY",
            "MEXC_API_SECRET",
            "MEXC_TRADING_API_KEY",
            "MEXC_TRADING_API_SECRET",
        } and not include_private:
            continue
        if key:
            os.environ.setdefault(key, value)


@dataclass(frozen=True)
class CryptoSettings:
    # ── Runtime mode ──────────────────────────────────────────────────────────
    # signal_mode=True means NO order submission, NO leverage mutation.
    # This is the only permitted mode in Tier 1.
    signal_mode: bool = field(default_factory=lambda: True)
    run_forever: bool = field(
        default_factory=lambda: _env_bool("CRYPTO_RUN_FOREVER", False)
    )
    cycle_interval_seconds: int = field(
        default_factory=lambda: _env_int("CRYPTO_CYCLE_INTERVAL_SECONDS", 60)
    )

    # ── Candidate / position limits ───────────────────────────────────────────
    candidate_count: int = field(
        default_factory=lambda: _env_int("CRYPTO_CANDIDATE_COUNT", 5)
    )
    max_open_positions: int = field(
        default_factory=lambda: _env_int("CRYPTO_MAX_OPEN_POSITIONS", 5)
    )

    # ── Position envelope ─────────────────────────────────────────────────────
    position_notional_usdt: float = field(
        default_factory=lambda: _env_float(
            "CRYPTO_POSITION_NOTIONAL_USDT", TIER1_POSITION_NOTIONAL_USDT
        )
    )
    max_isolated_margin_per_position_usdt: float = field(
        default_factory=lambda: _env_float(
            "CRYPTO_MAX_ISOLATED_MARGIN_PER_POSITION_USDT",
            TIER1_MAX_ISOLATED_MARGIN_PER_POSITION_USDT,
        )
    )
    daily_loss_limit_usdt: float = field(
        default_factory=lambda: _env_float("CRYPTO_DAILY_LOSS_LIMIT_USDT", 20.0)
    )
    daily_profit_target_usdt: float = field(
        default_factory=lambda: _env_float("CRYPTO_DAILY_PROFIT_TARGET_USDT", 40.0)
    )
    take_profit_usdt: float = field(
        default_factory=lambda: _env_float("CRYPTO_TAKE_PROFIT_USDT", 3.0)
    )
    basket_profit_target_usdt: float = field(
        default_factory=lambda: _env_float("CRYPTO_BASKET_PROFIT_TARGET_USDT", 5.0)
    )
    stop_atr_period: int = field(
        default_factory=lambda: _env_int("CRYPTO_STOP_ATR_PERIOD", 14)
    )
    stop_atr_multiplier: float = field(
        default_factory=lambda: _env_float("CRYPTO_STOP_ATR_MULTIPLIER", 1.5)
    )
    minimum_stop_pct: float = field(
        default_factory=lambda: _env_float("CRYPTO_MINIMUM_STOP_PCT", 0.002)
    )
    maximum_stop_pct: float = field(
        default_factory=lambda: _env_float("CRYPTO_MAXIMUM_STOP_PCT", 0.02)
    )
    profit_lock_activation_pct: int = field(
        default_factory=lambda: _env_int("CRYPTO_PROFIT_LOCK_ACTIVATION_PCT", 65)
    )
    profit_lock_protection_pct: int = field(
        default_factory=lambda: _env_int("CRYPTO_PROFIT_LOCK_PROTECTION_PCT", 35)
    )
    leverage_min: int = field(
        default_factory=lambda: _env_int("CRYPTO_LEVERAGE_MIN", TIER1_LEVERAGE_MIN)
    )
    leverage_max: int = field(
        default_factory=lambda: _env_int("CRYPTO_LEVERAGE_MAX", 20)
    )
    margin_mode: str = field(default_factory=lambda: "isolated")

    # ── Filter thresholds ─────────────────────────────────────────────────────
    max_spread_pct: float = field(
        default_factory=lambda: _env_float("CRYPTO_MAX_SPREAD_PCT", 0.15)
    )
    min_turnover_usdt_24h: float = field(
        default_factory=lambda: _env_float("CRYPTO_MIN_TURNOVER_USDT_24H", 5_000_000.0)
    )
    stale_data_threshold_seconds: int = field(
        default_factory=lambda: _env_int("CRYPTO_STALE_DATA_THRESHOLD_SECONDS", 300)
    )

    # ── Scan / probe limits ───────────────────────────────────────────────────
    scan_limit: int = field(
        default_factory=lambda: _env_int("CRYPTO_SCAN_LIMIT", 200)
    )
    probe_limit: int = field(
        default_factory=lambda: _env_int("CRYPTO_PROBE_LIMIT", 30)
    )
    microstructure_probe_limit: int = field(
        default_factory=lambda: _env_int("CRYPTO_MICROSTRUCTURE_PROBE_LIMIT", 30)
    )
    cross_market_probe_limit: int = field(
        default_factory=lambda: _env_int("CRYPTO_CROSS_MARKET_PROBE_LIMIT", 10)
    )
    order_book_depth: int = field(
        default_factory=lambda: _env_int("CRYPTO_ORDER_BOOK_DEPTH", 20)
    )
    recent_trade_limit: int = field(
        default_factory=lambda: _env_int("CRYPTO_RECENT_TRADE_LIMIT", 50)
    )
    candle_limit: int = field(
        default_factory=lambda: _env_int("CRYPTO_CANDLE_LIMIT", 100)
    )
    context_candle_limit: int = field(
        default_factory=lambda: _env_int("CRYPTO_CONTEXT_CANDLE_LIMIT", 40)
    )
    minimum_signal_confidence: int = field(
        default_factory=lambda: _env_int("CRYPTO_MINIMUM_SIGNAL_CONFIDENCE", 70)
    )
    relative_volume_min: float = field(
        default_factory=lambda: _env_float("CRYPTO_RELATIVE_VOLUME_MIN", 1.1)
    )
    minimum_level_distance_pct: float = field(
        default_factory=lambda: _env_float("CRYPTO_MINIMUM_LEVEL_DISTANCE_PCT", 0.003)
    )
    structure_zone_width_pct: float = field(
        default_factory=lambda: _env_float("CRYPTO_STRUCTURE_ZONE_WIDTH_PCT", 0.004)
    )
    structure_zone_min_touches: int = field(
        default_factory=lambda: _env_int("CRYPTO_STRUCTURE_ZONE_MIN_TOUCHES", 2)
    )
    entry_zone_proximity_pct: float = field(
        default_factory=lambda: _env_float("CRYPTO_ENTRY_ZONE_PROXIMITY_PCT", 0.004)
    )
    entry_setup_expiry_candles: int = field(
        default_factory=lambda: _env_int("CRYPTO_ENTRY_SETUP_EXPIRY_CANDLES", 4)
    )
    max_correlation: float = field(
        default_factory=lambda: _env_float("CRYPTO_MAX_CORRELATION", 0.80)
    )
    max_total_isolated_margin_usdt: float = field(
        default_factory=lambda: _env_float(
            "CRYPTO_MAX_TOTAL_ISOLATED_MARGIN_USDT",
            TIER1_MAX_TOTAL_ISOLATED_MARGIN_USDT,
        )
    )
    paper_trading_enabled: bool = field(
        default_factory=lambda: _env_bool("CRYPTO_PAPER_TRADING_ENABLED", False)
    )
    telegram_bot_token: str = field(
        default_factory=lambda: _env_str("CRYPTO_TELEGRAM_BOT_TOKEN", "")
    )
    telegram_chat_id: str = field(
        default_factory=lambda: _env_str("CRYPTO_TELEGRAM_CHAT_ID", "")
    )
    heartbeat_every_cycles: int = field(
        default_factory=lambda: _env_int("CRYPTO_HEARTBEAT_EVERY_CYCLES", 30)
    )
    telegram_daily_summary_time: str = field(
        default_factory=lambda: _env_str("CRYPTO_TELEGRAM_DAILY_SUMMARY_TIME", "23:59")
    )
    telegram_weekly_summary_time: str = field(
        default_factory=lambda: _env_str("CRYPTO_TELEGRAM_WEEKLY_SUMMARY_TIME", "23:30")
    )
    telegram_monthly_summary_time: str = field(
        default_factory=lambda: _env_str("CRYPTO_TELEGRAM_MONTHLY_SUMMARY_TIME", "23:45")
    )
    radar_limit: int = field(
        default_factory=lambda: _env_int("CRYPTO_RADAR_LIMIT", 25)
    )
    memory_history_limit: int = field(
        default_factory=lambda: _env_int("CRYPTO_MEMORY_HISTORY_LIMIT", 50)
    )
    # ── Optional AI operator commentary ────────────────────────────────────────
    # When OpenAI is available, operator commentary is enabled by default. It
    # only explains recorded deterministic evidence after paper lifecycle events
    # or in scheduled reports; it can never alter trade decisions.
    ai_explanations_enabled: bool = field(
        default_factory=lambda: _env_bool(
            "CRYPTO_AI_EXPLANATIONS_ENABLED",
            bool(os.environ.get("OPENAI_API_KEY", "").strip()),
        )
    )
    ai_request_timeout_seconds: int = field(
        default_factory=lambda: _env_int("CRYPTO_AI_REQUEST_TIMEOUT_SECONDS", 10)
    )
    ai_max_events_per_cycle: int = field(
        default_factory=lambda: _env_int("CRYPTO_AI_MAX_EVENTS_PER_CYCLE", 10)
    )
    openai_api_key: str = field(default_factory=lambda: _env_str("OPENAI_API_KEY", ""))
    gemini_api_key: str = field(default_factory=lambda: _env_str("GEMINI_API_KEY", ""))
    openai_model: str = field(
        default_factory=lambda: _env_str("CRYPTO_OPENAI_MODEL", "gpt-5-mini")
    )
    gemini_model: str = field(
        default_factory=lambda: _env_str("CRYPTO_GEMINI_MODEL", "gemini-2.5-flash")
    )

    # Laptop-only private read-only access. Main signal cycles never call the
    # private adapter; account checks require a separate explicit CLI flag.
    private_readonly_enabled: bool = field(
        default_factory=lambda: _env_bool("CRYPTO_PRIVATE_READONLY_ENABLED", False)
    )
    # Explicitly gated one-position live test. This is never used by normal
    # scanner or paper paths and requires a second CLI confirmation.
    live_canary_enabled: bool = field(
        default_factory=lambda: _env_bool("CRYPTO_LIVE_CANARY_ENABLED", False)
    )
    # A separately named, deliberately broader live validation path. It never
    # changes normal scans or the standard canary and stays disabled by default.
    live_trial_canary_enabled: bool = field(
        default_factory=lambda: _env_bool("CRYPTO_LIVE_TRIAL_CANARY_ENABLED", False)
    )
    trial_entry_mode: bool = False
    trial_minimum_signal_confidence: int = field(
        default_factory=lambda: _env_int("CRYPTO_TRIAL_MINIMUM_SIGNAL_CONFIDENCE", 50)
    )
    # Explicit live-cycle gate. The normal scanner never reads this setting.
    # A cycle can submit at most five independently protected Tier 1 entries.
    live_cycle_enabled: bool = field(
        default_factory=lambda: _env_bool("CRYPTO_LIVE_CYCLE_ENABLED", False)
    )
    live_cycle_max_positions: int = field(
        default_factory=lambda: _env_int("CRYPTO_LIVE_CYCLE_MAX_POSITIONS", 5)
    )

    # ── HTTP transport ────────────────────────────────────────────────────────
    request_timeout_seconds: int = field(
        default_factory=lambda: _env_int("CRYPTO_REQUEST_TIMEOUT_SECONDS", 10)
    )

    # ── Paths ─────────────────────────────────────────────────────────────────
    runtime_dir: str = field(
        default_factory=lambda: _env_str(
            "CRYPTO_RUNTIME_DIR",
            os.path.join(os.path.dirname(os.path.dirname(__file__)), "runtime"),
        )
    )
    log_dir: str = field(
        default_factory=lambda: _env_str(
            "CRYPTO_LOG_DIR",
            os.path.join(os.path.dirname(os.path.dirname(__file__)), "logs"),
        )
    )
    memory_db_path: str = field(
        default_factory=lambda: _env_str(
            "CRYPTO_MEMORY_DB_PATH",
            os.path.join(os.path.dirname(os.path.dirname(__file__)), "runtime", "coin_memory.sqlite3"),
        )
    )
    memory_snapshot_path: str = field(
        default_factory=lambda: _env_str(
            "CRYPTO_MEMORY_SNAPSHOT_PATH",
            os.path.join(os.path.dirname(os.path.dirname(__file__)), "runtime", "memory_snapshot.json"),
        )
    )

    def validate(self) -> None:
        """Raise ValueError on obviously invalid configuration."""
        if not self.signal_mode:
            raise ValueError(
                "signal_mode must be True; live execution is not implemented in Tier 1."
            )
        if self.leverage_max > 20:
            raise ValueError(
                f"leverage_max {self.leverage_max} exceeds the Tier 1 ceiling of 20x."
            )
        if self.leverage_min < 1:
            raise ValueError("leverage_min must be at least 1.")
        if self.leverage_min > self.leverage_max:
            raise ValueError("leverage_min must be <= leverage_max.")
        if self.position_notional_usdt <= 0:
            raise ValueError("position_notional_usdt must be positive.")
        if self.max_isolated_margin_per_position_usdt <= 0:
            raise ValueError("max_isolated_margin_per_position_usdt must be positive.")
        if self.daily_loss_limit_usdt <= 0:
            raise ValueError("daily_loss_limit_usdt must be positive.")
        if self.daily_profit_target_usdt <= 0:
            raise ValueError("daily_profit_target_usdt must be positive.")
        if self.take_profit_usdt <= 0:
            raise ValueError("take_profit_usdt must be positive.")
        if self.basket_profit_target_usdt <= 0:
            raise ValueError("basket_profit_target_usdt must be positive.")
        if self.stop_atr_period < 1:
            raise ValueError("stop_atr_period must be at least 1.")
        if self.stop_atr_multiplier <= 0:
            raise ValueError("stop_atr_multiplier must be positive.")
        if self.minimum_stop_pct <= 0 or self.maximum_stop_pct <= 0:
            raise ValueError("stop percentage bounds must be positive.")
        if self.minimum_stop_pct > self.maximum_stop_pct:
            raise ValueError("minimum_stop_pct must be <= maximum_stop_pct.")
        if not 0 < self.profit_lock_activation_pct < 100:
            raise ValueError("profit_lock_activation_pct must be between 1 and 99.")
        if not 0 < self.profit_lock_protection_pct < 100:
            raise ValueError("profit_lock_protection_pct must be between 1 and 99.")
        if self.candidate_count < 1:
            raise ValueError("candidate_count must be at least 1.")
        if not 1 <= self.live_cycle_max_positions <= 5:
            raise ValueError("live_cycle_max_positions must be between 1 and 5.")
        if self.probe_limit < self.candidate_count:
            raise ValueError("probe_limit must cover candidate_count.")
        if not 1 <= self.microstructure_probe_limit <= self.probe_limit:
            raise ValueError("microstructure_probe_limit must be between 1 and probe_limit.")
        if not self.candidate_count <= self.cross_market_probe_limit <= self.probe_limit:
            raise ValueError("cross_market_probe_limit must be between candidate_count and probe_limit.")
        if not 1 <= self.order_book_depth <= 50:
            raise ValueError("order_book_depth must be between 1 and 50.")
        if not 1 <= self.recent_trade_limit <= 100:
            raise ValueError("recent_trade_limit must be between 1 and 100.")
        if not 0 <= self.minimum_signal_confidence <= 100:
            raise ValueError("minimum_signal_confidence must be between 0 and 100.")
        if not 0 <= self.trial_minimum_signal_confidence <= 100:
            raise ValueError("trial_minimum_signal_confidence must be between 0 and 100.")
        if self.relative_volume_min < 1:
            raise ValueError("relative_volume_min must be at least 1.0.")
        if not 0 < self.minimum_level_distance_pct < 0.10:
            raise ValueError("minimum_level_distance_pct must be between 0 and 0.10.")
        if not 0 < self.structure_zone_width_pct < 0.10:
            raise ValueError("structure_zone_width_pct must be between 0 and 0.10.")
        if self.structure_zone_min_touches < 2:
            raise ValueError("structure_zone_min_touches must be at least 2.")
        if not 0 < self.entry_zone_proximity_pct < 0.10:
            raise ValueError("entry_zone_proximity_pct must be between 0 and 0.10.")
        if self.entry_setup_expiry_candles < 1:
            raise ValueError("entry_setup_expiry_candles must be at least 1.")
        if not 0 < self.max_correlation <= 1:
            raise ValueError("max_correlation must be between 0 and 1.")
        if self.max_total_isolated_margin_usdt < self.max_isolated_margin_per_position_usdt:
            raise ValueError(
                "max_total_isolated_margin_usdt must cover one planned trade."
            )
        if self.heartbeat_every_cycles < 1:
            raise ValueError("heartbeat_every_cycles must be at least 1.")
        if not 1 <= self.radar_limit <= 500:
            raise ValueError("radar_limit must be between 1 and 500.")
        if not 1 <= self.memory_history_limit <= 500:
            raise ValueError("memory_history_limit must be between 1 and 500.")
        if not 1 <= self.ai_request_timeout_seconds <= 60:
            raise ValueError("ai_request_timeout_seconds must be between 1 and 60.")
        if not 1 <= self.ai_max_events_per_cycle <= 10:
            raise ValueError("ai_max_events_per_cycle must be between 1 and 10.")
        report_times = {
            "telegram_daily_summary_time": (self.telegram_daily_summary_time, "23:59"),
            "telegram_weekly_summary_time": (self.telegram_weekly_summary_time, "23:30"),
            "telegram_monthly_summary_time": (self.telegram_monthly_summary_time, "23:45"),
        }
        for name, (configured, expected) in report_times.items():
            try:
                hour, minute = (int(part) for part in configured.split(":", 1))
            except (TypeError, ValueError):
                raise ValueError(f"{name} must use HH:MM.") from None
            if not 0 <= hour <= 23 or not 0 <= minute <= 59:
                raise ValueError(f"{name} must use a valid 24-hour time.")
            if configured != expected:
                raise ValueError(f"{name} is fixed at {expected} Dubai time.")
        if not isclose(self.position_notional_usdt, TIER1_POSITION_NOTIONAL_USDT):
            raise ValueError("Tier 1 position_notional_usdt is fixed at 50.0 USDT.")
        if not isclose(
            self.max_isolated_margin_per_position_usdt,
            TIER1_MAX_ISOLATED_MARGIN_PER_POSITION_USDT,
        ):
            raise ValueError(
                "Tier 1 max_isolated_margin_per_position_usdt is fixed at 2.5 USDT."
            )
        if not isclose(
            self.max_total_isolated_margin_usdt,
            TIER1_MAX_TOTAL_ISOLATED_MARGIN_USDT,
        ):
            raise ValueError(
                "Tier 1 max_total_isolated_margin_usdt is fixed at 12.5 USDT."
            )
        if not isclose(self.take_profit_usdt, TIER1_TAKE_PROFIT_USDT):
            raise ValueError("Tier 1 take_profit_usdt is fixed at 3.0 USDT.")
        if not isclose(self.basket_profit_target_usdt, TIER1_BASKET_PROFIT_TARGET_USDT):
            raise ValueError("Tier 1 basket_profit_target_usdt is fixed at 5.0 USDT.")
        if (self.leverage_min, self.leverage_max) != (TIER1_LEVERAGE_MIN, TIER1_LEVERAGE_MAX):
            raise ValueError("Tier 1 leverage is fixed at 20x.")
        if not isclose(self.minimum_stop_pct, TIER1_MINIMUM_STOP_PCT):
            raise ValueError("Tier 1 minimum_stop_pct is fixed at 0.2%.")
        if not isclose(self.maximum_stop_pct, TIER1_MAXIMUM_STOP_PCT):
            raise ValueError("Tier 1 maximum_stop_pct is fixed at 2.0%.")
        if self.profit_lock_activation_pct != TIER1_PROFIT_LOCK_ACTIVATION_PCT:
            raise ValueError("Tier 1 profit-lock activation is fixed at 65%.")
        if self.profit_lock_protection_pct != TIER1_PROFIT_LOCK_PROTECTION_PCT:
            raise ValueError("Tier 1 profit-lock protection is fixed at 35%.")


def load_settings(include_private_dotenv: bool = False) -> CryptoSettings:
    """
    Load public worker settings.

    Private key material is deliberately skipped unless an explicit laptop-only
    account inspection requests it. Normal market scans never read or validate
    MEXC private credentials.
    """
    _load_dotenv(include_private=include_private_dotenv)
    s = CryptoSettings()
    s.validate()
    return s
