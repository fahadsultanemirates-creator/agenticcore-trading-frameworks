"""
models.py – Normalized domain models for Crypto Standard Tier 1.

All models use explicit statuses and nullable fields.
No fabricated or estimated values are stored.
JSON serialization is dashboard-safe.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional


# ── Status enumerations ────────────────────────────────────────────────────────

class ReadinessStatus:
    PENDING = "pending"
    NOT_CONNECTED = "not_connected"
    UNKNOWN = "unknown"
    CONFIGURED = "configured"
    LIVE = "live"


class CandidateStatus:
    PENDING = "pending"
    SELECTED = "selected"
    REJECTED = "rejected"


class SignalStatus:
    PENDING = "pending"
    UNKNOWN = "unknown"
    LONG = "long"
    SHORT = "short"
    FLAT = "flat"


class EntryStatus:
    """Whether a candidate has a safe location to open a local paper trade."""
    UNKNOWN = "unknown"
    PENDING = "pending_entry"
    CONFIRMED = "confirmed"
    EXPIRED = "expired"
    REJECTED = "rejected"


class DailyGuardStatus:
    UNKNOWN = "unknown"
    ACTIVE = "active"
    LOSS_LIMIT_REACHED = "loss_limit_reached"
    PROFIT_TARGET_REACHED = "profit_target_reached"


class DataStatus:
    PENDING = "pending"
    NOT_CONNECTED = "not_connected"
    STALE = "stale"
    FRESH = "fresh"


class FrameworkMode:
    SIGNAL = "signal"
    PAPER = "paper"


# ── Data models ────────────────────────────────────────────────────────────────

@dataclass
class ContractDetail:
    """Normalized contract details from MEXC Futures public API."""
    symbol: str
    display_name: str
    base_coin: str
    quote_coin: str
    contract_size: Optional[float]        # value of one contract in base coin
    volume_step: Optional[float]          # minimum quantity increment
    min_quantity: Optional[float]
    max_quantity: Optional[float]
    price_precision: Optional[int]
    quantity_precision: Optional[int]
    is_active: bool
    fetched_at: Optional[str]             # ISO-8601
    contract_type: Optional[int] = None   # MEXC type; crypto perpetuals are type 1
    concept_plates: List[str] = field(default_factory=list)
    price_increment: Optional[float] = None


@dataclass
class Ticker:
    """Normalized 24h ticker snapshot."""
    symbol: str
    last_price: Optional[float]
    bid: Optional[float]
    ask: Optional[float]
    spread_pct: Optional[float]           # (ask-bid)/mid * 100, or None
    volume_24h: Optional[float]           # in contracts
    turnover_24h_usdt: Optional[float]    # in USDT
    change_pct_24h: Optional[float]
    fetched_at: Optional[str]


@dataclass
class Candle:
    """A single OHLCV candle."""
    symbol: str
    interval: str
    open_time: int                         # Unix ms
    open: float
    high: float
    low: float
    close: float
    volume: float
    is_complete: bool                      # False if candle is still forming


@dataclass
class FundingInfo:
    """Funding rate information."""
    symbol: str
    current_rate: Optional[float]
    next_rate: Optional[float]
    next_funding_time: Optional[str]       # ISO-8601
    fetched_at: Optional[str]


@dataclass
class OpenInterest:
    """Open interest snapshot."""
    symbol: str
    value_usdt: Optional[float]
    fetched_at: Optional[str]


@dataclass
class OrderBook:
    """Public MEXC depth snapshot; volumes remain exchange-reported contract units."""
    symbol: str
    bids: List[tuple[float, float]] = field(default_factory=list)
    asks: List[tuple[float, float]] = field(default_factory=list)
    fetched_at: Optional[str] = None


@dataclass
class RecentTrade:
    """One public MEXC fill. Side is the exchange aggressor-side classification."""
    symbol: str
    side: str
    price: Optional[float]
    quantity: Optional[float]
    timestamp_ms: Optional[int]


@dataclass
class Microstructure:
    """
    Deterministic public order-flow proxies.

    These describe exchange depth and recent trade pressure only. They do not
    identify wallets or claim that a particular whale acted.
    """
    buy_pressure_pct: Optional[float] = None
    order_book_imbalance_pct: Optional[float] = None
    large_trade_count: Optional[int] = None
    largest_trade_notional_usdt: Optional[float] = None
    fetched_at: Optional[str] = None


@dataclass
class MarketSnapshot:
    """
    Combined market data snapshot for one symbol.
    All fields are explicitly nullable; nothing is estimated.
    """
    symbol: str
    contract: Optional[ContractDetail]
    ticker: Optional[Ticker]
    candles: List[Candle] = field(default_factory=list)
    context_candles: List[Candle] = field(default_factory=list)
    funding: Optional[FundingInfo] = None
    open_interest: Optional[OpenInterest] = None
    mid_candles: List[Candle] = field(default_factory=list)
    order_book: Optional[OrderBook] = None
    recent_trades: List[RecentTrade] = field(default_factory=list)
    microstructure: Optional[Microstructure] = None
    data_status: str = DataStatus.PENDING
    data_error: Optional[str] = None


@dataclass
class Candidate:
    """A ranked futures candidate selected for the current cycle."""
    rank: int
    symbol: str
    selection_status: str = CandidateStatus.PENDING
    signal_status: str = SignalStatus.UNKNOWN
    data_status: str = DataStatus.PENDING
    risk_tier: str = "unassigned"
    confidence: Optional[int] = None          # 0–100, None means insufficient data
    planned_quantity: Optional[float] = None  # contracts, None until sized
    planned_side: Optional[str] = None        # "long" | "short" | None
    note: str = ""
    last_price: Optional[float] = None
    spread_pct: Optional[float] = None
    turnover_24h_usdt: Optional[float] = None
    funding_rate: Optional[float] = None
    oi_usdt: Optional[float] = None
    planned_margin_usdt: Optional[float] = None
    planned_stop_price: Optional[float] = None
    planned_take_profit_price: Optional[float] = None
    profit_lock_trigger_price: Optional[float] = None
    profit_lock_stop_price: Optional[float] = None
    planned_target_profit_usdt: Optional[float] = None
    contract_size: Optional[float] = None
    relative_volume: Optional[float] = None
    support_price: Optional[float] = None
    resistance_price: Optional[float] = None
    pullback_confirmed: Optional[bool] = None
    correlation_status: str = "unknown"
    support_zone_low: Optional[float] = None
    support_zone_high: Optional[float] = None
    support_zone_touches: Optional[int] = None
    resistance_zone_low: Optional[float] = None
    resistance_zone_high: Optional[float] = None
    resistance_zone_touches: Optional[int] = None
    entry_status: str = EntryStatus.UNKNOWN
    entry_zone_low: Optional[float] = None
    entry_zone_high: Optional[float] = None
    entry_invalidation_price: Optional[float] = None
    entry_expires_at: Optional[str] = None
    fake_reversal_detected: Optional[bool] = None
    opposing_zone_distance_pct: Optional[float] = None
    entry_structure_id: Optional[str] = None
    price_action_15m_pct: Optional[float] = None
    price_action_1h_pct: Optional[float] = None
    trend_1h: Optional[str] = None
    buy_pressure_pct: Optional[float] = None
    order_book_imbalance_pct: Optional[float] = None
    large_trade_count: Optional[int] = None
    largest_trade_notional_usdt: Optional[float] = None
    market_cap_usd: Optional[float] = None
    market_cap_rank: Optional[int] = None
    fully_diluted_valuation_usd: Optional[float] = None
    circulating_supply: Optional[float] = None
    cross_market_status: str = "unavailable"
    cross_market_agreement: str = "neutral"
    cross_market_adjustment: int = 0
    cross_market_evidence: Dict[str, Any] = field(default_factory=dict)


@dataclass
class MarketMover:
    """A liquidity-qualified 24-hour gainer or loser from MEXC."""
    symbol: str
    change_pct_24h: Optional[float]
    turnover_24h_usdt: Optional[float]
    spread_pct: Optional[float]


@dataclass
class FrameworkConfig:
    """Snapshot of active configuration for state persistence."""
    signal_mode: bool
    candidate_count: int
    max_open_positions: int
    position_notional_usdt: float
    max_isolated_margin_per_position_usdt: float
    daily_loss_limit_usdt: float
    daily_profit_target_usdt: float
    take_profit_usdt: float
    basket_profit_target_usdt: float
    stop_atr_period: int
    stop_atr_multiplier: float
    minimum_stop_pct: float
    maximum_stop_pct: float
    profit_lock_activation_pct: int
    profit_lock_protection_pct: int
    leverage_min: int
    leverage_max: int
    margin_mode: str


@dataclass
class FrameworkState:
    """
    Full runtime state written to runtime/state.json after each cycle.
    Explicitly null fields are safe for dashboard rendering.
    """
    schema_version: str = "1"
    exchange: str = "MEXC Futures"
    mode: str = FrameworkMode.SIGNAL
    market_data_status: str = ReadinessStatus.NOT_CONNECTED
    account_status: str = ReadinessStatus.UNKNOWN
    execution_status: str = ReadinessStatus.NOT_CONNECTED
    blockchain_status: str = ReadinessStatus.PENDING
    last_sync: Optional[str] = None
    config: Optional[FrameworkConfig] = None
    candidates: List[Candidate] = field(default_factory=list)
    open_positions: List[Any] = field(default_factory=list)   # always empty in Tier 1
    top_gainers: List[MarketMover] = field(default_factory=list)
    top_losers: List[MarketMover] = field(default_factory=list)
    market_context: Dict[str, Any] = field(default_factory=dict)
    paper_summary: Dict[str, Any] = field(default_factory=dict)
    scan_coverage: Dict[str, int] = field(default_factory=dict)
    radar: List[Dict[str, Any]] = field(default_factory=list)
    memory_status: str = "not_initialized"
    memory_last_update: Optional[str] = None
    memory_error: Optional[str] = None
    supervisor_status: str = ReadinessStatus.UNKNOWN
    last_error: Optional[str] = None
    cycle_count: int = 0
    daily_pnl_usdt: Optional[float] = None
    daily_guard_status: str = DailyGuardStatus.UNKNOWN

    def to_dict(self) -> Dict[str, Any]:
        """Return a dashboard-safe dict (explicit nulls, no fabricated values)."""
        d = asdict(self)
        return d


def pending_state(config: Optional[FrameworkConfig] = None) -> FrameworkState:
    """Return the safe default state when the worker has never run."""
    return FrameworkState(
        exchange="MEXC Futures",
        mode=FrameworkMode.SIGNAL,
        market_data_status=ReadinessStatus.NOT_CONNECTED,
        account_status=ReadinessStatus.UNKNOWN,
        execution_status=ReadinessStatus.NOT_CONNECTED,
        blockchain_status=ReadinessStatus.PENDING,
        last_sync=None,
        config=config,
        candidates=[],
        open_positions=[],
        last_error=None,
        cycle_count=0,
        daily_pnl_usdt=None,
        daily_guard_status=DailyGuardStatus.UNKNOWN,
    )
