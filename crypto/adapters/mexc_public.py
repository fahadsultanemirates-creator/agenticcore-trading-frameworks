"""
mexc_public.py – MEXC Futures public API adapter.

SIGNAL/READ-ONLY: no private credentials, no signed requests, no order paths.
Only public endpoints are used:
  - /api/v1/contract/detail          contract details
  - /api/v1/contract/ticker          24h ticker
  - /api/v1/contract/kline/{symbol}  OHLCV candles
  - /api/v1/contract/funding_rate/{symbol}  funding rate
  - /api/v1/contract/index/price/{symbol} (used for OI proxy)
  - /api/v1/contract/risk_reverse/{symbol} open interest (volume)

Transport is dependency-injected: pass a callable with the same signature
as `default_http_get` to make the adapter fully testable without real network.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

from domain.models import (
    Candle,
    ContractDetail,
    FundingInfo,
    OpenInterest,
    OrderBook,
    RecentTrade,
    Ticker,
)

# ── Public base URL ────────────────────────────────────────────────────────────
MEXC_FUTURES_BASE = "https://contract.mexc.com"


# ── HTTP transport ─────────────────────────────────────────────────────────────

HttpGetFn = Callable[[str, int], Tuple[int, Dict[str, Any]]]


def default_http_get(url: str, timeout_seconds: int = 10) -> Tuple[int, Dict[str, Any]]:
    """
    Perform a GET request and return (status_code, parsed_json_body).
    Raises ValueError on non-JSON responses.
    Raises urllib.error.URLError on network errors.
    """
    req = urllib.request.Request(
        url,
        headers={"Accept": "application/json", "User-Agent": "AgenticCore-Tier1/1.0"},
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
        raise ValueError(f"Non-JSON response from {url}: {body[:200]}") from exc
    return status, data


# ── Safe numeric helpers ───────────────────────────────────────────────────────

def _safe_float(v: Any) -> Optional[float]:
    """Convert any value to float or return None."""
    if v is None:
        return None
    try:
        f = float(v)
        return f if f == f else None  # NaN check
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


def _normalize_symbol(raw: str) -> str:
    """Normalize to uppercase, strip whitespace."""
    return str(raw).strip().upper()


# ── Response unwrapping ────────────────────────────────────────────────────────

class MexcApiError(Exception):
    """Raised when the MEXC API returns a documented error or unexpected shape."""

    def __init__(self, msg: str, code: Optional[int] = None, raw: Any = None) -> None:
        super().__init__(msg)
        self.code = code
        self.raw = raw


def _unwrap(resp: Dict[str, Any], endpoint: str) -> Any:
    """
    MEXC contract API responses follow:
      { "success": true, "data": <payload> }
    or
      { "success": false, "code": <int>, "message": <str> }

    Returns the `data` value on success.
    Raises MexcApiError on failure.
    """
    success = resp.get("success")
    if success is False or success == 0:
        code = resp.get("code")
        msg = resp.get("message") or resp.get("msg") or "Unknown error"
        raise MexcApiError(f"{endpoint}: {msg}", code=code, raw=resp)
    if "data" not in resp:
        # Some endpoints return flat objects without success/data wrapper
        return resp
    return resp["data"]


# ── Client class ──────────────────────────────────────────────────────────────

class MexcPublicClient:
    """
    Read-only MEXC Futures public data client.
    All methods are pure functions of their inputs; side effects are limited to HTTP.
    """

    def __init__(
        self,
        http_get: Optional[HttpGetFn] = None,
        timeout: int = 10,
        base_url: str = MEXC_FUTURES_BASE,
    ) -> None:
        self._get = http_get or default_http_get
        self._timeout = timeout
        self._base = base_url.rstrip("/")

    def _fetch(self, path: str) -> Any:
        url = f"{self._base}{path}"
        status, body = self._get(url, self._timeout)
        if status != 200:
            raise MexcApiError(f"HTTP {status} from {path}", raw=body)
        return _unwrap(body, path)

    # ── Contract list / detail ─────────────────────────────────────────────────

    def get_contract_list(self) -> List[ContractDetail]:
        """
        Fetch all USDT-settled perpetual contracts.
        Returns an empty list (not an exception) if the response is unexpectedly empty.
        """
        data = self._fetch("/api/v1/contract/detail")

        if not isinstance(data, list):
            # Some wrappers return {"resultList": [...]}
            if isinstance(data, dict):
                data = data.get("resultList", data.get("list", []))
        if not isinstance(data, list):
            raise MexcApiError(
                f"/api/v1/contract/detail returned unexpected type: {type(data).__name__}",
                raw=data,
            )

        contracts: List[ContractDetail] = []
        fetched_at = _now_iso()
        for item in data:
            if not isinstance(item, dict):
                continue
            symbol = _normalize_symbol(item.get("symbol", ""))
            if not symbol:
                continue
            contracts.append(
                ContractDetail(
                    symbol=symbol,
                    display_name=str(item.get("displayName") or item.get("name") or symbol),
                    base_coin=str(item.get("baseCoin") or "").upper(),
                    quote_coin=str(item.get("quoteCoin") or "USDT").upper(),
                    contract_size=_safe_float(item.get("contractSize")),
                    volume_step=_safe_float(item.get("volUnit") or item.get("volumeStep")),
                    min_quantity=_safe_float(item.get("minVol") or item.get("minQuantity")),
                    max_quantity=_safe_float(item.get("maxVol") or item.get("maxQuantity")),
                    price_precision=_safe_int(item.get("priceScale") or item.get("pricePrecision")),
                    quantity_precision=_safe_int(item.get("volScale") or item.get("volPrecision")),
                    is_active=str(item.get("state") or item.get("status") or "1") in ("0", "1", "OPEN")
                    and str(item.get("state") or item.get("status") or "1") != "DISABLED",
                    fetched_at=fetched_at,
                    contract_type=_safe_int(item.get("type")),
                    concept_plates=[
                        str(plate).strip()
                        for plate in item.get("conceptPlate", [])
                        if str(plate).strip()
                    ]
                    if isinstance(item.get("conceptPlate"), list)
                    else [],
                    price_increment=_safe_float(item.get("priceUnit")),
                )
            )
        return contracts

    # ── Ticker ─────────────────────────────────────────────────────────────────

    def get_all_tickers(self) -> List[Ticker]:
        """Fetch 24h ticker for all symbols."""
        data = self._fetch("/api/v1/contract/ticker")

        if isinstance(data, dict):
            # Some responses wrap in resultList
            data = data.get("resultList", data.get("list", [data]))
        if not isinstance(data, list):
            raise MexcApiError(
                f"/api/v1/contract/ticker returned unexpected type: {type(data).__name__}",
                raw=data,
            )

        tickers: List[Ticker] = []
        fetched_at = _now_iso()
        for item in data:
            if not isinstance(item, dict):
                continue
            symbol = _normalize_symbol(item.get("symbol", ""))
            if not symbol:
                continue

            bid = _safe_float(item.get("bid1") or item.get("bid"))
            ask = _safe_float(item.get("ask1") or item.get("ask"))
            last = _safe_float(item.get("lastPrice") or item.get("last"))
            mid = ((bid + ask) / 2.0) if (bid is not None and ask is not None) else None
            spread_pct: Optional[float] = None
            if mid and mid > 0 and bid is not None and ask is not None:
                spread_pct = round(((ask - bid) / mid) * 100, 6)

            volume_24h = _safe_float(item.get("volume24") or item.get("volume") or item.get("vol24h"))
            turnover_24h = _safe_float(item.get("amount24") or item.get("turnover24h") or item.get("amount"))
            change_pct = _safe_float(item.get("riseFallRate") or item.get("changeRate") or item.get("change"))

            tickers.append(
                Ticker(
                    symbol=symbol,
                    last_price=last,
                    bid=bid,
                    ask=ask,
                    spread_pct=spread_pct,
                    volume_24h=volume_24h,
                    turnover_24h_usdt=turnover_24h,
                    change_pct_24h=change_pct,
                    fetched_at=fetched_at,
                )
            )
        return tickers

    # ── Candles ────────────────────────────────────────────────────────────────

    def get_candles(
        self,
        symbol: str,
        interval: str = "Min15",
        limit: int = 20,
    ) -> List[Candle]:
        """
        Fetch OHLCV candles for a symbol.
        interval examples: Min1, Min5, Min15, Min30, Min60, Hour4, Day1
        The last candle in the response may still be forming (is_complete=False).
        """
        path = f"/api/v1/contract/kline/{symbol}?interval={interval}&limit={limit}"
        data = self._fetch(path)

        # data is typically {"time": [...], "open": [...], "high": [...], "low": [...], "close": [...], "vol": [...]}
        # or a list of [time, open, high, low, close, vol] arrays
        candles: List[Candle] = []
        now_ms = int(time.time() * 1000)

        if isinstance(data, dict):
            times = data.get("time", [])
            opens = data.get("open", [])
            highs = data.get("high", [])
            lows = data.get("low", [])
            closes = data.get("close", [])
            vols = data.get("vol", data.get("volume", []))

            for i, t in enumerate(times):
                open_time = _safe_int(t) or 0
                # Convert seconds to ms if needed
                if open_time < 1e12:
                    open_time = open_time * 1000

                def _g(lst: list, idx: int) -> float:
                    try:
                        return float(lst[idx]) if idx < len(lst) else 0.0
                    except (ValueError, TypeError):
                        return 0.0

                candles.append(
                    Candle(
                        symbol=symbol,
                        interval=interval,
                        open_time=open_time,
                        open=_g(opens, i),
                        high=_g(highs, i),
                        low=_g(lows, i),
                        close=_g(closes, i),
                        volume=_g(vols, i),
                        is_complete=i < len(times) - 1,
                    )
                )
        elif isinstance(data, list):
            for i, row in enumerate(data):
                if not row:
                    continue
                if isinstance(row, (list, tuple)) and len(row) >= 6:
                    open_time = _safe_int(row[0]) or 0
                    if open_time < 1e12:
                        open_time = open_time * 1000
                    candles.append(
                        Candle(
                            symbol=symbol,
                            interval=interval,
                            open_time=open_time,
                            open=float(row[1]),
                            high=float(row[2]),
                            low=float(row[3]),
                            close=float(row[4]),
                            volume=float(row[5]),
                            is_complete=i < len(data) - 1,
                        )
                    )

        return candles

    # ── Funding rate ───────────────────────────────────────────────────────────

    def get_funding_rate(self, symbol: str) -> FundingInfo:
        """Fetch funding rate for a symbol."""
        path = f"/api/v1/contract/funding_rate/{symbol}"
        try:
            data = self._fetch(path)
        except MexcApiError:
            return FundingInfo(
                symbol=symbol,
                current_rate=None,
                next_rate=None,
                next_funding_time=None,
                fetched_at=_now_iso(),
            )

        if not isinstance(data, dict):
            return FundingInfo(
                symbol=symbol,
                current_rate=None,
                next_rate=None,
                next_funding_time=None,
                fetched_at=_now_iso(),
            )

        current_rate = _safe_float(data.get("fundingRate") or data.get("currentRate"))
        next_rate = _safe_float(data.get("nextSettleRate") or data.get("nextRate"))
        next_time_raw = data.get("nextSettleTime") or data.get("nextFundingTime")
        next_time: Optional[str] = None
        if next_time_raw is not None:
            try:
                ts = int(next_time_raw)
                if ts < 1e12:
                    ts = ts * 1000
                next_time = datetime.fromtimestamp(ts / 1000, tz=timezone.utc).isoformat()
            except (ValueError, TypeError):
                next_time = str(next_time_raw)

        return FundingInfo(
            symbol=symbol,
            current_rate=current_rate,
            next_rate=next_rate,
            next_funding_time=next_time,
            fetched_at=_now_iso(),
        )

    # ── Open interest ──────────────────────────────────────────────────────────

    def get_open_interest(self, symbol: str) -> OpenInterest:
        """Fetch open interest for a symbol (via risk_reverse endpoint)."""
        path = f"/api/v1/contract/risk_reverse/{symbol}"
        try:
            data = self._fetch(path)
        except MexcApiError:
            return OpenInterest(symbol=symbol, value_usdt=None, fetched_at=_now_iso())

        if not isinstance(data, dict):
            return OpenInterest(symbol=symbol, value_usdt=None, fetched_at=_now_iso())

        # Try multiple field names
        oi = (
            _safe_float(data.get("openInterest"))
            or _safe_float(data.get("openInterestValue"))
            or _safe_float(data.get("holdVol"))
        )

        return OpenInterest(
            symbol=symbol,
            value_usdt=oi,
            fetched_at=_now_iso(),
        )

    # ── Public depth / recent trades ───────────────────────────────────────────

    def get_order_book(self, symbol: str, limit: int = 20) -> OrderBook:
        """Fetch public MEXC depth. No account or order capability is involved."""
        safe_limit = max(1, min(int(limit), 50))
        data = self._fetch(f"/api/v1/contract/depth/{symbol}?limit={safe_limit}")
        if not isinstance(data, dict):
            raise MexcApiError(
                f"/api/v1/contract/depth/{symbol} returned unexpected type: {type(data).__name__}",
                raw=data,
            )

        def parse_levels(raw: Any) -> List[tuple[float, float]]:
            levels: List[tuple[float, float]] = []
            if not isinstance(raw, list):
                return levels
            for level in raw:
                if not isinstance(level, (list, tuple)) or len(level) < 2:
                    continue
                price = _safe_float(level[0])
                quantity = _safe_float(level[1])
                if price is None or quantity is None or price <= 0 or quantity <= 0:
                    continue
                levels.append((price, quantity))
            return levels

        return OrderBook(
            symbol=symbol,
            bids=parse_levels(data.get("bids")),
            asks=parse_levels(data.get("asks")),
            fetched_at=_now_iso(),
        )

    def get_recent_trades(self, symbol: str, limit: int = 50) -> List[RecentTrade]:
        """Fetch recent public fills with the documented MEXC aggressor-side code."""
        safe_limit = max(1, min(int(limit), 100))
        data = self._fetch(
            f"/api/v1/contract/deals/{symbol}?page_num=1&page_size={safe_limit}"
        )
        if isinstance(data, dict):
            data = data.get("list", data.get("resultList", []))
        if not isinstance(data, list):
            raise MexcApiError(
                f"/api/v1/contract/deals/{symbol} returned unexpected type: {type(data).__name__}",
                raw=data,
            )

        trades: List[RecentTrade] = []
        for item in data:
            if not isinstance(item, dict):
                continue
            raw_side = item.get("T", item.get("side"))
            side = (
                "buy" if str(raw_side).lower() in ("1", "buy", "long")
                else "sell" if str(raw_side).lower() in ("2", "sell", "short")
                else "unknown"
            )
            timestamp = _safe_int(item.get("t", item.get("timestamp", item.get("cts"))))
            if timestamp is not None and timestamp < 1e12:
                timestamp *= 1000
            trades.append(
                RecentTrade(
                    symbol=symbol,
                    side=side,
                    price=_safe_float(item.get("p", item.get("price"))),
                    quantity=_safe_float(item.get("v", item.get("volume", item.get("qty")))),
                    timestamp_ms=timestamp,
                )
            )
        return trades
