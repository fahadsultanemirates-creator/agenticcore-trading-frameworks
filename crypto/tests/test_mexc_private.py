"""
Tests for adapters/mexc_private.py

All tests use injected fake transport – the real network is never called.
Covers:
  - HMAC-SHA256 signature presence in request headers
  - timestamp / recv-window injection
  - Account assets / single balance
  - Open positions
  - Open orders
  - Account info / status
  - Error cases: non-200, malformed JSON wrapper, missing success, API errors
  - Credential validation at construction
  - Clock / signature error codes → correct exception types
  - Proof that no order-submission, cancel, leverage, or withdrawal methods exist
"""

from __future__ import annotations

import hashlib
import hmac
import sys
import os
import unittest
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from adapters.mexc_private import (
    MexcPrivateClient,
    MexcPrivateError,
    MexcCredentialError,
    MexcClockError,
    AccountAsset,
    OpenPosition,
    OpenOrder,
    _build_signature,
)


# ── Fake transport helpers ─────────────────────────────────────────────────────

class CaptureTransport:
    """Records the last call for inspection."""

    def __init__(self, status: int, body: dict) -> None:
        self.status = status
        self.body = body
        self.calls: List[Tuple[str, Dict[str, str], int]] = []

    def __call__(self, url: str, headers: Dict[str, str], timeout: int) -> Tuple[int, dict]:
        self.calls.append((url, dict(headers), timeout))
        return self.status, self.body


def _transport(status: int, body: dict) -> CaptureTransport:
    return CaptureTransport(status, body)


def _ok(data: Any) -> dict:
    return {"success": True, "data": data}


def _err(code: int, message: str = "error") -> dict:
    return {"success": False, "code": code, "message": message}


# ── Signature tests ────────────────────────────────────────────────────────────

class TestSignature(unittest.TestCase):
    def test_build_signature_is_hmac_sha256(self):
        ak = "myaccesskey"
        sk = "mysecretkey"
        ts = 1_700_000_000_000
        ps = "currency=USDT"

        expected = hmac.new(
            sk.encode(), f"{ak}{ts}{ps}".encode(), hashlib.sha256
        ).hexdigest()
        self.assertEqual(_build_signature(sk, ak, ts, ps), expected)

    def test_signature_in_request_headers(self):
        t = _transport(200, _ok([]))
        client = MexcPrivateClient("AK", "SK", http_get=t)
        client.get_account_assets()
        _, headers, _ = t.calls[0]
        self.assertIn("ApiKey", headers)
        self.assertIn("Signature", headers)
        self.assertIn("Request-Time", headers)
        self.assertIn("Recv-Window", headers)
        self.assertEqual(headers["ApiKey"], "AK")
        expected = hmac.new(
            b"SK",
            f"AK{headers['Request-Time']}".encode(),
            hashlib.sha256,
        ).hexdigest()
        self.assertEqual(headers["Signature"], expected)

    def test_access_key_not_in_url(self):
        t = _transport(200, _ok([]))
        client = MexcPrivateClient("SECRETKEY123", "SK", http_get=t)
        client.get_account_assets()
        url, _, _ = t.calls[0]
        self.assertNotIn("SECRETKEY123", url)

    def test_secret_key_never_in_headers(self):
        sk = "SUPERSECRETVALUE"
        t = _transport(200, _ok([]))
        client = MexcPrivateClient("AK", sk, http_get=t)
        client.get_account_assets()
        _, headers, _ = t.calls[0]
        for v in headers.values():
            self.assertNotIn(sk, v)


# ── Credential validation ──────────────────────────────────────────────────────

class TestCredentialValidation(unittest.TestCase):
    def test_empty_access_key_raises(self):
        with self.assertRaises(MexcCredentialError):
            MexcPrivateClient("", "secret")

    def test_whitespace_access_key_raises(self):
        with self.assertRaises(MexcCredentialError):
            MexcPrivateClient("   ", "secret")

    def test_empty_secret_key_raises(self):
        with self.assertRaises(MexcCredentialError):
            MexcPrivateClient("access", "")

    def test_whitespace_secret_key_raises(self):
        with self.assertRaises(MexcCredentialError):
            MexcPrivateClient("access", "   ")

    def test_repr_does_not_expose_credentials(self):
        ak = "AKVERYSECRET"
        sk = "SKVERYSECRET"
        client = MexcPrivateClient(ak, sk, http_get=_transport(200, _ok([])))
        r = repr(client)
        self.assertNotIn(ak, r)
        self.assertNotIn(sk, r)


