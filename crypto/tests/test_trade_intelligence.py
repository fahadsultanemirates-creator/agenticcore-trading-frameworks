import os
import sys
import tempfile
import unittest
from dataclasses import replace
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from config.settings import CryptoSettings
from runtime.trade_intelligence import analyze_operator_report, analyze_paper_events
from storage.state import read_json_safe, write_json_atomic


def paper_event():
    return {
        "id": "paper_position_opened:one",
        "event": "paper_position_opened",
        "symbol": "BTC_USDT",
        "position": {
            "id": "one",
            "symbol": "BTC_USDT",
            "side": "long",
            "entry_price": 100.0,
            "stop_price": 99.0,
            "take_profit_price": 103.0,
            "confidence": 80,
            "condition_tags": ["entry_retest_confirmed", "cross_market_long"],
            "entry_evidence": {"cross_market_agreement": "long"},
        },
    }


class TestTradeIntelligence(unittest.TestCase):
    def test_disabled_ai_never_calls_providers(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "paper.json")
            summary = analyze_paper_events(
                replace(CryptoSettings(), ai_explanations_enabled=False),
                path,
                [paper_event()],
            )
        self.assertFalse(summary["enabled"])
        self.assertEqual(summary["processed"], 0)

    def test_provider_failures_are_saved_without_raising(self):
        settings = replace(CryptoSettings(), ai_explanations_enabled=True)
        event = paper_event()
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "paper.json")
            write_json_atomic(
                path,
                {"positions": [dict(event["position"])], "notification_outbox": [dict(event)]},
            )
            with patch(
                "runtime.trade_intelligence._openai_explanation",
                return_value={"provider": "openai", "status": "unavailable", "reason": "TimeoutError"},
            ):
                summary = analyze_paper_events(settings, path, [event])
            persisted = read_json_safe(path)
        self.assertEqual(summary["processed"], 1)
        reviews = persisted["positions"][0]["ai_explanations"][event["id"]]
        self.assertEqual(reviews[0]["status"], "unavailable")

    def test_operator_report_persists_openai_review_once(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = replace(
                CryptoSettings(),
                runtime_dir=directory,
                ai_explanations_enabled=True,
            )
            evidence = {
                "label": "Daily (20 Aug 2026)",
                "period_start": "2026-08-20",
                "period_end": "2026-08-20",
                "generated_at": "2026-08-20T23:59:00+04:00",
                "net_pnl_usdt": 1.25,
            }
            with patch(
                "runtime.trade_intelligence._openai_explanation",
                return_value={"provider": "openai", "status": "live", "text": "A factual result."},
            ) as openai:
                first = analyze_operator_report(settings, "daily-report:2026-08-20", evidence)
                second = analyze_operator_report(settings, "daily-report:2026-08-20", evidence)
            persisted = read_json_safe(os.path.join(directory, "operator_reports.json"))
        self.assertEqual(first[0]["text"], "A factual result.")
        self.assertEqual(second[0]["text"], "A factual result.")
        self.assertEqual(openai.call_count, 1)
        self.assertEqual(len(persisted["reports"]), 1)

    def test_profit_lock_event_receives_openai_explanation(self):
        settings = replace(CryptoSettings(), ai_explanations_enabled=True)
        event = paper_event()
        event["id"] = "paper_profit_lock:one"
        event["event"] = "paper_profit_lock"
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "paper.json")
            write_json_atomic(
                path,
                {"positions": [dict(event["position"])], "notification_outbox": [dict(event)]},
            )
            with patch(
                "runtime.trade_intelligence._openai_explanation",
                return_value={"provider": "openai", "status": "live", "text": "Protection is active."},
            ):
                summary = analyze_paper_events(settings, path, [event])
            persisted = read_json_safe(path)
        self.assertEqual(summary["processed"], 1)
        self.assertEqual(
            persisted["positions"][0]["ai_explanations"][event["id"]][0]["text"],
            "Protection is active.",
        )


if __name__ == "__main__":
    unittest.main()