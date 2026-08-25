"""
Premium Tier 1 Mock Bridge — for Linux, CI, and mock mode.

Implements the same interface as PremiumMT5Bridge but returns synthetic
data. NEVER places real orders. Safe for all non-Windows environments.

Identity validation always succeeds in mock mode (nothing to check).
Completed-candle simulation returns bars with offset=1 semantics —
the most-recent candle in mock data is a completed one.
"""
import random
import numpy as np
import pandas as pd
from datetime import datetime, timedelta
from typing import Optional

from config.settings import PREMIUM_MAGIC_NUMBER


# ── Base prices ────────────────────────────────────────────────────────────

BASE_PRICES: dict = {
    "EURUSD": 1.0850,
    "GBPUSD": 1.2700,
    "USDJPY": 149.50,
    "USDCHF": 0.9050,
    "AUDUSD": 0.6550,
    "NZDUSD": 0.6100,
    "USDCAD": 1.3650,
    "EURGBP": 0.8550,
    "EURJPY": 162.20,
    "GBPJPY": 189.80,
    "XAUUSD": 2330.00,
    "XAGUSD": 27.50,
    "USOIL": 78.50,
    "UKOIL": 82.00,
}

METALS = {"XAUUSD", "XAGUSD"}

_mock_positions: list = []
_ticket_counter: int = 30000


def _next_ticket() -> int:
    global _ticket_counter
    _ticket_counter += 1
    return _ticket_counter


def _base_price(pair: str) -> float:
    """Look up base price, stripping any suffix."""
    clean = pair.rstrip("m").rstrip(".")
    return BASE_PRICES.get(pair, BASE_PRICES.get(clean, 1.0))


def _vol_for_pair(pair: str) -> float:
    if "JPY" in pair:
        return 0.002
    if any(m in pair for m in ("XAU", "XAG")):
        return 0.0008
    if "OIL" in pair:
        return 0.0010
    return 0.0003


def generate_mock_candles(
    pair: str, bars: int = 200, tf_minutes: int = 15
) -> pd.DataFrame:
    """
    Generate synthetic completed OHLCV candles.
    All returned candles are treated as completed (no forming candle).
    """
    price = _base_price(pair)
    vol = _vol_for_pair(pair)

    rng = np.random.default_rng(abs(hash(pair)) % (2**32))
    closes = [price]
    for _ in range(bars - 1):
        closes.append(closes[-1] * (1 + rng.normal(0, vol)))

    # timestamps: end before now so all are completed
    now = datetime.utcnow() - timedelta(minutes=tf_minutes)
    times = [now - timedelta(minutes=tf_minutes * (bars - 1 - i)) for i in range(bars)]

    rows = []
    for i, c in enumerate(closes):
        spread = c * vol * 0.5
        o = closes[i - 1] if i > 0 else c
        h = max(o, c) + abs(rng.normal(0, spread))
        lo = min(o, c) - abs(rng.normal(0, spread))
        tv = max(int(rng.normal(500, 150)), 50)
        rows.append({
            "time": times[i],
            "open": round(o, 5),
            "high": round(h, 5),
            "low": round(lo, 5),
            "close": round(c, 5),
            "tick_volume": tv,
        })

    return pd.DataFrame(rows)


class PremiumMockBridge:
    """
    Mock bridge implementing the PremiumMT5Bridge interface.
    Safe for Linux/CI. Always signal-only by factory policy.
    """

    def __init__(self, symbol_suffix: str = ""):
        self.symbol_suffix = symbol_suffix
        self._identity_verified = False  # set by validate_identity

    def validate_identity(self) -> bool:
        """Mock identity is always valid."""
        self._identity_verified = True
        print("[PremiumMockBridge] Identity validation: OK (mock)")
        return True

    def is_connected(self) -> bool:
        return True

    def get_live_tick(self, pair: str) -> dict:
        base = _base_price(pair) * (1 + random.uniform(-0.0002, 0.0002))
        spread_pct = 0.0002 if any(m in pair for m in ("XAU", "XAG")) else 0.00003
        spread = base * spread_pct
        return {
            "pair": pair,
            "symbol": pair + self.symbol_suffix,
            "bid": round(base, 5),
            "ask": round(base + spread, 5),
            "spread": round(spread, 5),
            "time": datetime.utcnow().isoformat(),
        }

    def get_completed_candles(
        self, pair: str, timeframe: str = "M15", bars: int = 200
    ) -> Optional[pd.DataFrame]:
        tf_map = {
            "M1": 1, "M5": 5, "M15": 15, "M30": 30,
            "H1": 60, "H4": 240, "D1": 1440,
        }
        return generate_mock_candles(pair, bars=bars, tf_minutes=tf_map.get(timeframe, 15))

    def get_account_info(self) -> dict:
        total_profit = sum(p.get("profit", 0) for p in _mock_positions)
        balance = 10_000.0
        equity = balance + total_profit
        return {
            "balance": balance,
            "equity": equity,
            "margin": len(_mock_positions) * 50.0,
            "free_margin": equity - len(_mock_positions) * 50.0,
            "currency": "USD",
            "leverage": 100,
            "login": 99999,
            "server": "JustMarkets-Demo",
            "company": "JustMarkets",
            "account_type": "DEMO",
        }

    def get_positions(self) -> list:
        return list(_mock_positions)

    def place_order(
        self,
        pair: str,
        direction: str,
        lot: float,
        sl_price: float,
        tp_price: float,
        comment: str = "",
        filling_mode: str = "auto",
    ) -> dict:
        """
        In mock mode: simulates order placement (for demo testing only).
        The factory enforces signal_only mode so this is NOT called in
        normal signal operation — the manager must enforce this separately.
        """
        tick = self.get_live_tick(pair)
        price = tick["ask"] if direction == "BUY" else tick["bid"]
        ticket = _next_ticket()
        pos = {
            "ticket": ticket,
            "pair": pair,
            "direction": direction,
            "lot": lot,
            "open_price": price,
            "sl": sl_price,
            "tp": tp_price,
            "profit": 0.0,
            "magic": PREMIUM_MAGIC_NUMBER,
            "open_time": datetime.utcnow().isoformat(),
            "comment": comment,
        }
        _mock_positions.append(pos)
        print(
            f"[PremiumMockBridge] Simulated #{ticket} {direction} {lot} {pair} "
            f"@ {price:.5f} SL={sl_price} TP={tp_price}"
        )
        return {"success": True, "ticket": ticket, "price": price}

    def close_position(self, ticket: int) -> dict:
        global _mock_positions
        pos = next((p for p in _mock_positions if p["ticket"] == ticket), None)
        _mock_positions = [p for p in _mock_positions if p["ticket"] != ticket]
        profit = random.uniform(-20, 40)
        return {
            "success": pos is not None,
            "profit": profit,
            "pair": pos.get("pair", "") if pos else "",
            "error": "" if pos else "Not found",
        }

    def shutdown(self):
        pass