# ── Error handling ─────────────────────────────────────────────────────────────

class TestErrorHandling(unittest.TestCase):
    def test_non_200_raises_private_error(self):
        t = _transport(500, {"success": False, "code": 500, "message": "server error"})
        client = MexcPrivateClient("AK", "SK", http_get=t)
        with self.assertRaises(MexcPrivateError):
            client.get_account_assets()

    def test_401_raises_credential_error(self):
        t = _transport(401, {"success": False, "code": 401, "message": "Unauthorized"})
        client = MexcPrivateClient("AK", "SK", http_get=t)
        with self.assertRaises(MexcCredentialError):
            client.get_account_assets()

    def test_api_success_false_raises(self):
        t = _transport(200, _err(10001, "invalid signature"))
        client = MexcPrivateClient("AK", "SK", http_get=t)
        with self.assertRaises(MexcCredentialError):
            client.get_account_assets()

    def test_clock_error_code_raises_clock_error(self):
        t = _transport(200, _err(20001, "timestamp out of range"))
        client = MexcPrivateClient("AK", "SK", http_get=t)
        with self.assertRaises(MexcClockError):
            client.get_account_assets()

    def test_generic_api_error_code(self):
        t = _transport(200, _err(50000, "internal error"))
        client = MexcPrivateClient("AK", "SK", http_get=t)
        with self.assertRaises(MexcPrivateError):
            client.get_account_assets()

    def test_credential_error_message_does_not_contain_key(self):
        ak = "AKLEAKCHECK"
        sk = "SKLEAKCHECK"
        t = _transport(200, _err(10001, f"invalid key {ak}"))
        client = MexcPrivateClient(ak, sk, http_get=t)
        try:
            client.get_account_assets()
        except MexcCredentialError as exc:
            msg = str(exc)
            self.assertNotIn(ak, msg)
            self.assertNotIn(sk, msg)
        else:
            self.fail("Expected MexcCredentialError")

    def test_malformed_response_non_dict_raises(self):
        # Transport returns a list instead of dict
        def bad_transport(url, headers, timeout):
            return 200, []  # type: ignore[return-value]

        client = MexcPrivateClient("AK", "SK", http_get=bad_transport)
        with self.assertRaises(MexcPrivateError):
            client.get_account_assets()

    def test_wrong_data_type_for_assets_raises(self):
        t = _transport(200, _ok("not-a-list"))
        client = MexcPrivateClient("AK", "SK", http_get=t)
        with self.assertRaises(MexcPrivateError):
            client.get_account_assets()

    def test_wrong_data_type_for_positions_raises(self):
        t = _transport(200, _ok("not-a-list"))
        client = MexcPrivateClient("AK", "SK", http_get=t)
        with self.assertRaises(MexcPrivateError):
            client.get_open_positions()

# ── Account assets ─────────────────────────────────────────────────────────────

