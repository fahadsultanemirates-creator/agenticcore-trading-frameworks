"""
Tests for adapters/mexc_public.py – response parsing with injected transport.
Never calls the real network.
"""

import sys
import os
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from adapters.mexc_public import MexcPublicClient, MexcApiError


def _make_transport(status: int, body: dict):
    """Return a transport callable that returns a fixed response."""
    def _get(url: str, timeout: int) -> tuple:
        return status, body
    return _get


class TestContractList(unittest.TestCase):
    def test_parses_valid_contract_list(self):
        body = {
            "success": True,
            "data": [
                {
                    "symbol": "BTC_USDT",
                    "displayName": "BTCUSDT",
                    "baseCoin": "BTC",
                    "quoteCoin": "USDT",
                    "contractSize": "0.001",
                    "volUnit": "1",
                    "minVol": "1",
                    "maxVol": "100000",
                    "priceScale": "1",
                    "priceUnit": "0.1",
                    "volScale": "0",
                    "state": "1",
                },
            ],
        }
        client = MexcPublicClient(http_get=_make_transport(200, body))
        contracts = client.get_contract_list()
        self.assertEqual(len(contracts), 1)
        c = contracts[0]
        self.assertEqual(c.symbol, "BTC_USDT")
        self.assertAlmostEqual(c.contract_size, 0.001)
        self.assertAlmostEqual(c.price_increment, 0.1)
        self.assertTrue(c.is_active)

    def test_raises_on_api_error(self):
        body = {"success": False, "code": 10001, "message": "Symbol not found"}
        client = MexcPublicClient(http_get=_make_transport(200, body))
        with self.assertRaises(MexcApiError):
            client.get_contract_list()

    def test_raises_on_http_error(self):
        client = MexcPublicClient(http_get=_make_transport(500, {"error": "Server error"}))
        with self.assertRaises(MexcApiError):
            client.get_contract_list()

    def test_skips_items_without_symbol(self):
        body = {
            "success": True,
            "data": [
                {"symbol": "ETH_USDT", "state": "1"},
                {"displayName": "no symbol field"},
            ],
        }
        client = MexcPublicClient(http_get=_make_transport(200, body))
        contracts = client.get_contract_list()
        self.assertEqual(len(contracts), 1)
        self.assertEqual(contracts[0].symbol, "ETH_USDT")

    def test_handles_null_numerics(self):
        body = {
            "success": True,
            "data": [
                {
                    "symbol": "SOL_USDT",
                    "contractSize": None,
                    "volUnit": None,
                    "minVol": None,
                    "maxVol": None,
                    "state": "1",
                }
            ],
        }
        client = MexcPublicClient(http_get=_make_transport(200, body))
        contracts = client.get_contract_list()
        self.assertEqual(len(contracts), 1)
        self.assertIsNone(contracts[0].contract_size)
        self.assertIsNone(contracts[0].volume_step)


class TestTickers(unittest.TestCase):
    def test_parses_ticker_list(self):
        body = {
            "success": True,
            "data": [
                {
                    "symbol": "BTC_USDT",
                    "lastPrice": "67000.0",
                    "bid1": "66999.0",
                    "ask1": "67001.0",
                    "volume24": "50000",
                    "amount24": "3350000000",
                    "riseFallRate": "0.012",
                }
            ],
        }
        client = MexcPublicClient(http_get=_make_transport(200, body))
        tickers = client.get_all_tickers()
        self.assertEqual(len(tickers), 1)
        t = tickers[0]
        self.assertEqual(t.symbol, "BTC_USDT")
        self.assertAlmostEqual(t.last_price, 67000.0)
        self.assertIsNotNone(t.spread_pct)
        self.assertGreater(t.spread_pct, 0)

    def test_spread_computed_correctly(self):
        body = {
            "success": True,
            "data": [
                {
                    "symbol": "ETH_USDT",
                    "lastPrice": "3000.0",
                    "bid1": "2999.0",
                    "ask1": "3001.0",
                    "amount24": "1000000",
                }
            ],
        }
        client = MexcPublicClient(http_get=_make_transport(200, body))
        tickers = client.get_all_tickers()
        t = tickers[0]
        # spread = (3001-2999)/3000 * 100 = 0.0667%
        self.assertAlmostEqual(t.spread_pct, 0.066667, places=3)

    def test_handles_missing_bid_ask(self):
        body = {
            "success": True,
            "data": [
                {"symbol": "SOL_USDT", "lastPrice": "150.0"},
            ],
        }
        client = MexcPublicClient(http_get=_make_transport(200, body))
        tickers = client.get_all_tickers()
        t = tickers[0]
        self.assertIsNone(t.spread_pct)


