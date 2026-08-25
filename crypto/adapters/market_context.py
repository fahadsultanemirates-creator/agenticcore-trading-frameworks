"""
market_context.py – Bounded public context from independent market sources.

MEXC remains the only tradable-price source. CoinGecko, optional CoinMarketCap,
and public perpetual exchange flow are evidence only. A provider can confirm or
penalize an existing deterministic setup, but it cannot create a direction.
"""

from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ElementTree
from concurrent.futures import ThreadPoolExecutor, as_completed
from time import monotonic, time
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

HttpGetFn = Callable[[str, int], Tuple[int, Any]]

COINGECKO_BASE = "https://api.coingecko.com/api/v3"
COINMARKETCAP_BASE = "https://pro-api.coinmarketcap.com/v1"
BINANCE_FUTURES_BASE = "https://fapi.binance.com/fapi/v1"
BINANCE_FUTURES_DATA_BASE = "https://fapi.binance.com/futures/data"
BYBIT_BASE = "https://api.bybit.com/v5/market"
COINDESK_RSS = "https://www.coindesk.com/arc/outboundfeeds/rss/"
MAX_RATIO_AGE_MS = 20 * 60 * 1000
MAX_BOOK_AGE_MS = 2 * 60 * 1000


def _http_get_json(
    url: str, timeout: int, headers: Optional[Dict[str, str]] = None
) -> Tuple[int, Any]:
    request_headers = {
        "Accept": "application/json",
        "User-Agent": "AgenticCore-Crypto/1.0",
    }
    request_headers.update(headers or {})
    request = urllib.request.Request(url, headers=request_headers)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.status, json.loads(response.read().decode("utf-8", errors="replace"))