class TestAccountAssets(unittest.TestCase):
    def _make_asset_body(self) -> dict:
        return _ok([
            {
                "currency": "USDT",
                "positionMargin": "500.0",
                "availableBalance": "9500.0",
                "cashBalance": "10000.0",
                "unrealizedProfit": "25.5",
            },
            {
                "currency": "BTC",
                "positionMargin": None,
                "availableBalance": "0.05",
                "cashBalance": "0.05",
                "unrealizedProfit": None,
            },
        ])

    def test_parses_asset_list(self):
        t = _transport(200, self._make_asset_body())
        client = MexcPrivateClient("AK", "SK", http_get=t)
        assets = client.get_account_assets()
        self.assertEqual(len(assets), 2)

    def test_asset_fields(self):
        t = _transport(200, self._make_asset_body())
        client = MexcPrivateClient("AK", "SK", http_get=t)
        assets = client.get_account_assets()
        usdt = assets[0]
        self.assertEqual(usdt.currency, "USDT")
        self.assertAlmostEqual(usdt.position_margin, 500.0)
        self.assertAlmostEqual(usdt.available_balance, 9500.0)
        self.assertAlmostEqual(usdt.cash_balance, 10000.0)
        self.assertAlmostEqual(usdt.unrealised_pnl, 25.5)
        self.assertIsNotNone(usdt.fetched_at)

    def test_asset_null_numerics(self):
        t = _transport(200, self._make_asset_body())
        client = MexcPrivateClient("AK", "SK", http_get=t)
        assets = client.get_account_assets()
        btc = assets[1]
        self.assertIsNone(btc.position_margin)
        self.assertIsNone(btc.unrealised_pnl)

    def test_skips_items_without_currency(self):
        body = _ok([
            {"currency": "USDT", "availableBalance": "100.0"},
            {"positionMargin": "50.0"},  # no currency
        ])
        t = _transport(200, body)
        client = MexcPrivateClient("AK", "SK", http_get=t)
        assets = client.get_account_assets()
        self.assertEqual(len(assets), 1)

    def test_empty_asset_list(self):
        t = _transport(200, _ok([]))
        client = MexcPrivateClient("AK", "SK", http_get=t)
        assets = client.get_account_assets()
        self.assertEqual(assets, [])

    def test_currency_normalized_uppercase(self):
        body = _ok([{"currency": "usdt", "availableBalance": "100"}])
        t = _transport(200, body)
        client = MexcPrivateClient("AK", "SK", http_get=t)
        assets = client.get_account_assets()
        self.assertEqual(assets[0].currency, "USDT")


# ── Single asset balance ───────────────────────────────────────────────────────

class TestAssetBalance(unittest.TestCase):
    def test_returns_single_asset(self):
        body = _ok({
            "currency": "USDT",
            "availableBalance": "1234.56",
            "cashBalance": "2000.00",
            "positionMargin": "765.44",
            "unrealizedProfit": "-5.0",
        })
        t = _transport(200, body)
        client = MexcPrivateClient("AK", "SK", http_get=t)
        asset = client.get_asset_balance("USDT")
        self.assertIsInstance(asset, AccountAsset)
        self.assertEqual(asset.currency, "USDT")
        self.assertAlmostEqual(asset.available_balance, 1234.56)

    def test_url_contains_currency(self):
        body = _ok({"currency": "USDT", "availableBalance": "100"})
        t = _transport(200, body)
        client = MexcPrivateClient("AK", "SK", http_get=t)
        client.get_asset_balance("usdt")
        url, _, _ = t.calls[0]
        self.assertIn("USDT", url)

    def test_empty_currency_raises(self):
        t = _transport(200, _ok({}))
        client = MexcPrivateClient("AK", "SK", http_get=t)
        with self.assertRaises(MexcPrivateError):
            client.get_asset_balance("")

    def test_non_dict_response_raises(self):
        t = _transport(200, _ok([]))
        client = MexcPrivateClient("AK", "SK", http_get=t)
        with self.assertRaises(MexcPrivateError):
            client.get_asset_balance("USDT")


# ── Open positions ─────────────────────────────────────────────────────────────

