"""
sizing.py – Risk-based contract quantity calculator.

Uses stop-distance, contract size, volume step/min/max, leverage ceiling,
and margin mode to calculate a safe position size.

NEVER uses Forex lots. NEVER guesses on missing data.
Raises ValueError on any invalid, stale, or unknown input.
"""

from __future__ import annotations

import math
from typing import Optional

from domain.models import ContractDetail


# ── Public API ─────────────────────────────────────────────────────────────────

class SizingError(Exception):
    """Raised when position sizing cannot be completed safely."""


def calculate_quantity(
    entry_price: float,
    stop_price: float,
    risk_usdt: float,
    contract: ContractDetail,
    leverage: int,
    margin_mode: str = "isolated",
) -> float:
    """
    Calculate the number of contracts to trade given risk parameters.

    Parameters
    ----------
    entry_price   : float  – Intended entry price in USDT.
    stop_price    : float  – Hard stop-loss price in USDT.
    risk_usdt     : float  – Maximum USDT to risk (not total notional).
    contract      : ContractDetail – Must contain contract_size, volume_step,
                    min_quantity, max_quantity. Missing values raise SizingError.
    leverage      : int    – Applied leverage (must be <= 20 for Tier 1).
    margin_mode   : str    – Must be "isolated".

    Returns
    -------
    float – Calculated contract quantity, floored to the nearest volume_step.

    Raises
    ------
    SizingError on any invalid, missing, or unsafe input.
    """
    # ── Input validation ───────────────────────────────────────────────────────
    if margin_mode != "isolated":
        raise SizingError(
            f"margin_mode must be 'isolated' in Tier 1; got '{margin_mode}'."
        )
    if leverage < 1 or leverage > 20:
        raise SizingError(
            f"leverage {leverage} is outside the Tier 1 ceiling of 1–20x."
        )
    if entry_price is None or entry_price <= 0:
        raise SizingError(f"entry_price must be positive; got {entry_price!r}.")
    if stop_price is None or stop_price <= 0:
        raise SizingError(f"stop_price must be positive; got {stop_price!r}.")
    if entry_price == stop_price:
        raise SizingError("entry_price and stop_price must differ.")
    if risk_usdt <= 0:
        raise SizingError(f"risk_usdt must be positive; got {risk_usdt!r}.")

    # ── Contract validation ────────────────────────────────────────────────────
    if contract.contract_size is None:
        raise SizingError(
            f"contract_size is unknown for {contract.symbol}; cannot size position."
        )
    if contract.volume_step is None:
        raise SizingError(
            f"volume_step is unknown for {contract.symbol}; cannot size position."
        )
    if contract.min_quantity is None:
        raise SizingError(
            f"min_quantity is unknown for {contract.symbol}; cannot size position."
        )
    if contract.max_quantity is None:
        raise SizingError(
            f"max_quantity is unknown for {contract.symbol}; cannot size position."
        )

    contract_size = contract.contract_size
    volume_step = contract.volume_step
    min_qty = contract.min_quantity
    max_qty = contract.max_quantity

    if contract_size <= 0:
        raise SizingError(
            f"contract_size for {contract.symbol} is {contract_size}; expected positive."
        )
    if volume_step <= 0:
        raise SizingError(
            f"volume_step for {contract.symbol} is {volume_step}; expected positive."
        )
    if not all(math.isfinite(value) for value in (contract_size, volume_step, min_qty, max_qty)):
        raise SizingError(f"Contract limits for {contract.symbol} must be finite numbers.")
    if min_qty <= 0 or max_qty <= 0 or min_qty > max_qty:
        raise SizingError(
            f"Invalid quantity limits for {contract.symbol}: min={min_qty}, max={max_qty}."
        )

    # ── Sizing math ────────────────────────────────────────────────────────────
    # Stop distance in price terms
    stop_distance = abs(entry_price - stop_price)
    if stop_distance <= 0:
        raise SizingError(
            f"Derived stop distance is zero for {contract.symbol}."
        )

    # Value per contract per unit price move = contract_size (in base)
    # In USDT: value_per_contract_per_point = contract_size * 1 (since price is USDT)
    # Risk per contract = stop_distance * contract_size
    risk_per_contract_usdt = stop_distance * contract_size

    if risk_per_contract_usdt <= 0:
        raise SizingError(
            f"Computed risk_per_contract is zero or negative for {contract.symbol}."
        )

    # Raw quantity
    raw_qty = risk_usdt / risk_per_contract_usdt

    # Floor to volume_step increments
    steps = math.floor(raw_qty / volume_step)
    qty = steps * volume_step

    # Apply minimum
    if qty < min_qty:
        raise SizingError(
            f"Calculated quantity {qty} for {contract.symbol} is below "
            f"min_quantity {min_qty}. Increase risk or widen stop."
        )

    # Maximum must itself be aligned to the exchange increment. Never return
    # an exchange-invalid maximum just because the raw response was unaligned.
    max_steps = math.floor(max_qty / volume_step)
    max_aligned_qty = max_steps * volume_step
    if max_aligned_qty < min_qty:
        raise SizingError(
            f"Aligned maximum {max_aligned_qty} for {contract.symbol} is below "
            f"min_quantity {min_qty}."
        )
    qty = min(qty, max_aligned_qty)
    if qty < min_qty:
        raise SizingError(
            f"Aligned quantity {qty} for {contract.symbol} is below "
            f"min_quantity {min_qty}."
        )

    return round(qty, 8)


