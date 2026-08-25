"""
Premium Tier 1 — Deterministic Risk Guards.

All guards are synchronous, fast, and auditable.
The risk module can reject ANY signal independently of analysis.

Guards implemented:
1. Dubai entry window (05:00–23:29) — hard gate for new entries
2. Daily/session P&L baseline guard (15% loss / 20% profit lock)
3. Max portfolio positions
4. Max positions per pair (up to 3 with independent confirmation placeholder)
5. Spread hard block
6. Stale data hard block
7. Kill/pause state (circuit breaker)
8. Minimum confidence threshold
9. Volume gate for forex on LOW volume (configurable)

Identity-verified execution path requires validate_identity() on the bridge.
No order is ever placed by the risk module — it only approves or rejects.
"""
from __future__ import annotations
import logging
from datetime import datetime, time, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from config.settings import RiskConfig
from domain.models import RiskDecision

logger = logging.getLogger("premium.risk")

DUBAI_TZ = ZoneInfo("Asia/Dubai")
CORRELATION_GROUPS = (
    frozenset({"EURUSD", "GBPUSD", "AUDUSD", "NZDUSD"}),
    frozenset({"USDJPY", "USDCHF", "USDCAD"}),
    frozenset({"XAUUSD", "XAGUSD"}),
)


def _normalise_pair(pair: str) -> str:
    return "".join(char for char in pair.upper() if char.isalpha())[:6]


def _currencies(pair: str) -> tuple[str, str] | None:
    normalized = _normalise_pair(pair)
    if len(normalized) != 6:
        return None
    return normalized[:3], normalized[3:]


def _correlation_group(pair: str) -> frozenset[str] | None:
    normalized = _normalise_pair(pair)
    return next((group for group in CORRELATION_GROUPS if normalized in group), None)


class EntryWindowGuard:
    """
    Dubai entry window: new entries allowed only 05:00–23:29 Dubai time.
    This is a hard pre-entry gate. Management of open positions is unaffected.
    """

    def __init__(self, entry_start: str = "05:00", entry_stop: str = "23:29"):
        self._start = self._parse_time(entry_start)
        self._stop = self._parse_time(entry_stop)

    @staticmethod
    def _parse_time(t: str) -> time:
        h, m = t.split(":", 1)
        return time(int(h), int(m))

    def is_open(self, now: Optional[datetime] = None) -> bool:
        """Return True if current Dubai time is within the entry window."""
        if now is None:
            now = datetime.now(DUBAI_TZ)
        elif now.tzinfo is None:
            now = now.replace(tzinfo=DUBAI_TZ)
        else:
            now = now.astimezone(DUBAI_TZ)
        current = now.time().replace(tzinfo=None)
        return self._start <= current <= self._stop

    def session_key(self, now: Optional[datetime] = None) -> str:
        """Return the current trading session date string (Dubai)."""
        if now is None:
            now = datetime.now(DUBAI_TZ)
        elif now.tzinfo is None:
            now = now.replace(tzinfo=DUBAI_TZ)
        else:
            now = now.astimezone(DUBAI_TZ)
        current = now.time().replace(tzinfo=None)
        session_date = now.date()
        if current < self._start:
            session_date -= timedelta(days=1)
        return session_date.isoformat()