class TestCandles(unittest.TestCase):
    def test_parses_dict_format(self):
        body = {
            "success": True,
            "data": {
                "time": [1700000000, 1700000900, 1700001800],
                "open": ["100.0", "101.0", "102.0"],
                "high": ["105.0", "106.0", "107.0"],
                "low": ["99.0", "100.0", "101.0"],
                "close": ["101.0", "102.0", "103.0"],
                "vol": ["1000", "1100", "1200"],
            },
        }
        client = MexcPublicClient(http_get=_make_transport(200, body))
        candles = client.get_candles("BTC_USDT")
        self.assertEqual(len(candles), 3)
        # Last candle is not complete
        self.assertFalse(candles[-1].is_complete)
        # Others are complete
        self.assertTrue(candles[0].is_complete)
        self.assertAlmostEqual(candles[0].close, 101.0)

    def test_parses_list_format(self):
        body = {
            "success": True,
            "data": [
                [1700000000, "100.0", "105.0", "99.0", "101.0", "1000"],
                [1700000900, "101.0", "106.0", "100.0", "102.0", "1100"],
            ],
        }
        client = MexcPublicClient(http_get=_make_transport(200, body))
        candles = client.get_candles("BTC_USDT")
        self.assertEqual(len(candles), 2)
        self.assertFalse(candles[-1].is_complete)


class TestFunding(unittest.TestCase):
    def test_parses_funding_rate(self):
        body = {
            "success": True,
            "data": {
                "fundingRate": "0.0001",
                "nextSettleTime": 1700000000,
            },
        }
        client = MexcPublicClient(http_get=_make_transport(200, body))
        info = client.get_funding_rate("BTC_USDT")
        self.assertAlmostEqual(info.current_rate, 0.0001)
        self.assertIsNotNone(info.next_funding_time)

    def test_returns_null_on_error(self):
        body = {"success": False, "message": "not found"}
        client = MexcPublicClient(http_get=_make_transport(200, body))
        info = client.get_funding_rate("FAKE_USDT")
        self.assertIsNone(info.current_rate)


class TestOpenInterest(unittest.TestCase):
    def test_parses_oi(self):
        body = {
            "success": True,
            "data": {"openInterest": "1234567.89"},
        }
        client = MexcPublicClient(http_get=_make_transport(200, body))
        oi = client.get_open_interest("BTC_USDT")
        self.assertAlmostEqual(oi.value_usdt, 1234567.89)

    def test_returns_null_oi_on_error(self):
        body = {"success": False, "message": "not found"}
        client = MexcPublicClient(http_get=_make_transport(200, body))
        oi = client.get_open_interest("FAKE_USDT")
        self.assertIsNone(oi.value_usdt)


class TestPublicMicrostructure(unittest.TestCase):
    def test_parses_order_book_depth(self):
        body = {
            "success": True,
            "data": {
                "bids": [["100.0", "20"], ["99.9", "10"], ["bad", "1"]],
                "asks": [["100.1", "15"], ["100.2", "5"], ["100.3", "0"]],
            },
        }
        client = MexcPublicClient(http_get=_make_transport(200, body))
        book = client.get_order_book("BTC_USDT", limit=20)
        self.assertEqual(book.symbol, "BTC_USDT")
        self.assertEqual(book.bids, [(100.0, 20.0), (99.9, 10.0)])
        self.assertEqual(book.asks, [(100.1, 15.0), (100.2, 5.0)])

    def test_parses_recent_trades_and_aggressor_side(self):
        body = {
            "success": True,
            "data": [
                {"p": "100.0", "v": "10", "T": 1, "t": 1700000000000},
                {"p": "99.9", "v": "5", "T": 2, "t": 1700000001},
                {"p": "99.8", "v": "3", "T": 9},
            ],
        }
        client = MexcPublicClient(http_get=_make_transport(200, body))
        trades = client.get_recent_trades("BTC_USDT", limit=50)
        self.assertEqual([trade.side for trade in trades], ["buy", "sell", "unknown"])
        self.assertAlmostEqual(trades[0].price, 100.0)
        self.assertEqual(trades[1].timestamp_ms, 1700000001000)


if __name__ == "__main__":
    unittest.main()
