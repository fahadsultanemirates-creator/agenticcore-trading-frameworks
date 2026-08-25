"""Explicit laptop-only private-account readiness check with no mutations."""

from __future__ import annotations

import os
from typing import Any, Dict

from adapters.mexc_private import MexcPrivateClient, MexcPrivateError
from config.settings import CryptoSettings


def run_readonly_account_check(settings: CryptoSettings) -> Dict[str, Any]:
    """
    Read balances, positions, and open orders through signed GET calls only.
    This function is never called by normal signal cycles.
    """
    if not settings.private_readonly_enabled:
        raise RuntimeError(
            "Private read-only mode is disabled. Set CRYPTO_PRIVATE_READONLY_ENABLED=true on the laptop only."
        )
    access_key = os.environ.get("MEXC_API_KEY", "").strip()
    secret_key = os.environ.get("MEXC_API_SECRET", "").strip()
    if not access_key or not secret_key:
        raise RuntimeError(
            "Read-only mode requires MEXC_API_KEY and MEXC_API_SECRET on the laptop."
        )
    client = MexcPrivateClient(
        access_key,
        secret_key,
        timeout=settings.request_timeout_seconds,
    )
    try:
        assets = client.get_account_assets()
        positions = client.get_open_positions()
        open_orders = client.get_open_orders()
    except MexcPrivateError as exc:
        raise RuntimeError(f"Read-only account check failed safely: {exc}") from exc
    usdt = next((asset for asset in assets if asset.currency == "USDT"), None)
    return {
        "status": "live",
        "asset_count": len(assets),
        "position_count": len(positions),
        "open_order_count": len(open_orders),
        "usdt_available": usdt.available_balance if usdt else None,
        "equity": usdt.cash_balance if usdt else None,
        "can_trade": None,
        "note": (
            "Read-only account inspection completed. MEXC's current Futures API "
            "does not expose a separate account-status endpoint; no order endpoint "
            "exists in this framework."
        ),
    }