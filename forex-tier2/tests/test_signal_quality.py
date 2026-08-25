import asyncio
import unittest

import pandas as pd

from agents.signal_generator import SignalGeneratorAgent
from agents.volume_sense import completed_volume_snapshot


class Settings:
    min_signal_confidence = 65
    gemini_api_key = ""

    def __init__(self):
        self._cfg = {
            "gemini": {"use_llm_for_signals": False},
            "volume_sense": {
                "gate_forex_on_low_volume": True,
                "gate_metals_on_low_volume": False,
            },
            "confidence": {
                "max_score": 95,
                "max_mtf_adjustment": 20,
                "max_session_adjustment": 8,
                "max_session_penalty": 8,
                "max_memory_adjustment": 5,
                "high_volume_bonus": 3,
                "low_volume_penalty": 8,
                "exceptional_cap_without_full_evidence": 79,
            },
        }

    def get(self, key, default=None):
        return self._cfg.get(key, default)

    def strip_suffix(self, pair):
        return pair.removesuffix(".m")


def ta_snapshot(volume, confluence="WEAK (1/3)"):
    return {
        "rsi": 50,
        "trend": "UP",
        "macd_hist": 1.0,
        "bullish_signals": 6,
        "bearish_signals": 0,
        "confluence_bonus": 25,
        "confluence_summary": confluence,
        "volume": volume,
    }


class SignalQualityTests(unittest.TestCase):
    def test_volume_uses_latest_completed_candle_not_live_candle(self):
        candles = pd.DataFrame(
            {"tick_volume": [100] * 22 + [10_000]}
        )
        result = completed_volume_snapshot(
            candles, "EURUSD", {"lookback_candles": 20, "low_ratio": 0.65}
        )
        self.assertEqual(result["regime"], "normal")
        self.assertEqual(result["current"], 100.0)

    def test_low_completed_volume_blocks_forex_but_not_gold(self):
        agent = SignalGeneratorAgent(Settings())
        low = {"regime": "low", "ratio": 0.4, "reason": "weak participation"}

        forex = asyncio.run(agent.run({"EURUSD": ta_snapshot(low)}, []))
        gold = asyncio.run(agent.run({"XAUUSD": ta_snapshot(low)}, []))

        self.assertEqual(forex["EURUSD"]["direction"], "HOLD")
        self.assertIn("low volume", forex["EURUSD"]["summary"].lower())
        self.assertEqual(gold["XAUUSD"]["direction"], "BUY")

    def test_exceptional_confidence_requires_full_mtf_and_volume_evidence(self):
        agent = SignalGeneratorAgent(Settings())
        signals = asyncio.run(
            agent.run(
                {"EURUSD": ta_snapshot({"regime": "normal"}, "WEAK (1/3)")},
                [],
                session_bonus=20,
                memory_adjustments={"EURUSD": 20},
            )
        )
        signal = signals["EURUSD"]
        self.assertEqual(signal["direction"], "BUY")
        self.assertLessEqual(signal["confidence"], 79)
        self.assertEqual(signal["confidence_breakdown"]["session"], 8)
        self.assertEqual(signal["confidence_breakdown"]["memory"], 5)


if __name__ == "__main__":
    unittest.main()