def _http_get_text(url: str, timeout: int) -> Tuple[int, str]:
    request = urllib.request.Request(url, headers={"Accept": "application/rss+xml, application/xml, text/xml", "User-Agent": "AgenticCore-Crypto/1.0"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.status, response.read().decode("utf-8", errors="replace")


def _remaining_timeout(timeout: int, deadline: Optional[float]) -> int:
    remaining = timeout if deadline is None else min(timeout, deadline - monotonic())
    return max(0, int(remaining))


def _base_symbol(symbol: str) -> str:
    value = str(symbol or "").upper().replace("-", "_")
    for suffix in ("_USDT", "USDT", "_USDC", "USDC"):
        if value.endswith(suffix):
            return value[: -len(suffix)]
    return value.split("_", 1)[0]


def _exchange_symbol(symbol: str) -> str:
    return f"{_base_symbol(symbol)}USDT"


def _number(value: Any) -> Optional[float]:
    try:
        number = float(value)
        return number if number == number else None
    except (TypeError, ValueError):
        return None


def _timestamp_ms(value: Any) -> Optional[int]:
    parsed = _number(value)
    if parsed is None or parsed <= 0:
        return None
    return int(parsed * 1000) if parsed < 10_000_000_000 else int(parsed)


def _buy_pressure(buy: Any, sell: Any) -> Optional[float]:
    buy_value = _number(buy)
    sell_value = _number(sell)
    if buy_value is None or sell_value is None or buy_value < 0 or sell_value < 0:
        return None
    total = buy_value + sell_value
    return (buy_value / total) * 100 if total > 0 else None


def _depth_imbalance(bids: Any, asks: Any) -> Optional[float]:
    def total(levels: Any) -> Optional[float]:
        if not isinstance(levels, list):
            return None
        values = []
        for level in levels:
            if isinstance(level, (list, tuple)) and len(level) >= 2:
                amount = _number(level[1])
                if amount is not None and amount >= 0:
                    values.append(amount)
        return sum(values) if values else None

    bid_total = total(bids)
    ask_total = total(asks)
    if bid_total is None or ask_total is None or bid_total + ask_total <= 0:
        return None
    return ((bid_total - ask_total) / (bid_total + ask_total)) * 100


def _provider_status(evidence: Dict[str, Any], now_ms: Optional[int] = None) -> str:
    """Only fresh directional evidence may receive a cross-market vote."""
    current = int(time() * 1000) if now_ms is None else now_ms
    ratio_time = _timestamp_ms(evidence.get("buy_pressure_timestamp_ms"))
    book_time = _timestamp_ms(evidence.get("order_book_fetched_at_ms"))
    fresh_ratio = (
        evidence.get("buy_pressure_pct") is not None
        and ratio_time is not None
        and 0 <= current - ratio_time <= MAX_RATIO_AGE_MS
    )
    fresh_book = (
        evidence.get("order_book_imbalance_pct") is not None
        and book_time is not None
        and 0 <= current - book_time <= MAX_BOOK_AGE_MS
    )
    if fresh_ratio or fresh_book:
        return "live"
    return "unavailable"


def fetch_coingecko_context(
    symbols: Iterable[str],
    timeout: int = 8,
    http_get: Optional[HttpGetFn] = None,
    deadline: Optional[float] = None,
) -> Dict[str, Any]:
    """Fetch global and selected-coin context without affecting signal quality."""
    getter = http_get or _http_get_json
    result: Dict[str, Any] = {"status": "unavailable", "global": None, "coins": []}
    try:
        request_timeout = _remaining_timeout(timeout, deadline)
        if request_timeout < 1:
            return result
        status, payload = getter(f"{COINGECKO_BASE}/global", request_timeout)
        if status != 200 or not isinstance(payload, dict) or not isinstance(payload.get("data"), dict):
            return result
        global_data = payload["data"]
        result["global"] = {
            "market_cap_change_pct_24h": global_data.get("market_cap_change_percentage_24h_usd"),
            "btc_dominance_pct": (global_data.get("market_cap_percentage") or {}).get("btc"),
            "total_market_cap_usd": (global_data.get("total_market_cap") or {}).get("usd"),
        }
        normalized = sorted(
            {
                _base_symbol(str(symbol))
                for symbol in symbols
                if symbol
            }
        )
        if normalized:
            query = urllib.parse.urlencode(
                {
                    "vs_currency": "usd",
                    "symbols": ",".join(symbol.lower() for symbol in normalized),
                    "price_change_percentage": "24h",
                }
            )
            request_timeout = _remaining_timeout(timeout, deadline)
            if request_timeout < 1:
                result["status"] = "partial"
                return result
            coin_status, coins = getter(f"{COINGECKO_BASE}/coins/markets?{query}", request_timeout)
            if coin_status == 200 and isinstance(coins, list):
                result["coins"] = [
                    {
                        "symbol": str(item.get("symbol") or "").upper(),
                        "market_cap_rank": item.get("market_cap_rank"),
                        "market_cap_usd": item.get("market_cap"),
                        "fully_diluted_valuation_usd": item.get("fully_diluted_valuation"),
                        "circulating_supply": item.get("circulating_supply"),
                        "change_pct_24h": item.get("price_change_percentage_24h"),
                    }
                    for item in coins
                    if isinstance(item, dict)
                ]
        result["status"] = "live"
    except Exception:
        # Context sources must never halt the public scanner.
        return result
    return result


def fetch_coinmarketcap_context(
    symbols: Iterable[str],
    timeout: int = 8,
    http_get: Optional[HttpGetFn] = None,
    deadline: Optional[float] = None,
) -> Dict[str, Any]:
    """Fetch optional market-cap context; missing credentials stay unavailable."""
    result: Dict[str, Any] = {
        "status": "unavailable",
        "reason": "api_key_not_configured",
        "coins": [],
    }
    api_key = os.environ.get("COINMARKETCAP_API_KEY", "").strip() or os.environ.get(
        "CMC_API_KEY", ""
    ).strip()
    if not api_key:
        return result
    normalized = sorted({_base_symbol(str(symbol)) for symbol in symbols if symbol})
    if not normalized:
        result["reason"] = "no_symbols"
        return result
    query = urllib.parse.urlencode({"symbol": ",".join(normalized), "convert": "USD"})
    getter = http_get or _http_get_json
    try:
        request_timeout = _remaining_timeout(timeout, deadline)
        if request_timeout < 1:
            result["reason"] = "budget_exhausted"
            return result
        if http_get:
            status, payload = getter(f"{COINMARKETCAP_BASE}/cryptocurrency/quotes/latest?{query}", request_timeout)
        else:
            status, payload = getter(
                f"{COINMARKETCAP_BASE}/cryptocurrency/quotes/latest?{query}",
                request_timeout,
                {"X-CMC_PRO_API_KEY": api_key},
            )
        if status != 200 or not isinstance(payload, dict):
            result["reason"] = f"http_{status}"
            return result
        data = payload.get("data")
        if not isinstance(data, dict):
            result["reason"] = "malformed_response"
            return result
        coins: List[Dict[str, Any]] = []
        for item in data.values():
            if not isinstance(item, dict):
                continue
            quote = item.get("quote") or {}
            usd = quote.get("USD") if isinstance(quote, dict) else {}
            coins.append(
                {
                    "symbol": str(item.get("symbol") or "").upper(),
                    "market_cap_usd": usd.get("market_cap") if isinstance(usd, dict) else None,
                    "fully_diluted_valuation_usd": usd.get("fully_diluted_market_cap") if isinstance(usd, dict) else None,
                    "change_pct_24h": usd.get("percent_change_24h") if isinstance(usd, dict) else None,
                    "circulating_supply": item.get("circulating_supply"),
                    "total_supply": item.get("total_supply"),
                }
            )
        result["coins"] = coins
        result["status"] = "live" if coins else "partial"
        result.pop("reason", None)
    except Exception as exc:
        result["reason"] = type(exc).__name__
    return result


def _fetch_binance_flow(
    symbol: str,
    timeout: int,
    deadline: Optional[float],
) -> Dict[str, Any]:
    """Read only public Binance Futures taker, depth, funding, and OI data."""
    result: Dict[str, Any] = {
        "provider": "binance_futures",
        "symbol": _exchange_symbol(symbol),
        "status": "unavailable",
        "buy_pressure_pct": None,
        "order_book_imbalance_pct": None,
        "funding_rate": None,
        "open_interest": None,
        "fetched_at": None,
        "buy_pressure_timestamp_ms": None,
        "order_book_fetched_at_ms": None,
        "error": None,
    }
    try:
        request_timeout = _remaining_timeout(timeout, deadline)
        if request_timeout < 1:
            result["error"] = "budget_exhausted"
            return result
        ratio_status, ratio = _http_get_json(
            f"{BINANCE_FUTURES_DATA_BASE}/takerlongshortRatio?symbol={_exchange_symbol(symbol)}&period=15m&limit=1",
            request_timeout,
        )
        if ratio_status == 200 and isinstance(ratio, list) and ratio:
            row = ratio[-1] if isinstance(ratio[-1], dict) else {}
            result["buy_pressure_pct"] = _buy_pressure(
                row.get("buyVol"),
                row.get("sellVol"),
            )
            if result["buy_pressure_pct"] is None:
                buy_sell_ratio = _number(row.get("buySellRatio"))
                if buy_sell_ratio is not None and buy_sell_ratio >= 0:
                    result["buy_pressure_pct"] = (buy_sell_ratio / (1 + buy_sell_ratio)) * 100
            result["buy_pressure_timestamp_ms"] = _timestamp_ms(row.get("timestamp"))

        request_timeout = _remaining_timeout(timeout, deadline)
        if request_timeout >= 1:
            depth_status, depth = _http_get_json(
                f"{BINANCE_FUTURES_BASE}/depth?symbol={_exchange_symbol(symbol)}&limit=20",
                request_timeout,
            )
            if depth_status == 200 and isinstance(depth, dict):
                result["order_book_imbalance_pct"] = _depth_imbalance(
                    depth.get("bids"), depth.get("asks")
                )
                result["order_book_fetched_at_ms"] = int(time() * 1000)

        request_timeout = _remaining_timeout(timeout, deadline)
        if request_timeout >= 1:
            premium_status, premium = _http_get_json(
                f"{BINANCE_FUTURES_BASE}/premiumIndex?symbol={_exchange_symbol(symbol)}",
                request_timeout,
            )
            if premium_status == 200 and isinstance(premium, dict):
                result["funding_rate"] = _number(premium.get("lastFundingRate"))

        request_timeout = _remaining_timeout(timeout, deadline)
        if request_timeout >= 1:
            oi_status, oi = _http_get_json(
                f"{BINANCE_FUTURES_BASE}/openInterest?symbol={_exchange_symbol(symbol)}",
                request_timeout,
            )
            if oi_status == 200 and isinstance(oi, dict):
                result["open_interest"] = _number(oi.get("openInterest"))
        result["fetched_at"] = int(time() * 1000)
        result["status"] = _provider_status(result)
        if result["status"] == "unavailable":
            result["error"] = "no_public_fields"
    except Exception as exc:
        result["error"] = type(exc).__name__
    return result


def _fetch_bybit_flow(
    symbol: str,
    timeout: int,
    deadline: Optional[float],
) -> Dict[str, Any]:
    """Read only public Bybit Linear ticker and account-ratio data."""
    exchange_symbol = _exchange_symbol(symbol)
    result: Dict[str, Any] = {
        "provider": "bybit_linear",
        "symbol": exchange_symbol,
        "status": "unavailable",
        "buy_pressure_pct": None,
        "order_book_imbalance_pct": None,
        "funding_rate": None,
        "open_interest": None,
        "fetched_at": None,
        "buy_pressure_timestamp_ms": None,
        "error": None,
    }
    try:
        request_timeout = _remaining_timeout(timeout, deadline)
        if request_timeout < 1:
            result["error"] = "budget_exhausted"
            return result
        ticker_status, ticker_payload = _http_get_json(
            f"{BYBIT_BASE}/tickers?category=linear&symbol={exchange_symbol}",
            request_timeout,
        )
        if ticker_status == 200 and isinstance(ticker_payload, dict):
            rows = ((ticker_payload.get("result") or {}).get("list") or [])
            row = rows[0] if rows and isinstance(rows[0], dict) else {}
            result["funding_rate"] = _number(row.get("fundingRate"))
            result["open_interest"] = _number(row.get("openInterest"))
            bid = _number(row.get("bid1Price"))
            ask = _number(row.get("ask1Price"))
            if bid is not None and ask is not None and bid + ask > 0:
                # This is quoted spread evidence, not directional flow.
                result["quoted_spread_pct"] = ((ask - bid) / ((ask + bid) / 2)) * 100

        request_timeout = _remaining_timeout(timeout, deadline)
        if request_timeout >= 1:
            ratio_status, ratio_payload = _http_get_json(
                f"{BYBIT_BASE}/account-ratio?category=linear&symbol={exchange_symbol}&period=15&limit=1",
                request_timeout,
            )
            if ratio_status == 200 and isinstance(ratio_payload, dict):
                rows = ((ratio_payload.get("result") or {}).get("list") or [])
                row = rows[-1] if rows and isinstance(rows[-1], dict) else {}
                result["buy_pressure_pct"] = _buy_pressure(
                    row.get("buyRatio"),
                    row.get("sellRatio"),
                )
                result["buy_pressure_timestamp_ms"] = _timestamp_ms(row.get("timestamp"))
        result["fetched_at"] = int(time() * 1000)
        result["status"] = _provider_status(result)
        if result["status"] == "unavailable":
            result["error"] = "no_public_fields"
    except Exception as exc:
        result["error"] = type(exc).__name__
    return result


def _cross_market_summary(symbol: str, providers: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    """Aggregate independent directional fields without inventing a consensus."""
    provider_list = [item for item in providers if isinstance(item, dict)]
    votes: List[str] = []
    directional_pressures: List[float] = []
    for provider in provider_list:
        # Keep unavailable provider records for audit/reporting, but never let
        # stale, incomplete, or non-directional fields vote on a setup.
        if provider.get("status") != "live":
            continue
        pressure = _number(provider.get("buy_pressure_pct"))
        imbalance = _number(provider.get("order_book_imbalance_pct"))
        provider_votes: List[str] = []
        if pressure is not None:
            directional_pressures.append(pressure)
            if pressure >= 55:
                provider_votes.append("long")
            elif pressure <= 45:
                provider_votes.append("short")
        if imbalance is not None:
            if imbalance >= 15:
                provider_votes.append("long")
            elif imbalance <= -15:
                provider_votes.append("short")
        if provider_votes:
            long_count = provider_votes.count("long")
            short_count = provider_votes.count("short")
            if long_count > short_count:
                votes.append("long")
            elif short_count > long_count:
                votes.append("short")

    long_votes = votes.count("long")
    short_votes = votes.count("short")
    if long_votes and not short_votes:
        agreement = "long"
    elif short_votes and not long_votes:
        agreement = "short"
    elif long_votes or short_votes:
        agreement = "mixed"
    else:
        agreement = "neutral"
    live_count = sum(1 for provider in provider_list if provider.get("status") == "live")
    status = "live" if live_count == len(provider_list) and live_count else "partial" if live_count else "unavailable"
    return {
        "symbol": symbol,
        "status": status,
        "agreement": agreement,
        "long_votes": long_votes,
        "short_votes": short_votes,
        "provider_count": len(provider_list),
        "live_provider_count": live_count,
        "aggregate_buy_pressure_pct": (
            sum(directional_pressures) / len(directional_pressures)
            if directional_pressures
            else None
        ),
        "providers": provider_list,
        "disagreement": agreement == "mixed",
        "trading_note": "Public exchange proxies only; not wallet, whale, or on-chain identification.",
    }


def fetch_news_context(timeout: int = 8, deadline: Optional[float] = None) -> Dict[str, Any]:
    """Fetch a small public headline set for operator context, never sentiment."""
    result: Dict[str, Any] = {"status": "unavailable", "headlines": []}
    try:
        request_timeout = timeout if deadline is None else max(0, int(min(timeout, deadline - monotonic())))
        if request_timeout < 1:
            return result
        status, xml_text = _http_get_text(COINDESK_RSS, request_timeout)
        if status != 200:
            return result
        root = ElementTree.fromstring(xml_text)
        items = root.findall(".//item")[:5]
        headlines: List[Dict[str, str]] = []
        for item in items:
            title = (item.findtext("title") or "").strip()
            published = (item.findtext("pubDate") or "").strip()
            if title:
                headlines.append({"title": title[:180], "published_at": published[:80]})
        result["headlines"] = headlines
        result["status"] = "live" if headlines else "unavailable"
    except Exception:
        return result
    return result


def fetch_market_context(
    symbols: Iterable[str], timeout: int = 5, cross_market_probe_limit: int = 10
) -> Dict[str, Any]:
    """
    Return bounded public context for a small shortlisted set.

    CoinGecko/news remain operator context. Binance and Bybit are independent
    public-market evidence. Their fields are deliberately reported separately so
    the scorer can expose disagreement rather than averaging it away.
    """
    symbols_list = list(dict.fromkeys(str(symbol) for symbol in symbols if symbol))[:30]
    deadline = monotonic() + max(1, timeout)
    binance: Dict[str, Dict[str, Any]] = {}
    bybit: Dict[str, Dict[str, Any]] = {}
    flow_symbols = symbols_list[: max(1, min(cross_market_probe_limit, len(symbols_list)))]
    # Give exchange probes a bounded share of the context budget so CoinGecko
    # and the operator headlines still have a chance to report their status.
    flow_deadline = min(deadline, monotonic() + max(2, timeout * 0.65))

    def fetch_pair(symbol: str) -> Tuple[str, Dict[str, Any], Dict[str, Any]]:
        return (
            symbol,
            _fetch_binance_flow(symbol, timeout, flow_deadline),
            _fetch_bybit_flow(symbol, timeout, flow_deadline),
        )

    if flow_symbols:
        with ThreadPoolExecutor(max_workers=min(6, len(flow_symbols))) as executor:
            futures = [executor.submit(fetch_pair, symbol) for symbol in flow_symbols]
            for future in as_completed(futures):
                try:
                    symbol, binance_result, bybit_result = future.result()
                    binance[symbol] = binance_result
                    bybit[symbol] = bybit_result
                except Exception:
                    # An isolated public provider failure is reflected below as
                    # unavailable for its symbol; it cannot halt a market cycle.
                    continue

    def unavailable_provider(provider: str, symbol: str) -> Dict[str, Any]:
        return {
            "provider": provider,
            "symbol": _exchange_symbol(symbol),
            "status": "unavailable",
            "reason": "not_probed_within_budget",
        }

    cross_market = {
        symbol: _cross_market_summary(
            symbol,
            [
                binance.get(symbol, unavailable_provider("binance_futures", symbol)),
                bybit.get(symbol, unavailable_provider("bybit_linear", symbol)),
            ],
        )
        for symbol in symbols_list
    }
    return {
        "coingecko": fetch_coingecko_context(symbols_list, timeout=timeout, deadline=deadline),
        "coinmarketcap": fetch_coinmarketcap_context(
            symbols_list, timeout=timeout, deadline=deadline
        ),
        "cross_market": cross_market,
        "providers": {
            "binance_futures": binance,
            "bybit_linear": bybit,
        },
        "news": fetch_news_context(timeout=timeout, deadline=deadline),
        "trading_note": (
            "Context only. MEXC prices and completed candles remain the signal "
            "source. Cross-market evidence is capped confirmation, never a trigger."
        ),
    }