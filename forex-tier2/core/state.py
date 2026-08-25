"""
Shared mutable state for all agents and the Telegram bot.
"""
import asyncio
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class PendingTrade:
    """A trade waiting for /approve or /reject in semi-auto mode."""
    pair: str
    direction: str
    confidence: float
    lot_size: float
    sl_pips: float   # pips distance (not price level)
    tp_pips: float   # pips distance (not price level)
    signal_summary: str


class SharedState:
    def __init__(self):
        self.mode: str = "auto"
        self.trading_active: bool = True
        self.daily_pnl_usd: float = 0.0
        self.daily_pnl_date = None
        self.open_positions: list = []
        self.pending_trade: Optional[PendingTrade] = None
        self.last_scan_time: Optional[str] = None
        self.total_trades_today: int = 0
        self.wins_today: int = 0
        self.losses_today: int = 0
        self.basket_close_in_progress: bool = False
        self.closed_by_framework_tickets: set[int] = set()
        self.pending_broker_closures: dict[int, dict] = {}
        self.accounted_close_tickets: set[int] = set()
        self._lock = asyncio.Lock()
        self.circuit_breaker_active: bool = False
        # Tier 2 extras
        self.active_sessions: list[str] = []
        self.session_bonus: int = 0
        self.scan_count: int = 0
        self.last_session_alert: Optional[str] = None
        # Entry safeguards use a Dubai 05:00-to-05:00 trading session.
        self.trading_session_key: Optional[str] = None
        self.session_start_equity: Optional[float] = None
        self.session_equity_pnl_pct: float = 0.0
        self.session_baseline_unavailable: bool = False
        self.daily_entry_lock_reason: str = ""
        self.entry_block_reason: str = ""
        self.session_profit_basket_close_attempted: bool = False
        self.session_profit_basket_close_requested: bool = False

    def begin_trading_session(self, session_key: str, equity: float | None):
        """Reset entry limits and daily statistics at 05:00 Dubai time."""
        if self.trading_session_key != session_key:
            self.daily_pnl_usd = 0.0
            self.daily_pnl_date = session_key
            self.trading_session_key = session_key
            self.session_start_equity = equity if equity and equity > 0 else None
            self.session_equity_pnl_pct = 0.0
            self.session_baseline_unavailable: bool = False
            self.total_trades_today = 0
            self.wins_today = 0
            self.losses_today = 0
            self.circuit_breaker_active = False
            self.daily_entry_lock_reason = ""
            self.session_profit_basket_close_attempted = False
            self.session_profit_basket_close_requested = False

    def record_trade_result(self, profit_usd: float):
        self.daily_pnl_usd += profit_usd
        self.total_trades_today += 1
        if profit_usd >= 0:
            self.wins_today += 1
        else:
            self.losses_today += 1

    def record_trade_result_once(self, ticket: int, profit_usd: float) -> bool:
        if ticket in self.accounted_close_tickets:
            return False
        self.accounted_close_tickets.add(ticket)
        self.record_trade_result(profit_usd)
        return True

    @property
    def win_rate_today(self) -> float:
        if self.total_trades_today == 0:
            return 0.0
        return round(self.wins_today / self.total_trades_today * 100, 1)

    def status_summary(self) -> str:
        status = "🟢 ACTIVE" if self.trading_active and not self.entry_block_reason else "🟠 ENTRY LOCKED"
        if not self.trading_active:
            status = "🔴 PAUSED"
        sessions = ", ".join(self.active_sessions) if self.active_sessions else "None (dead zone)"
        entry_reason = self.entry_block_reason or "Open"
        return (
            f"*AgenticCore Forex PLUS — Status*\n"
            f"Mode: `{self.mode.upper()}`  |  {status}\n"
            f"Daily P&L: `{'+'if self.daily_pnl_usd>=0 else ''}${self.daily_pnl_usd:.2f}`\n"
            f"Trades today: `{self.total_trades_today}` "
            f"(✅ {self.wins_today} / ❌ {self.losses_today})\n"
            f"Win rate: `{self.win_rate_today}%`\n"
            f"Open positions: `{len(self.open_positions)}`\n"
            f"Session equity: `{'+' if self.session_equity_pnl_pct >= 0 else ''}{self.session_equity_pnl_pct:.2f}%`\n"
            f"New entries: `{entry_reason}`\n"
            f"Active sessions: `{sessions}`\n"
            f"Session bonus: `{self.session_bonus:+d}` conf pts\n"
            f"Last scan: `{self.last_scan_time or 'Not yet run'}`"
        )