class RiskGuard:
    """
    Master risk guard for Premium Tier 1.

    evaluate_entry() runs ALL checks in order and returns the first rejection
    (fail-fast), or an allowed RiskDecision with all checks listed.

    Parameters
    ----------
    risk_cfg : RiskConfig
        Risk configuration from PremiumSettings.
    """

    def __init__(self, risk_cfg: RiskConfig):
        self.cfg = risk_cfg
        self.entry_window = EntryWindowGuard(
            risk_cfg.entry_window_start, risk_cfg.entry_window_stop
        )
        self._kill_switch = False
        self._pause = False

    def set_kill_switch(self, active: bool, reason: str = "") -> None:
        self._kill_switch = active
        if active:
            logger.warning(f"[Risk] Kill switch ACTIVATED: {reason}")
        else:
            logger.info("[Risk] Kill switch deactivated")

    def set_pause(self, active: bool, reason: str = "") -> None:
        self._pause = active
        if active:
            logger.warning(f"[Risk] Pause state set: {reason}")

    @property
    def circuit_breaker_active(self) -> bool:
        return self._kill_switch or self._pause

    def evaluate_entry(
        self,
        pair: str,
        direction: str,
        confidence: float,
        spread_pips: float,
        data_age_seconds: float,
        open_positions: list,
        session_pnl_pct: float,
        session_baseline_available: bool = True,
        volume_regime: str = "normal",
        is_metal: bool = False,
        independent_confirmation: bool = False,
        now: Optional[datetime] = None,
    ) -> RiskDecision:
        """
        Evaluate ALL risk conditions for a proposed new entry.

        Returns RiskDecision(allowed=False, reason=...) on first failure,
        or RiskDecision(allowed=True, checks=...) if all pass.

        No orders are placed here. The manager enforces signal_only mode.
        """
        checks: dict = {}

        # ── 1. Kill switch / circuit breaker ─────────────────────────────
        if self._kill_switch:
            return RiskDecision(
                allowed=False,
                reason="Kill switch is active — all new entries blocked",
                checks={"kill_switch": False},
            )
        checks["kill_switch"] = True

        if self._pause:
            return RiskDecision(
                allowed=False,
                reason="Worker is paused — no new entries",
                checks={"pause": False},
            )
        checks["pause"] = True

        # ── 2. Stale data hard block ──────────────────────────────────────
        if data_age_seconds > self.cfg.max_data_age_seconds:
            return RiskDecision(
                allowed=False,
                reason=f"Stale data: {data_age_seconds:.0f}s old (max {self.cfg.max_data_age_seconds:.0f}s)",
                checks={**checks, "stale_data": False},
            )
        checks["stale_data"] = True

        # ── 3. Spread hard block ──────────────────────────────────────────
        if spread_pips > self.cfg.max_spread_pips:
            return RiskDecision(
                allowed=False,
                reason=f"Spread too wide: {spread_pips:.2f} pips (max {self.cfg.max_spread_pips:.2f})",
                checks={**checks, "spread": False},
            )
        checks["spread"] = True

        # ── 4. Dubai entry window ─────────────────────────────────────────
        if not self.entry_window.is_open(now):
            return RiskDecision(
                allowed=False,
                reason="Outside Dubai entry window (05:00–23:29) — no new entries",
                checks={**checks, "entry_window": False},
            )
        checks["entry_window"] = True

        # ── 5. Daily P&L guards ───────────────────────────────────────────
        if not session_baseline_available:
            return RiskDecision(
                allowed=False,
                reason=(
                    "Session equity baseline is unavailable. New entries stay locked "
                    "until a durable Dubai-session baseline is captured."
                ),
                checks={**checks, "session_baseline": False},
            )
        checks["session_baseline"] = True

        if session_pnl_pct <= -self.cfg.daily_loss_limit_pct:
            return RiskDecision(
                allowed=False,
                reason=f"Daily loss limit reached ({session_pnl_pct:.2f}% ≤ -{self.cfg.daily_loss_limit_pct}%)",
                checks={**checks, "daily_loss_limit": False},
            )
        checks["daily_loss_limit"] = True

        if session_pnl_pct >= self.cfg.daily_profit_limit_pct:
            return RiskDecision(
                allowed=False,
                reason=f"Daily profit limit reached ({session_pnl_pct:.2f}% ≥ +{self.cfg.daily_profit_limit_pct}%)",
                checks={**checks, "daily_profit_limit": False},
            )
        checks["daily_profit_limit"] = True

        # ── 6. Max portfolio positions ────────────────────────────────────
        total_open = len(open_positions)
        if total_open >= self.cfg.max_portfolio_positions:
            return RiskDecision(
                allowed=False,
                reason=f"Max portfolio positions reached ({total_open}/{self.cfg.max_portfolio_positions})",
                checks={**checks, "max_portfolio_positions": False},
            )
        checks["max_portfolio_positions"] = True

        # ── 7. Max positions per pair ─────────────────────────────────────
        pair_positions = [
            p for p in open_positions
            if p.get("pair", p.get("symbol", "")) == pair
        ]
        n_pair = len(pair_positions)
        if n_pair >= self.cfg.max_positions_per_pair:
            return RiskDecision(
                allowed=False,
                reason=(
                    f"Max positions per pair reached: {pair} has {n_pair} "
                    f"(max {self.cfg.max_positions_per_pair}). "
                    "Independent confirmation required for 3rd entry."
                ),
                checks={**checks, "max_pair_positions": False},
            )
        checks["max_pair_positions"] = True

        # Same-pair scale-ins are fail-closed until independently confirmed.
        if n_pair >= 1 and not independent_confirmation:
            return RiskDecision(
                allowed=False,
                reason=(
                    f"{pair} already has {n_pair} open position(s). "
                    "A second or third entry requires independent confirmation."
                ),
                checks={**checks, "independent_confirmation": False},
            )
        checks["independent_confirmation"] = True if n_pair else "N/A"

        # ── 8. Currency and correlation exposure ──────────────────────────
        target_currencies = _currencies(pair)
        if target_currencies:
            for currency in target_currencies:
                exposure = sum(
                    1
                    for position in open_positions
                    if currency in (_currencies(position.get("pair", position.get("symbol", "")) or ""))
                )
                if exposure >= self.cfg.max_currency_exposure:
                    return RiskDecision(
                        allowed=False,
                        reason=(
                            f"Currency exposure limit reached for {currency} "
                            f"({exposure}/{self.cfg.max_currency_exposure})."
                        ),
                        checks={**checks, "currency_exposure": False},
                    )
        checks["currency_exposure"] = True

        group = _correlation_group(pair)
        if group:
            correlated = sum(
                1
                for position in open_positions
                if _normalise_pair(position.get("pair", position.get("symbol", ""))) in group
            )
            if correlated >= self.cfg.max_correlated_positions:
                return RiskDecision(
                    allowed=False,
                    reason=(
                        f"Correlated exposure limit reached for {pair} "
                        f"({correlated}/{self.cfg.max_correlated_positions})."
                    ),
                    checks={**checks, "correlation_exposure": False},
                )
        checks["correlation_exposure"] = True

        # ── 9. Minimum confidence ─────────────────────────────────────────
        if confidence < self.cfg.min_confidence:
            return RiskDecision(
                allowed=False,
                reason=(
                    f"Confidence {confidence:.1f} below minimum {self.cfg.min_confidence:.1f}"
                ),
                checks={**checks, "min_confidence": False},
            )
        checks["min_confidence"] = True

        # ── 10. Volume gate ───────────────────────────────────────────────
        vol_lower = volume_regime.lower()
        if vol_lower == "low":
            if not is_metal and self.cfg.gate_forex_on_low_volume:
                return RiskDecision(
                    allowed=False,
                    reason=(
                        f"Low volume gate: {pair} is forex with low participation. "
                        "Entry blocked (gate_forex_on_low_volume=True)."
                    ),
                    checks={**checks, "volume_gate": False},
                )
            elif is_metal and self.cfg.gate_metals_on_low_volume:
                return RiskDecision(
                    allowed=False,
                    reason=(
                        f"Low volume gate: {pair} is metal with low participation "
                        "and gate_metals_on_low_volume=True."
                    ),
                    checks={**checks, "volume_gate": False},
                )
            else:
                # Volume is low but not gated — log as context
                checks["volume_gate"] = "LOW_NOT_GATED"
                logger.info(
                    f"[Risk] {pair}: low volume — not gated "
                    f"(is_metal={is_metal}). Used as context only."
                )
        else:
            checks["volume_gate"] = True

        # ── All checks passed ─────────────────────────────────────────────
        return RiskDecision(
            allowed=True,
            reason="All risk checks passed",
            checks=checks,
        )
