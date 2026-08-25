"""
mexc_private.py – MEXC Futures private (signed) read-only API adapter.

LAPTOP-ONLY / READ-ONLY: credentials must be injected; no order submission,
no cancel, no leverage mutation, no withdrawals, no execution paths.

Supported endpoints (MEXC Contract API):
  - GET /api/v1/private/account/assets          account asset list
  - GET /api/v1/private/account/asset/{currency} single asset balance
  - GET /api/v1/private/position/open_positions  open positions
  - GET /api/v1/private/order/list/open_orders   current open orders

Authentication: HMAC-SHA256 over (access_key + timestamp + params)
as required by the MEXC Futures API.

Transport is dependency-injected; pass a callable matching the HttpGetFn
signature to make this fully testable without network access.

Credentials are never logged or included in exception messages.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

# ── Constants ──────────────────────────────────────────────────────────────────

MEXC_FUTURES_BASE = "https://contract.mexc.com"

# Optional receive window sent as a request header. It is not part of the
# MEXC Futures signature payload.
DEFAULT_RECV_WINDOW = 10_000

# Maximum allowed timestamp drift vs server time (ms) – conservative guard
MAX_CLOCK_DRIFT_MS = 30_000


# ── Transport type alias ───────────────────────────────────────────────────────

# (url, headers, timeout_seconds) -> (status_code, parsed_json_body)
HttpGetFn = Callable[[str, Dict[str, str], int], Tuple[int, Dict[str, Any]]]


def default_http_get(
    url: str,
    headers: Dict[str, str],
    timeout_seconds: int = 10,
) -> Tuple[int, Dict[str, Any]]:
    """
    Perform a signed GET and return (status_code, parsed_json_body).
    Raises ValueError on non-JSON body.
    Raises urllib.error.URLError on network failure.
    """
    req = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "AgenticCore-Tier1/1.0",
            **headers,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_seconds) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            status = resp.status
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        status = exc.code

    try:
        data = json.loads(body)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Non-JSON response (status={status}): {body[:200]}") from exc
    return status, data


# ── Domain dataclasses (private API) ──────────────────────────────────────────

@dataclass
class AccountAsset:
    """One asset entry from the account assets endpoint."""
    currency: str
    position_margin: Optional[float]       # margin tied to open positions
    available_balance: Optional[float]     # free balance
    cash_balance: Optional[float]          # total wallet balance
    unrealised_pnl: Optional[float]
    fetched_at: str


@dataclass
class OpenPosition:
    """Normalized open futures position (read-only snapshot)."""
    position_id: Optional[str]
    symbol: str
    side: Optional[str]                    # "1" long / "2" short (MEXC convention)
    hold_vol: Optional[float]              # position size in contracts
    open_price: Optional[float]
    mark_price: Optional[float]
    unrealised_pnl: Optional[float]
    leverage: Optional[int]
    margin_type: Optional[int]             # 1=isolated, 2=cross
    margin: Optional[float]
    fetched_at: str


@dataclass
class OpenOrder:
    """Normalized open order snapshot (read-only)."""
    order_id: Optional[str]
    symbol: str
    side: Optional[int]
    price: Optional[float]
    vol: Optional[float]                   # order quantity in contracts
    deal_avg_price: Optional[float]
    deal_vol: Optional[float]
    order_type: Optional[int]
    state: Optional[int]
    create_time: Optional[str]             # ISO-8601
    fetched_at: str


# ── Exceptions ─────────────────────────────────────────────────────────────────

class MexcPrivateError(Exception):
    """Raised for any private-API error condition."""

    def __init__(self, msg: str, code: Optional[int] = None, raw: Any = None) -> None:
        super().__init__(msg)
        self.code = code
        self.raw = raw


class MexcCredentialError(MexcPrivateError):
    """Raised when credentials are absent or rejected by the server."""


class MexcClockError(MexcPrivateError):
    """Raised when the local clock is too far from server time."""


# ── Helpers ────────────────────────────────────────────────────────────────────

def _safe_float(v: Any) -> Optional[float]:
    if v is None:
        return None
    try:
        f = float(v)
        return f if f == f else None  # NaN guard
    except (ValueError, TypeError):
        return None


def _safe_int(v: Any) -> Optional[int]:
    if v is None:
        return None
    try:
        return int(v)
    except (ValueError, TypeError):
        return None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ms_to_iso(ms: Any) -> Optional[str]:
    v = _safe_int(ms)
    if v is None:
        return None
    try:
        return datetime.fromtimestamp(v / 1000, tz=timezone.utc).isoformat()
    except (OSError, OverflowError, ValueError):
        return None


def _unwrap(resp: Dict[str, Any], endpoint: str) -> Any:
    """
    MEXC contract API envelope:
      success=true  → return data field
      success=false → raise MexcPrivateError

    Error code conventions relevant to read-only:
      602 / 10001 / 10010 / 401 – signature / auth errors → MexcCredentialError
      20001 – timestamp too far → MexcClockError
    """
    success = resp.get("success")
    if success is False or success == 0:
        code = _safe_int(resp.get("code"))
        # Never include raw message in exception text (may contain hints about key)
        if code in (602, 10001, 10010, 401, 40001, 40003):
            raise MexcCredentialError(
                f"{endpoint}: authentication/signature error (code={code})",
                code=code,
                raw=None,  # suppress raw to keep creds out of traces
            )
        if code in (20001, 429):
            raise MexcClockError(
                f"{endpoint}: timestamp/rate error (code={code})",
                code=code,
                raw=None,
            )
        raise MexcPrivateError(
            f"{endpoint}: API rejected request (code={code})",
            code=code,
            raw=None,
        )
    if "data" not in resp:
        return resp
    return resp["data"]


def _build_signature(
    secret_key: str,
    access_key: str,
    timestamp: int,
    param_str: str,
) -> str:
    """
    MEXC Futures HMAC-SHA256 signature.

    The MEXC Futures integration guide defines the signed text as the access
    key, request timestamp, and sorted business-parameter string. Recv-Window
    is an optional header and must not be included in that signed text.
    """
    to_sign = f"{access_key}{timestamp}{param_str}"
    return hmac.new(
        secret_key.encode("utf-8"),
        to_sign.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


# ── Client ─────────────────────────────────────────────────────────────────────

class MexcPrivateClient:
    """
    Read-only MEXC Futures private data client.

    Only account inspection methods are exposed.  There are deliberately no
    methods for order placement, cancellation, leverage changes, or withdrawals.

    Credentials are injected at construction and never written to logs.
    """

    def __init__(
        self,
        access_key: str,
        secret_key: str,
        http_get: Optional[HttpGetFn] = None,
        timeout: int = 10,
        base_url: str = MEXC_FUTURES_BASE,
        recv_window: int = DEFAULT_RECV_WINDOW,
    ) -> None:
        if not access_key or not access_key.strip():
            raise MexcCredentialError("access_key must not be empty")
        if not secret_key or not secret_key.strip():
            raise MexcCredentialError("secret_key must not be empty")

        # Store but never expose in repr/str
        self.__access_key = access_key
        self.__secret_key = secret_key
        self._get = http_get or default_http_get
        self._timeout = timeout
        self._base = base_url.rstrip("/")
        self._recv_window = recv_window

    def __repr__(self) -> str:
        return f"MexcPrivateClient(base={self._base!r})"

    # ── Internal signed request ────────────────────────────────────────────────

    def _fetch(self, path: str, params: Optional[Dict[str, Any]] = None) -> Any:
        """
        Build a signed GET request, execute it, and return unwrapped data.

        Raises:
          MexcCredentialError – missing/invalid credentials
          MexcClockError      – timestamp drift / rate limit
          MexcPrivateError    – any other API error or non-200
        """
        timestamp = int(time.time() * 1000)

        param_str = ""
        if params:
            # Sort keys for deterministic signing
            param_str = urllib.parse.urlencode(
                sorted((str(k), str(v)) for k, v in params.items() if v is not None)
            )

        sig = _build_signature(
            self.__secret_key,
            self.__access_key,
            timestamp,
            param_str,
        )

        headers = {
            "ApiKey": self.__access_key,
            "Request-Time": str(timestamp),
            "Signature": sig,
            "Recv-Window": str(self._recv_window),
        }

        url = f"{self._base}{path}"
        if param_str:
            url = f"{url}?{param_str}"

        status, body = self._get(url, headers, self._timeout)

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
            # Extract code without exposing raw body
            code = _safe_int(body.get("code")) if isinstance(body, dict) else None
            raise MexcPrivateError(
                f"HTTP {status} from {path} (code={code})",
                code=code,
                raw=None,
            )

        return _unwrap(body, path)

    # ── Account assets ─────────────────────────────────────────────────────────

    def get_account_assets(self) -> List[AccountAsset]:
        """
        Return all asset balances for the futures account.
        Endpoint: GET /api/v1/private/account/assets
        """
        data = self._fetch("/api/v1/private/account/assets")

        if not isinstance(data, list):
            raise MexcPrivateError(
                "/api/v1/private/account/assets: expected list, got "
                f"{type(data).__name__}",
                raw=None,
            )

        fetched_at = _now_iso()
        assets: List[AccountAsset] = []
        for item in data:
            if not isinstance(item, dict):
                continue
            currency = str(item.get("currency") or item.get("coin") or "").upper()
            if not currency:
                continue
            assets.append(
                AccountAsset(
                    currency=currency,
                    position_margin=_safe_float(
                        item.get("positionMargin") or item.get("frozenMargin")
                    ),
                    available_balance=_safe_float(
                        item.get("availableBalance") or item.get("available")
                    ),
                    cash_balance=_safe_float(
                        item.get("cashBalance") or item.get("equity")
                    ),
                    unrealised_pnl=_safe_float(
                        item.get("unrealizedProfit") or item.get("unrealisedPnl")
                    ),
                    fetched_at=fetched_at,
                )
            )
        return assets

    # ── Single asset balance ───────────────────────────────────────────────────

    def get_asset_balance(self, currency: str) -> AccountAsset:
        """
        Return the balance for a single currency (e.g. "USDT").
        Endpoint: GET /api/v1/private/account/asset/{currency}
        """
        if not currency or not currency.strip():
            raise MexcPrivateError("currency must not be empty")

        currency = currency.strip().upper()
        path = f"/api/v1/private/account/asset/{urllib.parse.quote(currency)}"
        data = self._fetch(path)

        if not isinstance(data, dict):
            raise MexcPrivateError(
                f"{path}: expected dict, got {type(data).__name__}",
                raw=None,
            )

        fetched_at = _now_iso()
        return AccountAsset(
            currency=str(data.get("currency") or data.get("coin") or currency).upper(),
            position_margin=_safe_float(
                data.get("positionMargin") or data.get("frozenMargin")
            ),
            available_balance=_safe_float(
                data.get("availableBalance") or data.get("available")
            ),
            cash_balance=_safe_float(
                data.get("cashBalance") or data.get("equity")
            ),
            unrealised_pnl=_safe_float(
                data.get("unrealizedProfit") or data.get("unrealisedPnl")
            ),
            fetched_at=fetched_at,
        )

    # ── Open positions ─────────────────────────────────────────────────────────

    def get_open_positions(self, symbol: Optional[str] = None) -> List[OpenPosition]:
        """
        Return all open positions (optionally filtered by symbol).
        Endpoint: GET /api/v1/private/position/open_positions
        """
        params: Dict[str, Any] = {}
        if symbol:
            params["symbol"] = symbol.strip().upper()

        data = self._fetch("/api/v1/private/position/open_positions", params or None)

        if not isinstance(data, list):
            raise MexcPrivateError(
                "/api/v1/private/position/open_positions: expected list, got "
                f"{type(data).__name__}",
                raw=None,
            )

        fetched_at = _now_iso()
        positions: List[OpenPosition] = []
        for item in data:
            if not isinstance(item, dict):
                continue
            sym = str(item.get("symbol") or "").upper()
            if not sym:
                continue
            positions.append(
                OpenPosition(
                    position_id=str(item["positionId"]) if item.get("positionId") is not None else None,
                    symbol=sym,
                    side=str(item.get("positionType") or item.get("side") or ""),
                    hold_vol=_safe_float(item.get("holdVol")),
                    open_price=_safe_float(item.get("openAvgPrice") or item.get("entryPrice")),
                    mark_price=_safe_float(item.get("markPrice")),
                    unrealised_pnl=_safe_float(
                        item.get("unrealizedProfit") or item.get("unrealisedPnl")
                    ),
                    leverage=_safe_int(item.get("leverage")),
                    margin_type=_safe_int(item.get("autoAddIm") or item.get("marginType")),
                    margin=_safe_float(item.get("im") or item.get("margin")),
                    fetched_at=fetched_at,
                )
            )
        return positions

    # ── Open orders ────────────────────────────────────────────────────────────

    def get_open_orders(
        self,
        symbol: Optional[str] = None,
        page_num: int = 1,
        page_size: int = 100,
    ) -> List[OpenOrder]:
        """
        Return current open orders, optionally filtered locally by symbol.

        Endpoint: GET /api/v1/private/order/list/open_orders
        """
        if page_num < 1 or page_size < 1 or page_size > 100:
            raise MexcPrivateError("open-order pagination must use page_num >= 1 and 1 <= page_size <= 100")
        symbol_filter = symbol.strip().upper() if symbol and symbol.strip() else None
        path = "/api/v1/private/order/list/open_orders"
        data = self._fetch(path, {"page_num": page_num, "page_size": page_size})

        if isinstance(data, dict):
            # Some endpoints wrap in resultList / list
            data = data.get("resultList") or data.get("list") or []

        if not isinstance(data, list):
            raise MexcPrivateError(
                f"{path}: expected list, got {type(data).__name__}",
                raw=None,
            )

        fetched_at = _now_iso()
        orders: List[OpenOrder] = []
        for item in data:
            if not isinstance(item, dict):
                continue
            sym = str(item.get("symbol") or "").upper()
            if symbol_filter and sym != symbol_filter:
                continue
            orders.append(
                OpenOrder(
                    order_id=str(item["orderId"]) if item.get("orderId") is not None else None,
                    symbol=sym,
                    side=_safe_int(item.get("side")),
                    price=_safe_float(item.get("price")),
                    vol=_safe_float(item.get("vol")),
                    deal_avg_price=_safe_float(item.get("dealAvgPrice")),
                    deal_vol=_safe_float(item.get("dealVol")),
                    order_type=_safe_int(item.get("orderType")),
                    state=_safe_int(item.get("state")),
                    create_time=_ms_to_iso(item.get("createTime")),
                    fetched_at=fetched_at,
                )
            )
        return orders

    def get_order(self, order_id: str) -> Dict[str, Any]:
        """Return one exchange order by ID for canary reconciliation."""
        if not order_id or not order_id.strip():
            raise MexcPrivateError("order_id must not be empty")
        path = f"/api/v1/private/order/get/{urllib.parse.quote(order_id.strip())}"
        data = self._fetch(path)
        if not isinstance(data, dict):
            raise MexcPrivateError(
                f"{path}: expected dict, got {type(data).__name__}",
                raw=None,
            )
        return data

    def get_open_tpsl_orders(self, symbol: Optional[str] = None) -> List[Dict[str, Any]]:
        """Return current exchange TP/SL plans for protection reconciliation."""
        params = {"symbol": symbol.strip().upper()} if symbol and symbol.strip() else None
        data = self._fetch("/api/v1/private/stoporder/open_orders", params)
        if isinstance(data, dict):
            data = data.get("resultList") or data.get("list") or []
        if not isinstance(data, list):
            raise MexcPrivateError(
                "/api/v1/private/stoporder/open_orders: expected list, got "
                f"{type(data).__name__}",
                raw=None,
            )
        return [item for item in data if isinstance(item, dict)]
