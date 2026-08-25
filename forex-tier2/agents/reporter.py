"""
Agent 10 — Reporting Agent (Tier 2)
Logs trades, sends daily/weekly Telegram reports.
Identical to Tier 1 but log path updated for v2 and includes session info.
"""
import asyncio
import json
from datetime import datetime, date, timedelta, timezone
from pathlib import Path
from typing import Optional, Callable
from zoneinfo import ZoneInfo


LOG_PATH = Path(__file__).parent.parent / "logs" / "trades.jsonl"
DUBAI = ZoneInfo("Asia/Dubai")


def _append_trade(record: dict, idempotency_key: str = "") -> bool:
    LOG_PATH.parent.mkdir(exist_ok=True)
    if idempotency_key and LOG_PATH.exists():
        for prior in _read_trades():
            if prior.get("idempotency_key") == idempotency_key:
                return False
    if idempotency_key:
        record["idempotency_key"] = idempotency_key
    with open(LOG_PATH, "a") as f:
        f.write(json.dumps(record) + "\n")
    return True


def _read_trades(since_date: Optional[date] = None) -> list[dict]:
    if not LOG_PATH.exists():
        return []
    trades = []
    with open(LOG_PATH, "r") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                t = json.loads(line)
                if since_date:
                    timestamp = datetime.fromisoformat(t.get("time", "2000-01-01"))
                    if timestamp.tzinfo is None:
                        timestamp = timestamp.replace(tzinfo=timezone.utc)
                    t_date = timestamp.astimezone(DUBAI).date()
                    if t_date < since_date:
                        continue
                trades.append(t)
            except Exception:
                continue
    return trades


def _build_summary(trades: list[dict], period: str, state) -> str:
    if not trades:
        return f"*AgenticCore Forex PLUS — {period}*\nNo trades recorded."
    closed  = [t for t in trades if t.get("status") == "closed" or t.get("profit", 0) != 0]
    if not closed:
        return f"*AgenticCore Forex PLUS — {period}*\nNo closed trades yet."
    profits  = [t.get("profit", 0) for t in closed]
    wins     = [p for p in profits if p > 0]
    losses   = [p for p in profits if p < 0]
    total    = sum(profits)
    win_rate = len(wins) / len(closed) * 100 if closed else 0
    best     = max(profits) if profits else 0
    worst    = min(profits) if profits else 0

    pnl_emoji = "🟢" if total >= 0 else "🔴"
    lines = [
        f"*AgenticCore Forex PLUS — {period}*",
        f"{pnl_emoji} Net P&L: `${'+'if total>=0 else ''}{total:.2f}`",
        f"📊 Trades: `{len(closed)}` (✅ {len(wins)} / ❌ {len(losses)})",
        f"🎯 Win Rate: `{win_rate:.1f}%`",
        f"🏆 Best: `+${best:.2f}`  |  💸 Worst: `${worst:.2f}`",
    ]
    return "\n".join(lines)


class ReportingAgent:
    def __init__(self, settings, notify_fn: Optional[Callable] = None):
        self.settings                 = settings
        self.notify                   = notify_fn
        self._now                      = lambda: datetime.now(DUBAI)
        self._report_lock              = asyncio.Lock()
        self._last_daily_report_date: Optional[date]  = None
        self._last_weekly_report_week: Optional[int]  = None

    async def log_trade(self, trade_result: dict, signal_summary: str = "",
                        idempotency_key: str = "") -> bool:
        record = {
            "time":           datetime.utcnow().isoformat(),
            "pair":           trade_result.get("pair"),
            "direction":      trade_result.get("direction"),
            "lot":            trade_result.get("lot"),
            "ticket":         trade_result.get("ticket"),
            "price":          trade_result.get("price"),
            "sl":             trade_result.get("sl"),
            "tp":             trade_result.get("tp"),
            "profit":         trade_result.get("profit", 0),
            "status":         trade_result.get("status", "open"),
            "signal_summary": signal_summary,
        }
        return await asyncio.to_thread(_append_trade, record, idempotency_key)

    async def check_and_send_reports(self, state):
        async with self._report_lock:
            now = self._now()
            today = now.date()
            raw_time = self.settings.reporting.get("daily_summary_time", "23:45")
            report_hour, report_minute = (int(part) for part in str(raw_time).split(":", 1))
            due = (now.hour, now.minute) >= (report_hour, report_minute)

            if due and self._last_daily_report_date != today and self.notify:
                self._last_daily_report_date = today
                trades = await asyncio.to_thread(_read_trades, today)
                await self.notify(_build_summary(trades, f"Daily ({today.strftime('%d %b %Y')})", state))

            week = now.isocalendar()[1]
            if (now.weekday() == 0 and due and
                    self._last_weekly_report_week != week and self.notify):
                self._last_weekly_report_week = week
                week_start = today - timedelta(days=7)
                trades = await asyncio.to_thread(_read_trades, week_start)
                await self.notify(_build_summary(trades, f"Weekly (w/c {week_start.strftime('%d %b')})", state))

    async def get_report_message(self, period: str, state) -> str:
        if period == "today":
            today = datetime.now(DUBAI).date()
            trades = await asyncio.to_thread(_read_trades, today)
            return _build_summary(trades, f"Daily ({today.strftime('%d %b %Y')})", state)
        elif period == "week":
            today = datetime.now(DUBAI).date()
            week_start = today - timedelta(days=today.weekday())
            trades     = await asyncio.to_thread(_read_trades, week_start)
            return _build_summary(trades, "This Week", state)
        elif period == "all":
            trades = await asyncio.to_thread(_read_trades, None)
            return _build_summary(trades, "All Time", state)
        return "Unknown period. Use: today | week | all"
