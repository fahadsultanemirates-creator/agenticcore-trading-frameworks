"""Guarded MEXC Futures execution client for an explicitly confirmed live canary.

This module is intentionally separate from the read-only private client. It is
never imported by normal scanner or paper-trading paths.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional, Tuple

from adapters.mexc_private import (
    DEFAULT_RECV_WINDOW,
    MEXC_FUTURES_BASE,
    MexcCredentialError,
    MexcPrivateError,
    _build_signature,
    _unwrap,
)

MEXC_FUTURES_EXECUTION_BASE = "https://api.mexc.com"


HttpPostFn = Callable[[str, Dict[str, str], str, int], Tuple[int, Dict[str, Any]]]


def default_http_post(
    url: str,
    headers: Dict[str, str],
    payload: str,
    timeout_seconds: int = 10,
) -> Tuple[int, Dict[str, Any]]:
    """Send a JSON POST and return the parsed MEXC response envelope."""
    request = urllib.request.Request(
        url,
        data=payload.encode("utf-8"),
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "AgenticCore-LiveCanary/1.0",
            **headers,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            body = response.read().decode("utf-8", errors="replace")
            status = response.status
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        status = exc.code

    try:
        data = json.loads(body)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Non-JSON response (status={status}): {body[:200]}") from exc
    return status, data


@dataclass(frozen=True)
class SubmittedOrder:
    order_id: str
    external_oid: str
    submitted_at_ms: int


class MexcExecutionClient:
    """
    Signed POST-only order client for a deliberately constrained live canary.

    The caller is responsible for all strategy, balance, duplicate-position,
    and explicit-confirmation checks. This client only permits isolated 20x
    opening orders that include both stop-loss and take-profit values.
    """

    def __init__(
        self,
        access_key: str,
        secret_key: str,
        http_post: Optional[HttpPostFn] = None,
        timeout: int = 10,
        base_url: str = MEXC_FUTURES_EXECUTION_BASE,
        recv_window: int = DEFAULT_RECV_WINDOW,
    ) -> None:
        if not access_key or not access_key.strip():
            raise MexcCredentialError("trading access key must not be empty")
        if not secret_key or not secret_key.strip():
            raise MexcCredentialError("trading secret key must not be empty")
        self.__access_key = access_key.strip()
        self.__secret_key = secret_key.strip()
        self._post_transport = http_post or default_http_post
        self._timeout = timeout
        self._base = base_url.rstrip("/")
        self._recv_window = recv_window

    def __repr__(self) -> str:
        return f"MexcExecutionClient(base={self._base!r})"

    def _post(self, path: str, params: Dict[str, Any]) -> Any:
        clean_params = {key: value for key, value in params.items() if value is not None}
        payload = json.dumps(clean_params, separators=(",", ":"), ensure_ascii=False)
        timestamp = int(time.time() * 1000)
        signature = _build_signature(
            self.__secret_key,
            self.__access_key,
            timestamp,
            payload,
        )
        headers = {
            "ApiKey": self.__access_key,
            "Request-Time": str(timestamp),
            "Signature": signature,
            "Recv-Window": str(self._recv_window),
        }
        status, body = self._post_transport(
            f"{self._base}{path}",
            headers,
            payload,
            self._timeout,
        )
        if not isinstance(body, dict):
            raise MexcPrivateError(
                f"Unexpected response type from {path}: {type(body).__name__}",
                raw=None,
            )
        if status == 401:
            raise MexcCredentialError(
                f"HTTP 401 from {path}: authentication rejected",
                raw=None,
            )
        if status != 200:
            code = body.get("code")
            raise MexcPrivateError(
                f"HTTP {status} from {path} (code={code})",
                raw=None,
            )
        return _unwrap(body, path)

    def submit_protected_market_entry(
        self,
        *,
        symbol: str,
        side: str,
        quantity: float,
        reference_price: float,
        stop_loss_price: float,
        take_profit_price: float,
        external_oid: str,
    ) -> SubmittedOrder:
        """
        Submit one 20x isolated market entry with both hard protections included.

        MEXC documents stopLossPrice/takeProfitPrice on the same create-order
        request, so an accepted order is never intentionally submitted naked.
        """
        if side not in {"long", "short"}:
            raise ValueError("side must be 'long' or 'short'")
        if not symbol or quantity <= 0 or reference_price <= 0:
            raise ValueError("symbol, quantity, and reference_price must be positive")
        if stop_loss_price <= 0 or take_profit_price <= 0:
            raise ValueError("Both stop-loss and take-profit prices are required.")
        if side == "long" and not (stop_loss_price < reference_price < take_profit_price):
            raise ValueError("Long protection must be stop < entry < target.")
        if side == "short" and not (take_profit_price < reference_price < stop_loss_price):
            raise ValueError("Short protection must be target < entry < stop.")

        data = self._post(
            "/api/v1/private/order/create",
            {
                "symbol": symbol,
                "price": reference_price,
                "vol": quantity,
                "leverage": 20,
                "side": 1 if side == "long" else 3,
                "type": 5,
                "openType": 1,
                "externalOid": external_oid,
                "stopLossPrice": stop_loss_price,
                "takeProfitPrice": take_profit_price,
                "lossTrend": 1,
                "profitTrend": 1,
                "priceProtect": 0,
                "positionMode": 1,
            },
        )
        if not isinstance(data, dict) or not data.get("orderId"):
            raise MexcPrivateError(
                "/api/v1/private/order/create: missing orderId in success response",
                raw=None,
            )
        submitted_at = data.get("ts")
        try:
            submitted_at_ms = int(submitted_at)
        except (TypeError, ValueError):
            submitted_at_ms = int(time.time() * 1000)
        return SubmittedOrder(
            order_id=str(data["orderId"]),
            external_oid=external_oid,
            submitted_at_ms=submitted_at_ms,
        )

    def close_market_position(
        self,
        *,
        symbol: str,
        position_id: str,
        side: str,
        quantity: float,
        reference_price: float,
        external_oid: str,
    ) -> SubmittedOrder:
        """Emergency-only market close for a known canary position."""
        if side not in {"long", "short"}:
            raise ValueError("side must be 'long' or 'short'")
        if not symbol or not position_id or quantity <= 0 or reference_price <= 0:
            raise ValueError("Emergency close requires a known position and positive quantity.")
        data = self._post(
            "/api/v1/private/order/create",
            {
                "symbol": symbol,
                "price": reference_price,
                "vol": quantity,
                "side": 4 if side == "long" else 2,
                "type": 5,
                "openType": 1,
                "positionId": position_id,
                "externalOid": external_oid,
                "positionMode": 1,
            },
        )
        if not isinstance(data, dict) or not data.get("orderId"):
            raise MexcPrivateError(
                "/api/v1/private/order/create: missing orderId in close response",
                raw=None,
            )
        return SubmittedOrder(
            order_id=str(data["orderId"]),
            external_oid=external_oid,
            submitted_at_ms=int(data.get("ts") or time.time() * 1000),
        )

    def change_tpsl_plan(
        self,
        *,
        stop_plan_order_id: str,
        stop_loss_price: float,
        take_profit_price: float,
    ) -> None:
        """Move the canary stop while preserving its original take-profit."""
        if not stop_plan_order_id or stop_loss_price <= 0 or take_profit_price <= 0:
            raise ValueError("TP/SL plan id and positive prices are required.")
        self._post(
            "/api/v1/private/stoporder/change_plan_price",
            {
                "stopPlanOrderId": stop_plan_order_id,
                "stopLossPrice": stop_loss_price,
                "takeProfitPrice": take_profit_price,
                "lossTrend": 1,
                "profitTrend": 1,
            },
        )