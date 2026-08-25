"""Deterministic 65%-progress / 35%-profit protection levels."""

from __future__ import annotations

from dataclasses import dataclass
from math import isfinite

from risk.sizing import SizingError


@dataclass(frozen=True)
class ProfitLockLevels:
    activation_price: float
    protected_stop_price: float


def calculate_profit_lock_levels(
    side: str,
    entry_price: float,
    take_profit_price: float,
    activation_pct: int = 65,
    protection_pct: int = 35,
) -> ProfitLockLevels:
    """
    At 65% progress toward the original target, move the protective stop to
    secure 35% of the original target distance.
    """
    if side not in {"long", "short"}:
        raise SizingError(f"Unsupported trade side: {side!r}.")
    if not all(isfinite(value) and value > 0 for value in (entry_price, take_profit_price)):
        raise SizingError("Entry and take-profit prices must be finite positive values.")
    if not 0 < activation_pct < 100 or not 0 < protection_pct < 100:
        raise SizingError("Profit-lock percentages must be between 1 and 99.")

    distance = abs(take_profit_price - entry_price)
    if distance <= 0:
        raise SizingError("Take-profit price must differ from entry price.")
    if (side == "long" and take_profit_price <= entry_price) or (
        side == "short" and take_profit_price >= entry_price
    ):
        raise SizingError("Take-profit direction does not match trade side.")

    sign = 1 if side == "long" else -1
    return ProfitLockLevels(
        activation_price=entry_price + sign * distance * (activation_pct / 100),
        protected_stop_price=entry_price + sign * distance * (protection_pct / 100),
    )


def should_activate_profit_lock(side: str, current_price: float, levels: ProfitLockLevels) -> bool:
    """Return whether the current price has reached the profit-lock trigger."""
    if not isfinite(current_price) or current_price <= 0:
        return False
    return (
        current_price >= levels.activation_price
        if side == "long"
        else current_price <= levels.activation_price
    )