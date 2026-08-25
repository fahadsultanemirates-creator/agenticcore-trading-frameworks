"""
Agent — Trade Memory (NEW in Tier 2)
Tracks per-pair performance from historical trades.
Adjusts confidence scores based on recent win rates.
Pairs that have consistently performed well get a boost;
pairs that have been consistently losing get penalised.

Memory is persisted to logs/trade_memory.json and survives restarts.
"""
import json
import asyncio
from pathlib import Path
from datetime import datetime
from typing import Optional


MEMORY_PATH = Path(__file__).parent.parent / "logs" / "trade_memory.json"


def _load_memory() -> dict:
    if not MEMORY_PATH.exists():
        return {}
    try:
        return json.loads(MEMORY_PATH.read_text())
    except Exception:
        return {}


def _save_memory(data: dict):
    MEMORY_PATH.parent.mkdir(exist_ok=True)
    MEMORY_PATH.write_text(json.dumps(data, indent=2))


class TradeMemoryAgent:
    def __init__(self, settings):
        cfg = settings.get("trade_memory", {})
        self.enabled                = cfg.get("enabled", True)
        self.min_trades             = cfg.get("min_trades_for_adjustment", 5)
        self.high_threshold         = cfg.get("high_win_rate_threshold", 60)
        self.low_threshold          = cfg.get("low_win_rate_threshold", 35)
        self.high_bonus             = cfg.get("high_win_rate_bonus", 10)
        self.low_penalty            = cfg.get("low_win_rate_penalty", -15)
        self.lookback               = cfg.get("lookback_trades", 20)
        self._memory: dict          = _load_memory()
        self._settings              = settings

    def get_adjustments(self) -> dict[str, int]:
        """
        Returns { pair: confidence_adjustment } for all known pairs.
        Pairs with insufficient history return 0.
        """
        if not self.enabled:
            return {}

        adjustments = {}
        for pair, data in self._memory.items():
            recent = data.get("recent_results", [])[-self.lookback:]
            if len(recent) < self.min_trades:
                adjustments[pair] = 0
                continue

            wins     = sum(1 for r in recent if r > 0)
            win_rate = wins / len(recent) * 100

            if win_rate >= self.high_threshold:
                adjustments[pair] = self.high_bonus
            elif win_rate <= self.low_threshold:
                adjustments[pair] = self.low_penalty
            else:
                adjustments[pair] = 0

            print(f"[Memory] {pair}: win_rate={win_rate:.1f}% ({len(recent)} trades) → adjustment={adjustments[pair]:+d}")

        return adjustments

    async def record_trade(self, pair: str, profit: float, idempotency_key: str = ""):
        """Record a closed trade result. Call from TradeExecutionAgent after close."""
        if not self.enabled:
            return

        # Strip broker suffix for consistent keys
        clean_pair = self._settings.strip_suffix(pair) if hasattr(self._settings, "strip_suffix") else pair

        def _update():
            mem = _load_memory()
            entry = mem.setdefault(clean_pair, {
                "total_trades": 0,
                "wins": 0,
                "losses": 0,
                "total_profit": 0.0,
                "recent_results": [],
                "last_updated": "",
                "processed_trade_ids": [],
            })
            processed = entry.setdefault("processed_trade_ids", [])
            if idempotency_key and idempotency_key in processed:
                return False
            entry["total_trades"] += 1
            entry["total_profit"]  = round(entry["total_profit"] + profit, 2)
            if profit >= 0:
                entry["wins"] += 1
            else:
                entry["losses"] += 1
            entry["recent_results"].append(round(profit, 2))
            # Keep only last N results
            entry["recent_results"] = entry["recent_results"][-self.lookback:]
            entry["last_updated"] = datetime.utcnow().isoformat()
            if idempotency_key:
                entry["processed_trade_ids"] = (processed + [idempotency_key])[-200:]
            mem[clean_pair] = entry
            _save_memory(mem)
            self._memory = mem
            return True

        recorded = await asyncio.to_thread(_update)
        if recorded:
            print(f"[Memory] Recorded trade for {clean_pair}: P&L=${profit:+.2f}")
        return recorded

    def get_summary(self) -> str:
        """Telegram-formatted memory summary."""
        if not self._memory:
            return "*Trade Memory* — No data yet."

        lines = ["*Trade Memory — Per-Pair Performance*\n"]
        for pair, d in sorted(self._memory.items()):
            recent  = d.get("recent_results", [])[-self.lookback:]
            total   = len(recent)
            if total == 0:
                continue
            wins     = sum(1 for r in recent if r > 0)
            win_rate = wins / total * 100
            adj      = self.get_adjustments().get(pair, 0)
            emoji    = "🟢" if adj > 0 else ("🔴" if adj < 0 else "⚪")
            lines.append(
                f"{emoji} `{pair}` — WR: `{win_rate:.0f}%` ({total} trades) | adj: `{adj:+d}`"
            )

        return "\n".join(lines)
