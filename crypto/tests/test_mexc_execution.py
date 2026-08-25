"""Tests for the isolated, explicitly invoked MEXC live-canary adapter."""

from __future__ import annotations

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from adapters.mexc_execution import MexcExecutionClient


def _transport(status: int, body: dict):
    calls = []

    def send(url, headers, payload, timeout):
        calls.append((url, headers, payload, timeout))
        return status, body

    send.calls = calls
    return send


class TestMexcExecutionClient(unittest.TestCase):
    def test_submits_atomic_protected_market_entry(self):
        transport = _transport(
            200,
            {"success": True, "code": 0, "data": {"orderId": "123", "ts": 1700000000000}},
        )
        client = MexcExecutionClient("AK", "SK", http_post=transport)
        submitted = client.submit_protected_market_entry(
            symbol="XRP_USDT",
            side="long",
            quantity=33,
            reference_price=1.491,
            stop_loss_price=1.47,
            take_profit_price=1.58,
            external_oid="ac-canary-test",
        )
        self.assertEqual(submitted.order_id, "123")
        self.assertEqual(submitted.external_oid, "ac-canary-test")
        url, headers, payload, _ = transport.calls[0]
        self.assertTrue(url.endswith("/api/v1/private/order/create"))
        self.assertIn("Signature", headers)
        body = json.loads(payload)
        self.assertEqual(body["type"], 5)
        self.assertEqual(body["openType"], 1)
        self.assertEqual(body["leverage"], 20)
        self.assertEqual(body["stopLossPrice"], 1.47)
        self.assertEqual(body["takeProfitPrice"], 1.58)

    def test_rejects_unprotected_or_invalid_entry_before_request(self):
        transport = _transport(200, {"success": True, "data": {"orderId": "123"}})
        client = MexcExecutionClient("AK", "SK", http_post=transport)
        with self.assertRaises(ValueError):
            client.submit_protected_market_entry(
                symbol="XRP_USDT",
                side="long",
                quantity=33,
                reference_price=1.491,
                stop_loss_price=1.50,
                take_profit_price=1.58,
                external_oid="ac-canary-test",
            )
        self.assertEqual(transport.calls, [])

    def test_secret_is_not_exposed_by_repr(self):
        client = MexcExecutionClient("public-key", "private-secret", http_post=_transport(200, {}))
        self.assertNotIn("private-secret", repr(client))
        self.assertNotIn("public-key", repr(client))


if __name__ == "__main__":
    unittest.main()