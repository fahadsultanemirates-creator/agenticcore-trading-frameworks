"""
Agent 8 — Backtester (Tier 2)
Runs historical simulation on the last N days of OHLCV data.
Same logic as Tier 1 — foundation for future Tier 2 strategy optimisation.
"""
import asyncio
from datetime import datetime, timedelta
from typing import Optional


class BacktestingAgent:
    def __init__(self, bridge, settings):
        self.bridge   = bridge
        self.settings = settings

    async def run(self, pair: Optional[str] = None,
                  lookback_days: Optional[int] = None) -> dict:
        pair     = pair or (self.settings.pairs_with_suffix[0] if self.settings.pairs_with_suffix else "EURUSD")
        days     = lookback_days or self.settings.backtest.get("lookback_days", 30)
        balance  = float(self.settings.backtest.get("initial_balance", 10000))
        bars     = days * 24 * 4  # ~4 bars/hour H1 equivalent

        print(f"[Backtest] Running {pair} over {days} days ({bars} bars)")
        try:
            df = await asyncio.to_thread(self.bridge.get_ohlcv, pair, "H1", bars)
        except Exception as e:
            return {"error": str(e)}

        if df is None or len(df) < 50:
            return {"error": f"Insufficient data for {pair}"}

        from agents.technical_analysis import _analyse_tf
        import random

        trades        = []
        equity        = balance
        peak_equity   = balance
        max_drawdown  = 0.0
        open_trade    = None
        sl_usd        = self.settings.default_sl_usd
        tp_usd        = self.settings.default_tp_usd

        for i in range(55, len(df) - 1):
            window = df.iloc[:i]
            snap   = _analyse_tf(pair, window)
            if snap is None:
                continue

            # Simulate signal
            if snap["bullish_signals"] > snap["bearish_signals"] + 1:
                direction = "BUY"
            elif snap["bearish_signals"] > snap["bullish_signals"] + 1:
                direction = "SELL"
            else:
                direction = "HOLD"

            if open_trade is None and direction != "HOLD":
                entry_price = float(df.iloc[i]["close"])
                open_trade  = {"direction": direction, "entry": entry_price, "bar": i}
                continue

            if open_trade:
                current_price = float(df.iloc[i]["close"])
                entry         = open_trade["entry"]
                direction     = open_trade["direction"]

                if direction == "BUY":
                    pnl_usd = (current_price - entry) / entry * balance * 0.01
                else:
                    pnl_usd = (entry - current_price) / entry * balance * 0.01

                if pnl_usd <= -sl_usd or pnl_usd >= tp_usd or i - open_trade["bar"] >= 20:
                    profit = pnl_usd if pnl_usd >= tp_usd else (-sl_usd if pnl_usd <= -sl_usd else pnl_usd)
                    equity += profit
                    trades.append(profit)
                    peak_equity  = max(peak_equity, equity)
                    dd           = (peak_equity - equity) / peak_equity * 100
                    max_drawdown = max(max_drawdown, dd)
                    open_trade   = None

        if not trades:
            return {"error": "No trades generated — try longer lookback or different pair"}

        wins        = [t for t in trades if t > 0]
        losses      = [t for t in trades if t < 0]
        total_profit = round(sum(trades), 2)
        win_rate    = round(len(wins) / len(trades) * 100, 1) if trades else 0
        profit_factor = (
            round(sum(wins) / abs(sum(losses)), 2)
            if losses and sum(wins) > 0 else 0.0
        )
        from_date   = df.iloc[0]["time"].strftime("%Y-%m-%d") if hasattr(df.iloc[0]["time"], "strftime") else str(df.iloc[0]["time"])[:10]

        return {
            "pair":             pair,
            "lookback_days":    days,
            "from_date":        from_date,
            "total_trades":     len(trades),
            "win_rate_pct":     win_rate,
            "total_profit_usd": total_profit,
            "profit_factor":    profit_factor,
            "max_drawdown_pct": round(max_drawdown, 2),
            "sharpe_ratio":     round(total_profit / (len(trades) * 5 + 1), 2),
        }
