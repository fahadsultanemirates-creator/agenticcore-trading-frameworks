"""Coin-specific stop, target, and isolated-margin calculations."""

from __future__ import annotations

from dataclasses import dataclass
from math import ceil, floor, isfinite
from typing import Iterable

from domain.models import Candle, ContractDetail
from risk.sizing import SizingError


def _completed(candles: Iterable[Candle]) -> list[Candle]:
    return [
        candle
        for candle in candles
        if candle.is_complete
        and candle.high > 0
        and candle.low > 0
        and candle.close > 0
    ]


def average_true_range(candles: Iterable[Candle], period: int) -> float:
    """Return ATR from completed candles only; never uses the forming candle."""
    completed = _completed(candles)
    if period < 1 or len(completed) < period + 1:
        raise SizingError("Insufficient completed candles for ATR stop calculation.")

    true_ranges: list[float] = []
    for previous, current in zip(completed, completed[1:]):
        true_ranges.append(
            max(
                current.high - current.low,
                abs(current.high - previous.close),
                abs(current.low - previous.close),
            )
        )
    atr = sum(true_ranges[-period:]) / period
    if not isfinite(atr) or atr <= 0:
        raise SizingError("ATR stop calculation produced an invalid value.")
    return atr


def derive_volatility_stop_price(
    side: str,
    entry_price: float,
    candles: Iterable[Candle],
    atr_period: int,
    atr_multiplier: float,
    minimum_stop_pct: float,
    maximum_stop_pct: float,
) -> float:
    """
    Derive a coin-specific, bounded hard stop.

    The stop is 1.5 ATR by default, never tighter than 0.2% and never wider
    than 2.0% of entry. This gives each coin room based on its own volatility
    while preventing an unreasonably long stop.
    """
    if side not in {"long", "short"}:
        raise SizingError(f"Unsupported trade side: {side!r}.")
    if not isfinite(entry_price) or entry_price <= 0:
        raise SizingError("entry_price must be a finite positive number.")
    if atr_multiplier <= 0 or minimum_stop_pct <= 0 or maximum_stop_pct <= 0:
        raise SizingError("ATR multiplier and stop percentage bounds must be positive.")
    if minimum_stop_pct > maximum_stop_pct:
        raise SizingError("minimum_stop_pct cannot exceed maximum_stop_pct.")

    atr_distance = average_true_range(candles, atr_period) * atr_multiplier
    min_distance = entry_price * minimum_stop_pct
    max_distance = entry_price * maximum_stop_pct
    stop_distance = min(max(atr_distance, min_distance), max_distance)

    stop_price = entry_price - stop_distance if side == "long" else entry_price + stop_distance
    if not isfinite(stop_price) or stop_price <= 0:
        raise SizingError("Derived stop price is invalid.")
    return stop_price


def quantize_price(price: float, contract: ContractDetail, direction: str) -> float:
    """
    Quantize to MEXC's price increment.

    `up` moves a long stop or target toward profit/entry; `down` moves a short
    stop or target toward profit/entry. Callers select the direction so a
    rounded protective level never weakens the intended risk or reward rule.
    """
    if direction not in {"up", "down"}:
        raise SizingError("Price quantization direction must be 'up' or 'down'.")
    if not isfinite(price) or price <= 0:
        raise SizingError("Price to quantize must be finite and positive.")

    increment = contract.price_increment
    if increment is None or not isfinite(increment) or increment <= 0:
        raise SizingError(
            f"Price increment is unknown for {contract.symbol}; "
            "MEXC priceUnit is required for a trade plan."
        )

    steps = price / increment
    rounded_steps = ceil(steps - 1e-12) if direction == "up" else floor(steps + 1e-12)
    quantized = rounded_steps * increment
    if not isfinite(quantized) or quantized <= 0:
        raise SizingError(f"Quantized price is invalid for {contract.symbol}.")
    return round(quantized, max(0, (contract.price_precision or 0) + 2))


def calculate_target_price(
    side: str,
    entry_price: float,
    quantity: float,
    contract: ContractDetail,
    target_profit_usdt: float,
) -> float:
    """Calculate the price that produces the fixed gross USDT target."""
    if side not in {"long", "short"}:
        raise SizingError(f"Unsupported trade side: {side!r}.")
    if not all(isfinite(value) and value > 0 for value in (entry_price, quantity, target_profit_usdt)):
        raise SizingError("Entry, quantity, and target profit must be finite positive values.")
    if contract.contract_size is None or not isfinite(contract.contract_size) or contract.contract_size <= 0:
        raise SizingError(f"contract_size is invalid for {contract.symbol}.")

    target_distance = target_profit_usdt / (quantity * contract.contract_size)
    target_price = entry_price + target_distance if side == "long" else entry_price - target_distance
    if not isfinite(target_price) or target_price <= 0:
        raise SizingError("Derived take-profit price is invalid.")
    return target_price


def calculate_estimated_isolated_margin(
    entry_price: float,
    quantity: float,
    contract: ContractDetail,
    leverage: int,
) -> float:
    """Estimate initial isolated margin; fees and funding are intentionally excluded."""
    if leverage < 1 or leverage > 20:
        raise SizingError("Leverage must remain within the Tier 1 1–20x range.")
    if contract.contract_size is None or contract.contract_size <= 0:
        raise SizingError(f"contract_size is invalid for {contract.symbol}.")
    if entry_price <= 0 or quantity <= 0:
        raise SizingError("Entry price and quantity must be positive.")
    margin = (entry_price * quantity * contract.contract_size) / leverage
    if not isfinite(margin) or margin <= 0:
        raise SizingError("Estimated isolated margin is invalid.")
    return margin