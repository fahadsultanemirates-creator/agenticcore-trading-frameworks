"""
scanner.py – Broad USDT Futures universe filter.

Applies conservative thresholds to the full contract+ticker universe
to produce a reduced shortlist suitable for signal analysis.
No network calls; works on already-fetched data.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Set, Tuple

from config.settings import CryptoSettings
from domain.models import (
    ContractDetail,
    DataStatus,
    Ticker,
)


# ── Known non-USDT or exotic suffixes we skip ──────────────────────────────────
_EXCLUDED_SUFFIXES: Tuple[str, ...] = ("_USD", "USDC", "BTC", "ETH")
_NON_CRYPTO_CONCEPT_MARKERS: Tuple[str, ...] = (
    "stock",
    "tradfi",
    "metal",
    "commodit",
    "forex",
    "energy",
    "oil",
    "gas",
)


def _is_usdt_perp(symbol: str) -> bool:
    """Return True if the symbol looks like a USDT-settled perpetual future."""
    return symbol.endswith("_USDT") or symbol.endswith("USDT")


def _passes_symbol_filter(symbol: str) -> bool:
    """Quick symbol-level sanity checks."""
    if not _is_usdt_perp(symbol):
        return False
    for suffix in _EXCLUDED_SUFFIXES:
        if symbol.endswith(suffix) and not symbol.endswith("_USDT"):
            return False
    return True


def _is_crypto_contract(contract: ContractDetail) -> bool:
    """
    Fail closed unless MEXC classifies the instrument as a crypto perpetual.

    Symbols alone are not reliable: MEXC also lists equity and commodity
    perpetuals with the same USDT settlement suffix. A missing classification
    is treated as unknown rather than assumed to be crypto.
    """
    if contract.contract_type != 1 or not contract.concept_plates:
        return False
    concepts = " ".join(contract.concept_plates).lower()
    return not any(marker in concepts for marker in _NON_CRYPTO_CONCEPT_MARKERS)


def filter_universe(
    contracts: List[ContractDetail],
    tickers: Dict[str, Ticker],
    settings: CryptoSettings,
) -> List[str]:
    """
    Apply first-pass filters to produce a list of candidate symbols.

    Filters applied:
    1. Symbol must look like a USDT perpetual.
    2. Contract must be marked active.
    3. Ticker must be available; treat missing ticker as stale.
    4. Last price must be positive and not None.
    5. Spread must be within max_spread_pct.
    6. 24h turnover must meet min_turnover_usdt_24h.
    7. Limit output to scan_limit.

    Returns a list of symbols that passed all filters, unsorted.
    """
    passed: List[str] = []
    seen: Set[str] = set()

    for contract in contracts:
        if len(passed) >= settings.scan_limit:
            break

        symbol = contract.symbol
        if symbol in seen:
            continue
        seen.add(symbol)

        # Symbol filter
        if not _passes_symbol_filter(symbol):
            continue
        if not _is_crypto_contract(contract):
            continue

        # Contract active filter
        if not contract.is_active:
            continue

        # Ticker availability
        ticker = tickers.get(symbol)
        if ticker is None:
            continue

        # Price validity
        if ticker.last_price is None or ticker.last_price <= 0:
            continue

        # Spread filter — must be present and within limit
        if ticker.spread_pct is None:
            # No spread data: skip (unknown data lowers confidence, here we require it for scan)
            continue
        if ticker.spread_pct > settings.max_spread_pct:
            continue

        # Turnover filter
        if ticker.turnover_24h_usdt is None:
            continue
        if ticker.turnover_24h_usdt < settings.min_turnover_usdt_24h:
            continue

        passed.append(symbol)

    return passed


def rank_candidates(
    symbols: List[str],
    tickers: Dict[str, Ticker],
    limit: int,
) -> List[str]:
    """
    Rank filtered symbols by descending 24h USDT turnover and return top `limit`.
    Symbols without turnover data are sorted to the bottom.
    """
    def sort_key(sym: str) -> float:
        t = tickers.get(sym)
        if t is None or t.turnover_24h_usdt is None:
            return -1.0
        return t.turnover_24h_usdt

    ranked = sorted(symbols, key=sort_key, reverse=True)
    return ranked[:limit]
