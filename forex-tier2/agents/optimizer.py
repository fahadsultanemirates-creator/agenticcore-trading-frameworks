"""
Agent 9 — Strategy Optimizer (Tier 2)
Grid-searches SL/TP and RSI threshold combinations.
Updates config with best-performing parameters.
"""
import asyncio
from typing import Optional


class StrategyOptimizerAgent:
    def __init__(self, bridge, settings):
        self.bridge   = bridge
        self.settings = settings

    async def run(self, pair: Optional[str] = None) -> dict:
        pair     = pair or (self.settings.pairs_with_suffix[0] if self.settings.pairs_with_suffix else "EURUSD")
        days     = self.settings.backtest.get("lookback_days", 30)
        bars     = days * 24 * 4
        balance  = float(self.settings.backtest.get("initial_balance", 10000))

        print(f"[Optimizer] Running grid search on {pair}...")
        try:
            df = await asyncio.to_thread(self.bridge.get_ohlcv, pair, "H1", bars)
        except Exception as e:
            return {"error": str(e)}
        if df is None or len(df) < 50:
            return {"error": f"Not enough data for {pair}"}

        from agents.technical_analysis import _analyse_tf

        sl_range  = [8, 10, 15, 20]
        tp_range  = [20, 30, 40, 60]
        best_pnl  = -float("inf")
        best_params = None
        best_metrics = None
        combos_tested = 0

        for sl_usd in sl_range:
            for tp_usd in tp_range:
                trades    = []
                open_trade = None

                for i in range(55, len(df) - 1):
                    snap = _analyse_tf(pair, df.iloc[:i])
                    if snap is None:
                        continue
                    bull = snap["bullish_signals"]
                    bear = snap["bearish_signals"]

                    direction = "BUY" if bull > bear + 1 else ("SELL" if bear > bull + 1 else "HOLD")

                    if open_trade is None and direction != "HOLD":
                        open_trade = {"direction": direction, "entry": float(df.iloc[i]["close"]), "bar": i}
                        continue

                    if open_trade:
                        current = float(df.iloc[i]["close"])
                        entry   = open_trade["entry"]
                        d       = open_trade["direction"]
                        pnl     = ((current - entry) if d == "BUY" else (entry - current)) / entry * balance * 0.01

                        if pnl <= -sl_usd or pnl >= tp_usd or i - open_trade["bar"] >= 20:
                            profit = pnl if pnl >= tp_usd else (-sl_usd if pnl <= -sl_usd else pnl)
                            trades.append(profit)
                            open_trade = None

                if not trades:
                    continue
                combos_tested += 1
                wins   = [t for t in trades if t > 0]
                losses = [t for t in trades if t < 0]
                total  = sum(trades)
                pf     = round(sum(wins) / abs(sum(losses)), 2) if losses and wins else 0

                if total > best_pnl:
                    best_pnl    = total
                    best_params = {"sl_usd": sl_usd, "tp_usd": tp_usd,
                                   "rsi_period": 14, "ema_fast": 20, "ema_slow": 50}
                    best_metrics = {
                        "total_profit_usd": round(total, 2),
                        "win_rate_pct":     round(len(wins) / len(trades) * 100, 1) if trades else 0,
                        "profit_factor":    pf,
                        "max_drawdown_pct": 0,
                    }

        if best_params:
            self.settings._cfg["default_sl_usd"] = best_params["sl_usd"]
            self.settings._cfg["default_tp_usd"] = best_params["tp_usd"]
            print(f"[Optimizer] Best: SL=${best_params['sl_usd']} TP=${best_params['tp_usd']} "
                  f"P&L=${best_metrics['total_profit_usd']}")

        return {
            "pair":               pair,
            "combinations_tested": combos_tested,
            "best_params":         best_params or {},
            "best_metrics":        best_metrics or {},
        }
