"""
MockBridge — synthetic price data for dev mode (no MT5 needed).
Also implements get_tick() so the live-price SL/TP fix works in dev.
"""
import random
import numpy as np
import pandas as pd
from datetime import datetime, timedelta
from typing import Optional


BASE_PRICES = {
    "EURUSD": 1.0850, "GBPUSD": 1.2700, "USDJPY": 149.50,
    "USDCHF": 0.9050, "AUDUSD": 0.6550, "NZDUSD": 0.6100,
    "USDCAD": 1.3650, "EURGBP": 0.8550, "EURJPY": 162.20,
    "GBPJPY": 189.80, "XAUUSD": 2330.00, "XAGUSD": 27.50,
    "USOIL":  78.50,  "UKOIL":  82.00,
}

MOCK_POSITIONS: list[dict] = []
_ticket_counter = 20000


def _next_ticket():
    global _ticket_counter
    _ticket_counter += 1
    return _ticket_counter


def _base_price(pair: str) -> float:
    """Strip any suffix (.m etc.) and look up base price."""
    clean = pair.rstrip("m").rstrip(".")
    return BASE_PRICES.get(pair, BASE_PRICES.get(clean, 1.0))


def generate_ohlcv(pair: str, bars: int = 200, tf_minutes: int = 15) -> pd.DataFrame:
    price = _base_price(pair)
    vol   = 0.0003
    if "JPY" in pair:
        vol = 0.002
    elif "XAU" in pair or "XAG" in pair:
        vol = 0.0008
    elif "OIL" in pair:
        vol = 0.0010

    closes = [price]
    for _ in range(bars - 1):
        closes.append(closes[-1] * (1 + np.random.normal(0, vol)))

    now   = datetime.utcnow()
    times = [now - timedelta(minutes=tf_minutes * (bars - i)) for i in range(bars)]

    rows = []
    for i, c in enumerate(closes):
        spread = c * vol * 0.5
        o = closes[i - 1] if i > 0 else c
        h = max(o, c) + abs(np.random.normal(0, spread))
        l = min(o, c) - abs(np.random.normal(0, spread))
        rows.append({
            "time": times[i], "open": o, "high": h,
            "low": l, "close": c,
            "tick_volume": max(int(random.gauss(500, 150)), 50),
        })
    return pd.DataFrame(rows)


class MockBridge:
    def is_connected(self) -> bool:
        return True

    def get_ohlcv(self, pair: str, timeframe: str = "M15", bars: int = 200) -> Optional[pd.DataFrame]:
        tf_map = {"M1": 1, "M5": 5, "M15": 15, "M30": 30, "H1": 60, "H4": 240, "D1": 1440}
        return generate_ohlcv(pair, bars=bars, tf_minutes=tf_map.get(timeframe, 15))

    def get_tick(self, pair: str) -> dict:
        """Return a live-ish bid/ask tick. Used for SL/TP calculation at order time."""
        base = _base_price(pair) * (1 + random.uniform(-0.0002, 0.0002))
        spread_pct = 0.00003 if "JPY" not in pair and "XAU" not in pair else 0.0002
        spread = base * spread_pct
        return {"bid": base, "ask": base + spread, "pair": pair}

    def get_account_info(self) -> dict:
        balance = 10_000.0
        equity  = balance + sum(p.get("profit", 0) for p in MOCK_POSITIONS)
        return {
            "balance": balance, "equity": equity,
            "margin": len(MOCK_POSITIONS) * 50.0,
            "free_margin": equity - len(MOCK_POSITIONS) * 50.0,
            "currency": "USD", "leverage": 100,
        }

    def get_positions(self) -> list[dict]:
        return list(MOCK_POSITIONS)

    def place_order(self, pair: str, direction: str, lot: float,
                    sl_price: float, tp_price: float, comment: str = "",
                    filling_mode: str = "auto") -> dict:
        tick   = self.get_tick(pair)
        price  = tick["ask"] if direction == "BUY" else tick["bid"]
        ticket = _next_ticket()
        pos = {
            "ticket": ticket, "pair": pair, "direction": direction,
            "lot": lot, "open_price": price, "sl": sl_price, "tp": tp_price,
            "profit": 0.0, "open_time": datetime.utcnow().isoformat(), "comment": comment,
        }
        MOCK_POSITIONS.append(pos)
        print(f"[MockBridge] Placed #{ticket} {direction} {lot} {pair} @ {price:.5f}  SL={sl_price}  TP={tp_price}")
        return {"success": True, "ticket": ticket, "price": price}

    def close_position(self, ticket: int) -> dict:
        global MOCK_POSITIONS
        before = len(MOCK_POSITIONS)
        pos    = next((p for p in MOCK_POSITIONS if p["ticket"] == ticket), None)
        MOCK_POSITIONS = [p for p in MOCK_POSITIONS if p["ticket"] != ticket]
        closed = before > len(MOCK_POSITIONS)
        profit = random.uniform(-20, 40)
        if pos:
            print(f"[MockBridge] Closed #{ticket} {pos.get('pair')} profit=${profit:.2f}")
        return {"success": closed, "profit": profit,
                "pair": pos.get("pair") if pos else "", "direction": pos.get("direction") if pos else ""}

    def close_all_positions(self) -> list[dict]:
        return [self.close_position(p["ticket"]) for p in list(MOCK_POSITIONS)]

    def modify_position(self, ticket: int, sl: float, tp: float) -> dict:
        for p in MOCK_POSITIONS:
            if p["ticket"] == ticket:
                p["sl"], p["tp"] = sl, tp
                return {"success": True}
        return {"success": False}