class TestOpenPositions(unittest.TestCase):
    def _pos_body(self) -> dict:
        return _ok([
            {
                "symbol": "BTC_USDT",
                "positionId": 12345,
                "positionType": 1,
                "holdVol": "2.0",
                "openAvgPrice": "65000.0",
                "markPrice": "66000.0",
                "unrealizedProfit": "2000.0",
                "leverage": 10,
                "im": "13000.0",
            }
        ])

    def test_parses_position_list(self):
        t = _transport(200, self._pos_body())
        client = MexcPrivateClient("AK", "SK", http_get=t)
        positions = client.get_open_positions()
        self.assertEqual(len(positions), 1)

    def test_position_fields(self):
        t = _transport(200, self._pos_body())
        client = MexcPrivateClient("AK", "SK", http_get=t)
        p = client.get_open_positions()[0]
        self.assertEqual(p.symbol, "BTC_USDT")
        self.assertEqual(p.position_id, "12345")
        self.assertEqual(p.side, "1")
        self.assertAlmostEqual(p.hold_vol, 2.0)
        self.assertAlmostEqual(p.open_price, 65000.0)
        self.assertAlmostEqual(p.mark_price, 66000.0)
        self.assertAlmostEqual(p.unrealised_pnl, 2000.0)
        self.assertEqual(p.leverage, 10)
        self.assertAlmostEqual(p.margin, 13000.0)

    def test_symbol_filter_sent_as_param(self):
        t = _transport(200, _ok([]))
        client = MexcPrivateClient("AK", "SK", http_get=t)
        client.get_open_positions(symbol="eth_usdt")
        url, _, _ = t.calls[0]
        self.assertIn("symbol=ETH_USDT", url)

    def test_empty_positions_list(self):
        t = _transport(200, _ok([]))
        client = MexcPrivateClient("AK", "SK", http_get=t)
        positions = client.get_open_positions()
        self.assertEqual(positions, [])

    def test_skips_non_dict_items(self):
        body = _ok([
            {"symbol": "BTC_USDT", "holdVol": "1.0"},
            "bad-entry",
            None,
        ])
        t = _transport(200, body)
        client = MexcPrivateClient("AK", "SK", http_get=t)
        positions = client.get_open_positions()
        self.assertEqual(len(positions), 1)

    def test_skips_positions_without_symbol(self):
        body = _ok([
            {"holdVol": "1.0"},  # no symbol
            {"symbol": "ETH_USDT", "holdVol": "3.0"},
        ])
        t = _transport(200, body)
        client = MexcPrivateClient("AK", "SK", http_get=t)
        positions = client.get_open_positions()
        self.assertEqual(len(positions), 1)
        self.assertEqual(positions[0].symbol, "ETH_USDT")


# ── Open orders ────────────────────────────────────────────────────────────────

class TestOpenOrders(unittest.TestCase):
    def _order_body(self) -> dict:
        return _ok([
            {
                "orderId": "ORD001",
                "symbol": "BTC_USDT",
                "side": 1,
                "price": "65000.0",
                "vol": "1.0",
                "dealAvgPrice": "0.0",
                "dealVol": "0.0",
                "orderType": 1,
                "state": 2,
                "createTime": 1700000000000,
            }
        ])

    def test_parses_order_list(self):
        t = _transport(200, self._order_body())
        client = MexcPrivateClient("AK", "SK", http_get=t)
        orders = client.get_open_orders()
        self.assertEqual(len(orders), 1)

    def test_order_fields(self):
        t = _transport(200, self._order_body())
        client = MexcPrivateClient("AK", "SK", http_get=t)
        o = client.get_open_orders()[0]
        self.assertEqual(o.order_id, "ORD001")
        self.assertEqual(o.symbol, "BTC_USDT")
        self.assertEqual(o.side, 1)
        self.assertAlmostEqual(o.price, 65000.0)
        self.assertAlmostEqual(o.vol, 1.0)
        self.assertEqual(o.state, 2)
        self.assertIsNotNone(o.create_time)

    def test_url_uses_pagination_parameters(self):
        t = _transport(200, _ok([]))
        client = MexcPrivateClient("AK", "SK", http_get=t)
        client.get_open_orders()
        url, _, _ = t.calls[0]
        self.assertIn("page_num=1", url)
        self.assertIn("page_size=100", url)

    def test_symbol_filter_is_local(self):
        t = _transport(200, self._order_body())
        client = MexcPrivateClient("AK", "SK", http_get=t)
        self.assertEqual(client.get_open_orders("ETH_USDT"), [])

    def test_wrapped_resultlist_format(self):
        body = _ok({"resultList": [
            {"orderId": "X1", "symbol": "SOL_USDT", "price": "150.0"}
        ]})
        t = _transport(200, body)
        client = MexcPrivateClient("AK", "SK", http_get=t)
        orders = client.get_open_orders()
        self.assertEqual(len(orders), 1)
        self.assertEqual(orders[0].order_id, "X1")

    def test_empty_orders_list(self):
        t = _transport(200, _ok([]))
        client = MexcPrivateClient("AK", "SK", http_get=t)
        orders = client.get_open_orders()
        self.assertEqual(orders, [])

    def test_create_time_converted_to_iso(self):
        body = _ok([{"orderId": "T1", "symbol": "BTC_USDT", "createTime": 1700000000000}])
        t = _transport(200, body)
        client = MexcPrivateClient("AK", "SK", http_get=t)
        orders = client.get_open_orders()
        self.assertIsNotNone(orders[0].create_time)
        self.assertIn("2023", orders[0].create_time)  # 2023-11-xx

    def test_null_create_time(self):
        body = _ok([{"orderId": "T2", "symbol": "BTC_USDT", "createTime": None}])
        t = _transport(200, body)
        client = MexcPrivateClient("AK", "SK", http_get=t)
        orders = client.get_open_orders()
        self.assertIsNone(orders[0].create_time)