def calculate_quantity_for_notional(
    entry_price: float,
    notional_usdt: float,
    contract: ContractDetail,
    leverage: int,
    margin_mode: str = "isolated",
) -> float:
    """
    Calculate a contract quantity whose entry value never exceeds a fixed
    USDT position amount.

    This is deliberately independent from the stop distance: Tier 1 uses a
    fixed $50 position notional at 20x isolated leverage, then derives
    coin-specific protective levels around that position.
    """
    if margin_mode != "isolated":
        raise SizingError(
            f"margin_mode must be 'isolated' in Tier 1; got '{margin_mode}'."
        )
    if leverage < 1 or leverage > 20:
        raise SizingError(
            f"leverage {leverage} is outside the Tier 1 ceiling of 1–20x."
        )
    if entry_price is None or not math.isfinite(entry_price) or entry_price <= 0:
        raise SizingError(f"entry_price must be positive; got {entry_price!r}.")
    if notional_usdt is None or not math.isfinite(notional_usdt) or notional_usdt <= 0:
        raise SizingError(f"notional_usdt must be positive; got {notional_usdt!r}.")

    for name, value in (
        ("contract_size", contract.contract_size),
        ("volume_step", contract.volume_step),
        ("min_quantity", contract.min_quantity),
        ("max_quantity", contract.max_quantity),
    ):
        if value is None or not math.isfinite(value) or value <= 0:
            raise SizingError(f"{name} is invalid for {contract.symbol}.")
    if contract.min_quantity > contract.max_quantity:
        raise SizingError(
            f"Invalid quantity limits for {contract.symbol}: "
            f"min={contract.min_quantity}, max={contract.max_quantity}."
        )

    raw_quantity = notional_usdt / (entry_price * contract.contract_size)
    steps = math.floor(raw_quantity / contract.volume_step)
    quantity = steps * contract.volume_step
    max_steps = math.floor(contract.max_quantity / contract.volume_step)
    quantity = min(quantity, max_steps * contract.volume_step)

    if quantity < contract.min_quantity:
        raise SizingError(
            f"Fixed ${notional_usdt:.2f} notional is below the minimum "
            f"quantity for {contract.symbol}."
        )
    return round(quantity, 8)


def validate_leverage(leverage: int) -> None:
    """Raise SizingError if leverage exceeds Tier 1 ceiling."""
    if leverage > 20:
        raise SizingError(
            f"Requested leverage {leverage}x exceeds the Tier 1 ceiling of 20x."
        )
    if leverage < 1:
        raise SizingError(f"Leverage must be at least 1; got {leverage}.")
