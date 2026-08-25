"""
Agent 2 — Technical Analysis Agent (Tier 2)
Key upgrade: multi-timeframe confluence scoring.
Each timeframe (M15, H1, H4) is analysed independently.
The more timeframes that agree on direction, the higher the confluence score.
"""
import asyncio
import pandas as pd
import numpy as np
from agents.volume_sense import completed_volume_snapshot


# ── Pure-pandas indicator helpers ──────────────────────────────────────────

def _safe_last(s) -> float:
    if s is None:
        return float("nan")
    if isinstance(s, pd.DataFrame):
        s = s.iloc[:, 0]
    if isinstance(s, pd.Series):
        v = s.dropna()
        return float(v.iloc[-1]) if len(v) > 0 else float("nan")
    try:
        return float(s)
    except Exception:
        return float("nan")


def _ema(series: pd.Series, length: int) -> pd.Series:
    return series.ewm(span=length, adjust=False).mean()


def _rsi(series: pd.Series, length: int = 14) -> float:
    delta    = series.diff()
    gain     = delta.clip(lower=0)
    loss     = -delta.clip(upper=0)
    avg_gain = gain.ewm(com=length - 1, adjust=False).mean()
    avg_loss = loss.ewm(com=length - 1, adjust=False).mean()
    rs       = avg_gain / avg_loss.replace(0, float("nan"))
    rsi      = 100 - (100 / (1 + rs))
    return float(rsi.dropna().iloc[-1]) if len(rsi.dropna()) > 0 else 50.0


def _macd(series: pd.Series, fast=12, slow=26, signal=9) -> tuple[float, float, float]:
    ema_fast    = series.ewm(span=fast,   adjust=False).mean()
    ema_slow    = series.ewm(span=slow,   adjust=False).mean()
    macd_line   = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    histogram   = macd_line - signal_line
    return float(macd_line.iloc[-1]), float(signal_line.iloc[-1]), float(histogram.iloc[-1])


def _bollinger(series: pd.Series, length=20, std_mult=2.0) -> tuple[float, float, float]:
    mid   = series.rolling(length).mean()
    std   = series.rolling(length).std()
    upper = mid + std_mult * std
    lower = mid - std_mult * std
    return float(upper.iloc[-1]), float(mid.iloc[-1]), float(lower.iloc[-1])


def _atr(high: pd.Series, low: pd.Series, close: pd.Series, length=14) -> float:
    hl  = high - low
    hc  = (high - close.shift()).abs()
    lc  = (low  - close.shift()).abs()
    tr  = pd.concat([hl, hc, lc], axis=1).max(axis=1)
    atr = tr.ewm(com=length - 1, adjust=False).mean()
    return float(atr.dropna().iloc[-1]) if len(atr.dropna()) > 0 else 0.0


def _compute_sr(df: pd.DataFrame, lookback: int = 20) -> tuple[float, float]:
    recent = df.tail(lookback)
    return float(recent["low"].min()), float(recent["high"].max())


# ── Single-timeframe analysis ──────────────────────────────────────────────

def _analyse_tf(pair: str, df: pd.DataFrame, volume_cfg: dict) -> dict | None:
    """Analyse one timeframe. Returns indicator dict or None if insufficient data."""
    # MT5's final rate is still forming. Build every indicator from completed
    # candles only, matching the same rule used by VolumeSense.
    if df is None or len(df) < 56:
        return None

    volume = completed_volume_snapshot(df, pair, volume_cfg)
    df    = df.iloc[:-1].copy()
    close = df["close"].astype(float)
    high  = df["high"].astype(float)
    low   = df["low"].astype(float)

    rsi                       = _rsi(close, 14)
    macd_val, macd_sig, macd_h = _macd(close)
    ema20                     = float(_ema(close, 20).iloc[-1])
    ema50                     = float(_ema(close, 50).iloc[-1])
    ema200                    = float(_ema(close, 200).iloc[-1])
    price                     = float(close.iloc[-1])
    bb_upper, bb_mid, bb_lower = _bollinger(close)
    atr                       = _atr(high, low, close)
    support, resistance       = _compute_sr(df)

    trend = "UP"       if price > ema50 > ema200 else (
            "DOWN"     if price < ema50 < ema200 else "SIDEWAYS")

    bullish = sum([
        rsi < 30,
        macd_val > macd_sig,
        macd_h > 0,
        price > ema20,
        price > ema50,
        price <= bb_lower * 1.001,
    ])
    bearish = sum([
        rsi > 70,
        macd_val < macd_sig,
        macd_h < 0,
        price < ema20,
        price < ema50,
        price >= bb_upper * 0.999,
    ])

    return {
        "price":          price,
        "rsi":            round(rsi, 2),
        "macd":           round(macd_val, 6),
        "macd_signal":    round(macd_sig, 6),
        "macd_hist":      round(macd_h, 6),
        "ema20":          round(ema20, 5),
        "ema50":          round(ema50, 5),
        "ema200":         round(ema200, 5),
        "bb_upper":       round(bb_upper, 5),
        "bb_mid":         round(bb_mid, 5),
        "bb_lower":       round(bb_lower, 5),
        "atr":            round(atr, 6),
        "support":        round(support, 5),
        "resistance":     round(resistance, 5),
        "trend":          trend,
        "bullish_signals": bullish,
        "bearish_signals": bearish,
        "volume":         volume,
    }