# ── Proof: no execution methods exist ─────────────────────────────────────────

class TestNoExecutionMethods(unittest.TestCase):
    """
    Prove that MexcPrivateClient exposes no order submission, cancellation,
    leverage mutation, or withdrawal methods.  Any addition of such methods
    must make these tests fail.
    """

    FORBIDDEN_NAMES = [
        # Order submission
        "place_order", "submit_order", "create_order", "new_order",
        "open_order", "send_order", "post_order",
        # Order cancellation
        "cancel_order", "cancel_all", "cancel_orders",
        # Leverage
        "set_leverage", "change_leverage", "update_leverage",
        # Margin mode
        "set_margin_mode", "change_margin_mode",
        # Withdrawals / transfers
        "withdraw", "transfer", "submit_withdrawal",
        # Generic mutation helpers
        "_post", "_delete", "_put",
    ]

    def test_forbidden_methods_absent(self):
        client = MexcPrivateClient("AK", "SK", http_get=_transport(200, _ok([])))
        for name in self.FORBIDDEN_NAMES:
            self.assertFalse(
                hasattr(client, name),
                msg=f"MexcPrivateClient must not have method: {name}",
            )

    def test_public_methods_are_only_readonly(self):
        """
        All public instance methods must be from an approved read-only list.
        """
        allowed = {
            "get_account_assets",
            "get_asset_balance",
            "get_open_positions",
            "get_open_orders",
            "get_order",
            "get_open_tpsl_orders",
        }
        client = MexcPrivateClient("AK", "SK", http_get=_transport(200, _ok([])))
        public_methods = {
            name for name in dir(client)
            if not name.startswith("_") and callable(getattr(client, name))
        }
        extra = public_methods - allowed
        self.assertEqual(
            extra, set(),
            msg=f"Unexpected public method(s) on MexcPrivateClient: {extra}",
        )


# ── recv_window is configurable ────────────────────────────────────────────────

class TestRecvWindow(unittest.TestCase):
    def test_custom_recv_window_in_header(self):
        t = _transport(200, _ok([]))
        client = MexcPrivateClient("AK", "SK", http_get=t, recv_window=5000)
        client.get_account_assets()
        _, headers, _ = t.calls[0]
        self.assertEqual(headers["Recv-Window"], "5000")

    def test_default_recv_window_present(self):
        t = _transport(200, _ok([]))
        client = MexcPrivateClient("AK", "SK", http_get=t)
        client.get_account_assets()
        _, headers, _ = t.calls[0]
        self.assertIn("Recv-Window", headers)
        self.assertTrue(int(headers["Recv-Window"]) > 0)


# ── Timestamp in request ───────────────────────────────────────────────────────

class TestTimestamp(unittest.TestCase):
    def test_request_time_header_is_numeric_ms(self):
        import time as _time
        before = int(_time.time() * 1000)
        t = _transport(200, _ok([]))
        client = MexcPrivateClient("AK", "SK", http_get=t)
        client.get_account_assets()
        _, headers, _ = t.calls[0]
        ts = int(headers["Request-Time"])
        after = int(_time.time() * 1000)
        self.assertGreaterEqual(ts, before)
        self.assertLessEqual(ts, after + 1000)


if __name__ == "__main__":
    unittest.main()
