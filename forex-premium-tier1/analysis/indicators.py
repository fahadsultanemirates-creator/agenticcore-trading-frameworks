"""
Premium Tier 1 — deterministic indicator helpers.

Pure pandas/numpy. No external TA libraries required.
All functions operate on completed-candle DataFrames only.
"""
import pandas as pd
import numpy as np
from typing import Optional, Tuple


# ── EMA ────────────────────────────────────────────────────────────────────

def ema(series: pd.Series, length: int) -> pd.Series:
    """Exponential moving average."""
    return series.ewm(span=length, adjust=False).mean()


# ── RSI ────────────────────────────────────────────────────────────────────

def rsi(series: pd.Series, length: int = 14) -> float:
    """Relative Strength Index — returns last value."""
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(com=length - 1, adjust=False).mean()
    avg_loss = loss.ewm(com=length - 1, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    rsi_series = 100 - (100 / (1 + rs))
    valid = rsi_series.dropna()
    return float(valid.iloc[-1]) if len(valid) > 0 else 50.0


# ── MACD ───────────────────────────────────────────────────────────────────

def macd(
    series: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9
) -> Tuple[float, float, float]:
    """MACD line, signal line, histogram — returns last values."""
    ema_fast = series.ewm(span=fast, adjust=False).mean()
    ema_slow = series.ewm(span=slow, adjust=False).mean()
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    histogram = macd_line - signal_line
    return float(macd_line.iloc[-1]), float(signal_line.iloc[-1]), float(histogram.iloc[-1])


# ── Bollinger Bands ────────────────────────────────────────────────────────

def bollinger(
    series: pd.Series, length: int = 20, std_mult: float = 2.0
) -> Tuple[float, float, float]:
    """Bollinger Bands — returns (upper, mid, lower) last values."""
    mid = series.rolling(length).mean()
    std = series.rolling(length).std()
    upper = mid + std_mult * std
    lower = mid - std_mult * std
    return float(upper.iloc[-1]), float(mid.iloc[-1]), float(lower.iloc[-1])


# ── ATR ────────────────────────────────────────────────────────────────────

def atr(
    high: pd.Series, low: pd.Series, close: pd.Series, length: int = 14
) -> float:
    """Average True Range — returns last value."""
    hl = high - low
    hc = (high - close.shift()).abs()
    lc = (low - close.shift()).abs()
    tr = pd.concat([hl, hc, lc], axis=1).max(axis=1)
    atr_series = tr.ewm(com=length - 1, adjust=False).mean()
    valid = atr_series.dropna()
    return float(valid.iloc[-1]) if len(valid) > 0 else 0.0


# ── Support / Resistance ───────────────────────────────────────────────────

def support_resistance(df: pd.DataFrame, lookback: int = 20) -> Tuple[float, float]:
    """Simple swing high/low S/R from last N completed candles."""
    recent = df.tail(lookback)
    return float(recent["low"].min()), float(recent["high"].max())


# ── Stochastic ─────────────────────────────────────────────────────────────

def stochastic(
    high: pd.Series, low: pd.Series, close: pd.Series,
    k_period: int = 14, d_period: int = 3
) -> Tuple[float, float]:
    """Stochastic oscillator (%K, %D) — last values."""
    lowest_low = low.rolling(k_period).min()
    highest_high = high.rolling(k_period).max()
    denom = (highest_high - lowest_low).replace(0, np.nan)
    k = 100 * (close - lowest_low) / denom
    d = k.rolling(d_period).mean()
    k_valid = k.dropna()
    d_valid = d.dropna()
    k_val = float(k_valid.iloc[-1]) if len(k_valid) > 0 else 50.0
    d_val = float(d_valid.iloc[-1]) if len(d_valid) > 0 else 50.0
    return k_val, d_val


# ── Full indicator snapshot for one timeframe ──────────────────────────────

def compute_indicators(pair: str, df: pd.DataFrame) -> Optional[dict]:
    """
    Compute all indicators for one completed-candle DataFrame.
    Returns None if insufficient data (min 55 candles required).
    """
    if df is None or len(df) < 55:
        return None

    df = df.copy()
    close = df["close"].astype(float)
    high = df["high"].astype(float)
    low = df["low"].astype(float)

    rsi_val = rsi(close, 14)
    macd_val, macd_sig, macd_hist = macd(close)
    ema20 = float(ema(close, 20).iloc[-1])
    ema50 = float(ema(close, 50).iloc[-1])
    ema200 = float(ema(close, 200).iloc[-1]) if len(df) >= 200 else float("nan")
    price = float(close.iloc[-1])
    bb_upper, bb_mid, bb_lower = bollinger(close)
    atr_val = atr(high, low, close)
    support, resistance = support_resistance(df)
    stoch_k, stoch_d = stochastic(high, low, close)

    # Trend classification (robust to NaN ema200)
    if np.isnan(ema200):
        trend = "UP" if price > ema50 and ema20 > ema50 else (
            "DOWN" if price < ema50 and ema20 < ema50 else "SIDEWAYS"
        )
    else:
        trend = "UP" if price > ema50 > ema200 else (
            "DOWN" if price < ema50 < ema200 else "SIDEWAYS"
        )

    # Signal counting
    bullish = sum([
        rsi_val < 30,
        macd_val > macd_sig,
        macd_hist > 0,
        price > ema20,
        price > ema50,
        price <= bb_lower * 1.001,
        stoch_k < 20 and stoch_k > stoch_d,
    ])
    bearish = sum([
        rsi_val > 70,
        macd_val < macd_sig,
        macd_hist < 0,
        price < ema20,
        price < ema50,
        price >= bb_upper * 0.999,
        stoch_k > 80 and stoch_k < stoch_d,
    ])

    # Technical quality score (0–1): how many signals align, how extreme
    max_signals = 7
    signal_imbalance = abs(bullish - bearish) / max_signals
    rsi_extreme = max(0.0, (70 - rsi_val) / 40) if rsi_val < 50 else max(0.0, (rsi_val - 30) / 40)
    macd_momentum = abs(macd_hist) / (abs(macd_sig) + 1e-10)
    bb_position = (
        max(0.0, (bb_lower - price) / (bb_lower * 0.01))  # below lower band
        if price < bb_mid
        else max(0.0, (price - bb_upper) / (bb_upper * 0.01))  # above upper band
    )
    tech_quality = min(1.0, (signal_imbalance * 0.5 + min(rsi_extreme, 0.3) + min(macd_momentum * 0.2, 0.2)))

    return {
        "price": round(price, 5),
        "rsi": round(rsi_val, 2),
        "macd": round(macd_val, 6),
        "macd_signal": round(macd_sig, 6),
        "macd_hist": round(macd_hist, 6),
        "ema20": round(ema20, 5),
        "ema50": round(ema50, 5),
        "ema200": round(ema200, 5) if not np.isnan(ema200) else None,
        "bb_upper": round(bb_upper, 5),
        "bb_mid": round(bb_mid, 5),
        "bb_lower": round(bb_lower, 5),
        "atr": round(atr_val, 6),
        "support": round(support, 5),
        "resistance": round(resistance, 5),
        "stoch_k": round(stoch_k, 2),
        "stoch_d": round(stoch_d, 2),
        "trend": trend,
        "bullish_signals": bullish,
        "bearish_signals": bearish,
        "tech_quality": round(tech_quality, 4),
    }