# ── Multi-timeframe confluence ─────────────────────────────────────────────

def _confluence_score(tf_results: dict[str, dict | None],
                       primary_direction: str,
                       full_bonus: int,
                       partial_bonus: int) -> tuple[int, str]:
    """
    Count how many timeframes agree with the primary direction.
    Returns (confluence_bonus, readable_summary).
    """
    agreements = 0
    details    = []
    for tf, snap in tf_results.items():
        if snap is None:
            details.append(f"{tf}:NO_DATA")
            continue
        bull = snap["bullish_signals"]
        bear = snap["bearish_signals"]
        if primary_direction == "BUY"  and bull > bear:
            agreements += 1
            details.append(f"{tf}:✅BUY")
        elif primary_direction == "SELL" and bear > bull:
            agreements += 1
            details.append(f"{tf}:✅SELL")
        else:
            details.append(f"{tf}:❌NEUTRAL")

    total_tfs = sum(1 for v in tf_results.values() if v is not None)
    if total_tfs == 0:
        return 0, "no data"

    if agreements >= 3 or (total_tfs == 2 and agreements >= 2):
        bonus   = full_bonus
        verdict = "FULL"
    elif agreements >= 2:
        bonus   = partial_bonus
        verdict = "PARTIAL"
    else:
        bonus   = 0
        verdict = "WEAK"

    summary = f"{verdict} ({agreements}/{total_tfs}) [{', '.join(details)}]"
    return bonus, summary


# ── Main agent ─────────────────────────────────────────────────────────────

def _analyse_pair(pair: str, tf_data: dict, mtf_cfg: dict, volume_cfg: dict) -> dict | None:
    """Full multi-timeframe analysis for one pair."""
    tf_results: dict[str, dict | None] = {}
    for tf, df in tf_data.items():
        tf_results[tf] = _analyse_tf(pair, df, volume_cfg)

    # Primary snapshot = M15, fall back to whatever is available
    primary = tf_results.get("M15")
    if primary is None:
        primary = next((v for v in tf_results.values() if v), None)
    if primary is None:
        return None

    # Determine primary direction for confluence check
    bull = primary["bullish_signals"]
    bear = primary["bearish_signals"]
    primary_dir = "BUY" if bull > bear else ("SELL" if bear > bull else "HOLD")

    # Confluence score
    full_bonus    = mtf_cfg.get("full_confluence_bonus",    25) if mtf_cfg.get("enabled", True) else 0
    partial_bonus = mtf_cfg.get("partial_confluence_bonus", 10) if mtf_cfg.get("enabled", True) else 0
    confluence_bonus, confluence_summary = _confluence_score(
        tf_results, primary_dir, full_bonus, partial_bonus
    )

    print(f"[TA] {pair} | primary={primary_dir} | confluence={confluence_summary}")

    return {
        "pair":               pair,
        "price":              primary["price"],
        "rsi":                primary["rsi"],
        "macd":               primary["macd"],
        "macd_signal":        primary["macd_signal"],
        "macd_hist":          primary["macd_hist"],
        "ema20":              primary["ema20"],
        "ema50":              primary["ema50"],
        "ema200":             primary["ema200"],
        "bb_upper":           primary["bb_upper"],
        "bb_mid":             primary["bb_mid"],
        "bb_lower":           primary["bb_lower"],
        "atr":                primary["atr"],
        "support":            primary["support"],
        "resistance":         primary["resistance"],
        "trend":              primary["trend"],
        "bullish_signals":    primary["bullish_signals"],
        "bearish_signals":    primary["bearish_signals"],
        # Tier 2 extras
        "confluence_bonus":   confluence_bonus,
        "confluence_summary": confluence_summary,
        "volume":             primary["volume"],
        "timeframes":         {tf: v for tf, v in tf_results.items() if v},
    }


class TechnicalAnalysisAgent:
    def __init__(self, settings):
        self.settings = settings
        self.mtf_cfg  = settings.get("mtf", {})
        self.volume_cfg = settings.get("volume_sense", {})

    async def run(self, market_data: dict) -> dict:
        """Analyse all pairs. Returns { pair: snapshot | None }"""
        results = {}
        for pair, tf_data in market_data.items():
            try:
                snap = await asyncio.to_thread(
                    _analyse_pair, pair, tf_data, self.mtf_cfg, self.volume_cfg
                )
                results[pair] = snap
            except Exception as e:
                print(f"[TA] Error {pair}: {e}")
                results[pair] = None

        analysed = sum(1 for v in results.values() if v)
        print(f"[TA] Analysed {analysed}/{len(results)} pairs")
        return results
