"""
Agent 1 — Market Data Agent
Fetches OHLCV candles for all configured pairs at all timeframes.
Uses broker-suffixed symbols automatically via settings.pairs_with_suffix.
"""
import asyncio
from typing import Optional
import pandas as pd


class MarketDataAgent:
    def __init__(self, bridge, settings):
        self.bridge   = bridge
        self.settings = settings

    async def run(self) -> dict[str, dict[str, pd.DataFrame]]:
        """
        Returns { "EURUSD" (or "EURUSD.m"): { "M15": df, "H1": df, "H4": df }, ... }
        Pair keys use the broker-suffixed symbol so all downstream agents
        can pass the symbol directly to MT5 without re-applying the suffix.
        """
        pairs      = self.settings.pairs_with_suffix
        timeframes = self.settings.timeframes
        bars       = self.settings.bars_to_fetch
        result: dict[str, dict[str, pd.DataFrame]] = {}

        for pair in pairs:
            result[pair] = {}
            for tf in timeframes:
                try:
                    df = await asyncio.to_thread(self.bridge.get_ohlcv, pair, tf, bars)
                    if df is not None and not df.empty:
                        result[pair][tf] = df
                    else:
                        print(f"[MarketData] No data: {pair} {tf}")
                except Exception as e:
                    print(f"[MarketData] Error {pair} {tf}: {e}")

        fetched = sum(1 for v in result.values() if v)
        print(f"[MarketData] Fetched {fetched}/{len(pairs)} pairs ({', '.join(timeframes)})")
        return result
