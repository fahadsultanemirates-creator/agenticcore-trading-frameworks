"""
daily_guard.py – deterministic daily P&L guardrails.

The current Tier 1 worker has no private account/P&L feed, so an absent P&L is
explicitly UNKNOWN rather than treated as zero. Future paper/private runtimes
must pass the actual session P&L through this function before planning or
execution.
"""

from __future__ import annotations

from math import isfinite
from typing import Optional

from config.settings import CryptoSettings
from domain.models import DailyGuardStatus


def evaluate_daily_guard(
    daily_pnl_usdt: Optional[float],
    settings: CryptoSettings,
) -> str:
    """Return the guard status for the current session P&L."""
    if daily_pnl_usdt is None:
        return DailyGuardStatus.UNKNOWN
    if not isfinite(daily_pnl_usdt):
        raise ValueError("daily_pnl_usdt must be a finite number when provided.")
    if daily_pnl_usdt <= -settings.daily_loss_limit_usdt:
        return DailyGuardStatus.LOSS_LIMIT_REACHED
    if daily_pnl_usdt >= settings.daily_profit_target_usdt:
        return DailyGuardStatus.PROFIT_TARGET_REACHED
    return DailyGuardStatus.ACTIVE


def is_daily_guard_halted(status: str) -> bool:
    """Return whether no new paper/live position may be planned."""
    return status in {
        DailyGuardStatus.LOSS_LIMIT_REACHED,
        DailyGuardStatus.PROFIT_TARGET_REACHED,
    }