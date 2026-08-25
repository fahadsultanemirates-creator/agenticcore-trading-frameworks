"""
Shared test fixtures for Premium Tier 1.
Sets PREMIUM_* env vars to safe defaults before any import.
"""
import os
import sys

# Ensure the premium package is importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Safe defaults for all tests
os.environ.setdefault("PREMIUM_MODE", "mock")
os.environ.setdefault("PREMIUM_SIGNAL_ONLY", "true")
os.environ.setdefault("PREMIUM_WORKER_NAME", "test-worker")
os.environ.setdefault(
    "PREMIUM_STATE_PATH",
    "/tmp/premium_test_state.json"
)
os.environ.setdefault(
    "PREMIUM_AUDIT_PATH",
    "/tmp/premium_test_audit.jsonl"
)

import numpy as np
import pandas as pd
import pytest


def make_candles(
    pair: str = "EURUSD",
    bars: int = 100,
    base_price: float = 1.0850,
    vol_multiplier: float = 1.0,
    seed: int = 42,
) -> pd.DataFrame:
    """Create synthetic completed-candle DataFrame for tests."""
    rng = np.random.default_rng(seed)
    closes = [base_price]
    for _ in range(bars - 1):
        closes.append(closes[-1] * (1 + rng.normal(0, 0.0003)))

    rows = []
    for i, c in enumerate(closes):
        o = closes[i - 1] if i > 0 else c
        spread = c * 0.0003 * 0.5
        h = max(o, c) + abs(rng.normal(0, spread))
        lo = min(o, c) - abs(rng.normal(0, spread))
        tv = max(int(rng.normal(500, 150) * vol_multiplier), 10)
        rows.append({
            "time": pd.Timestamp("2024-01-01") + pd.Timedelta(minutes=15 * i),
            "open": round(o, 5),
            "high": round(h, 5),
            "low": round(lo, 5),
            "close": round(c, 5),
            "tick_volume": tv,
        })
    return pd.DataFrame(rows)


def make_low_volume_candles(pair: str = "EURUSD", bars: int = 100) -> pd.DataFrame:
    """Candles where the last bar has very low volume."""
    df = make_candles(pair, bars=bars, vol_multiplier=1.0)
    # Last bar: 20% of median → classified as LOW
    df.loc[df.index[-1], "tick_volume"] = 50
    return df


def make_high_volume_candles(pair: str = "EURUSD", bars: int = 100) -> pd.DataFrame:
    """Candles where the last bar has very high volume."""
    df = make_candles(pair, bars=bars, vol_multiplier=1.0)
    # Last bar: 3× median → classified as HIGH
    median = df["tick_volume"].iloc[:-1].median()
    df.loc[df.index[-1], "tick_volume"] = int(median * 3)
    return df
