"""Entry safeguards shared by the manager, monitor, and Telegram approvals."""
from dataclasses import dataclass
from datetime import datetime, time, timedelta
import json
from pathlib import Path
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class EntryDecision:
    allowed: bool
    reason: str = ""
    changed: bool = False


def position_capacity_block_reason(settings, open_positions: list, pair: str,
                                  confidence: float) -> str:
    """Return a human-readable reason when a new trade cannot take a slot."""
    cfg = settings.get("trading_guard", {}) or {}
    normal_limit = int(cfg.get("normal_max_open_positions", 5))
    absolute_limit = int(cfg.get("max_open_positions", 7))
    exceptional_confidence = float(cfg.get("exceptional_min_confidence", 80))
    open_pairs = {p.get("pair", p.get("symbol", "")) for p in open_positions}

    if pair in open_pairs:
        return f"{pair} already has an open position"
    if len(open_positions) >= absolute_limit:
        return f"Maximum of {absolute_limit} open positions reached"
    if len(open_positions) >= normal_limit and confidence <= exceptional_confidence:
        return (
            f"Reserved positions require confidence above "
            f"{exceptional_confidence:.0f}%"
        )
    return ""


class TradingGuard:
    """Protect new entries without interfering with open-position management."""

    def __init__(self, settings):
        self.settings = settings
        self.state_path = Path(__file__).parent.parent / "trading_guard_state.json"

    def _config(self) -> dict:
        return self.settings.get("trading_guard", {}) or {}

    def _timezone(self) -> ZoneInfo:
        return ZoneInfo(self._config().get("timezone", "Asia/Dubai"))

    def _time(self, key: str, default: str) -> time:
        raw = str(self._config().get(key, default))
        hour, minute = raw.split(":", 1)
        return time(int(hour), int(minute))

    def _local_now(self, now: datetime | None = None) -> datetime:
        if now is None:
            return datetime.now(self._timezone())
        if now.tzinfo is None:
            return now.replace(tzinfo=self._timezone())
        return now.astimezone(self._timezone())

    def session_key(self, now: datetime | None = None) -> str:
        local = self._local_now(now)
        start = self._time("entry_start", "05:00")
        session_date = local.date()
        if local.timetz().replace(tzinfo=None) < start:
            session_date -= timedelta(days=1)
        return session_date.isoformat()

    def in_entry_window(self, now: datetime | None = None) -> bool:
        local = self._local_now(now)
        start = self._time("entry_start", "05:00")
        stop = self._time("entry_stop", "23:30")
        current = local.timetz().replace(tzinfo=None)
        return start <= current < stop

    def _load_session(self) -> dict:
        try:
            return json.loads(self.state_path.read_text())
        except (OSError, ValueError, TypeError):
            return {}

    def _save_session(self, state) -> None:
        data = {
            "session_key": state.trading_session_key,
            "session_start_equity": state.session_start_equity,
            "session_equity_pnl_pct": state.session_equity_pnl_pct,
            "daily_entry_lock_reason": state.daily_entry_lock_reason,
        }
        try:
            temp_path = self.state_path.with_suffix(".tmp")
            temp_path.write_text(json.dumps(data))
            temp_path.replace(self.state_path)
        except OSError as error:
            print(f"[Guard] Could not persist session baseline: {error}")

    def _can_capture_baseline(self, local: datetime) -> bool:
        start = self._time("entry_start", "05:00")
        current = local.timetz().replace(tzinfo=None)
        # The monitor normally evaluates every 15 seconds, so five minutes
        # provides restart tolerance without inventing a mid-session baseline.
        grace_end = (datetime.combine(local.date(), start) + timedelta(minutes=5)).time()
        return start <= current < grace_end

    def _restore_or_begin_session(self, state, session_key: str, equity: float,
                                  local: datetime) -> None:
        if state.trading_session_key == session_key:
            return

        saved = self._load_session()
        if (
            saved.get("session_key") == session_key
            and float(saved.get("session_start_equity") or 0) > 0
        ):
            state.begin_trading_session(session_key, float(saved["session_start_equity"]))
            state.session_equity_pnl_pct = float(saved.get("session_equity_pnl_pct") or 0)
            state.daily_entry_lock_reason = str(saved.get("daily_entry_lock_reason") or "")
            return

        if self._can_capture_baseline(local) and equity > 0:
            state.begin_trading_session(session_key, equity)
            self._save_session(state)
            return

        # A process launched after the 05:00 capture window cannot reliably
        # reconstruct earlier P&L, so it must wait for the next session.
        state.begin_trading_session(session_key, None)
        state.session_baseline_unavailable = True

    def evaluate(self, state, account: dict, now: datetime | None = None) -> EntryDecision:
        """Update session state and return whether a new position may be opened."""
        local = self._local_now(now)
        session_key = self.session_key(local)
        equity = float(account.get("equity") or account.get("balance") or 0)
        self._restore_or_begin_session(state, session_key, equity, local)

        baseline = state.session_start_equity
        if baseline and baseline > 0:
            state.session_equity_pnl_pct = round((equity - baseline) / baseline * 100, 3)

        cfg = self._config()
        if not state.daily_entry_lock_reason:
            if state.session_equity_pnl_pct <= -float(cfg.get("daily_loss_limit_pct", 15)):
                state.daily_entry_lock_reason = "Daily loss limit reached (15%)"
            elif state.session_equity_pnl_pct >= float(cfg.get("daily_profit_limit_pct", 5)):
                state.daily_entry_lock_reason = (
                    f"Daily profit limit reached (+{float(cfg.get('daily_profit_limit_pct', 5)):g}%)"
                )

        self._save_session(state)

        if state.session_baseline_unavailable:
            reason = "Daily session baseline unavailable; entries resume at 05:00 Dubai"
        elif state.daily_entry_lock_reason:
            reason = state.daily_entry_lock_reason
        elif not self.in_entry_window(local):
            reason = "New-entry window is closed (05:00–23:29 Dubai)"
        else:
            reason = ""

        changed = state.entry_block_reason != reason
        state.entry_block_reason = reason
        return EntryDecision(allowed=not reason, reason=reason, changed=changed)