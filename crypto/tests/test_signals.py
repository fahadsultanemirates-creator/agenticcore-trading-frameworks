"""
Tests for analysis/signals.py – bounded confidence, stale/unknown rejection.
"""

import sys
import os
import time
import unittest
from dataclasses import replace
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from config.settings import CryptoSettings
from domain.models import (
    Candle, CandidateStatus, ContractDetail, DataStatus, EntryStatus, FundingInfo, MarketSnapshot,
    OpenInterest, SignalStatus, Ticker,
)
from analysis.signals import score_snapshot


def _fresh_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _stale_iso() -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=600)).isoformat()


def _make_ticker(symbol: str, stale: bool = False, price: float = 100.0) -> Ticker:
    return Ticker(
        symbol=symbol,
        last_price=price,
        bid=price - 0.05,
        ask=price + 0.05,
        spread_pct=0.05,
        volume_24h=100000.0,
        turnover_24h_usdt=10_000_000.0,
        change_pct_24h=0.01,
        fetched_at=_stale_iso() if stale else _fresh_iso(),
    )


def _make_candles(symbol: str, trend: str = "long", count: int = 8) -> list:
    """Create completed candles with a clear trend. Use recent timestamps."""
    import time as _time
    candles = []
    base_price = 100.0
    now_ms = int(_time.time() * 1000)
    for i in range(count):
        price = base_price + (i * 0.5 if trend == "long" else -i * 0.5)
        # Place candles ending just before now (recent, not stale)
        open_time = now_ms - (count - i) * 900_000
        candles.append(
            Candle(
                symbol=symbol,
                interval="Min15",
                open_time=open_time,
                open=price - 0.2,
                high=price + 0.3,
                low=price - 0.4,
                close=price,
                volume=1000.0,
                is_complete=i < count - 1,  # last candle not complete
            )
        )
    return candles


def _make_funding(symbol: str, rate: float = 0.0001) -> FundingInfo:
    return FundingInfo(
        symbol=symbol,
        current_rate=rate,
        next_rate=None,
        next_funding_time=None,
        fetched_at=_fresh_iso(),
    )


def _make_oi(symbol: str, value: float = 5_000_000.0) -> OpenInterest:
    return OpenInterest(symbol=symbol, value_usdt=value, fetched_at=_fresh_iso())


def _make_contract(symbol: str) -> ContractDetail:
    return ContractDetail(
        symbol=symbol,
        display_name=symbol,
        base_coin=symbol.replace("_USDT", ""),
        quote_coin="USDT",
        contract_size=0.1,
        volume_step=1.0,
        min_quantity=1.0,
        max_quantity=100_000.0,
        price_precision=4,
        quantity_precision=0,
        is_active=True,
        fetched_at=_fresh_iso(),
        contract_type=1,
        concept_plates=["mc-trade-zone-layer1"],
        price_increment=0.0001,
    )


def _make_zone_candles(symbol: str, trend: str = "long") -> list[Candle]:
    """Create a multi-touch 15m range with a final directional zone reclaim."""
    closes = [
        101.0, 100.2, 101.0, 102.0, 100.1, 101.0, 102.0,
        100.2, 101.0, 102.0, 100.2, 101.0, 102.0, 101.5,
    ]
    if trend == "short":
        closes = [202.0 - price for price in closes]
    now_ms = int(time.time() * 1000)
    result = []
    for index, close in enumerate(closes):
        is_last = index == len(closes) - 1
        open_price = close - 0.1 if trend == "long" else close + 0.1
        low = close - 0.2
        high = close + 0.2
        if is_last:
            low = 100.9 if trend == "long" else 100.3
            high = 101.7 if trend == "long" else 101.1
        result.append(
            Candle(
                symbol=symbol,
                interval="Min15",
                open_time=now_ms - (len(closes) - index - 1) * 900_000,
                open=open_price,
                high=high,
                low=low,
                close=close,
                volume=2_000.0 if is_last else 1_000.0,
                is_complete=True,
            )
        )
    return result


class TestConfidenceBounds(unittest.TestCase):
    def _settings(self):
        return CryptoSettings()

    def test_confidence_between_0_and_100(self):
        """Confidence must always be in [0, 100]."""
        snap = MarketSnapshot(
            symbol="BTC_USDT",
            contract=None,
            ticker=_make_ticker("BTC_USDT"),
            candles=_make_candles("BTC_USDT"),
            funding=_make_funding("BTC_USDT"),
            open_interest=_make_oi("BTC_USDT"),
        )
        candidate = score_snapshot(snap, self._settings())
        self.assertIsNotNone(candidate.confidence)
        self.assertGreaterEqual(candidate.confidence, 0)
        self.assertLessEqual(candidate.confidence, 100)

    def test_no_ticker_returns_none_confidence(self):
        snap = MarketSnapshot(
            symbol="BTC_USDT",
            contract=None,
            ticker=None,
        )
        candidate = score_snapshot(snap, self._settings())
        self.assertIsNone(candidate.confidence)
        self.assertEqual(candidate.selection_status, CandidateStatus.REJECTED)

    def test_stale_ticker_returns_rejected(self):
        snap = MarketSnapshot(
            symbol="BTC_USDT",
            contract=None,
            ticker=_make_ticker("BTC_USDT", stale=True),
            candles=_make_candles("BTC_USDT"),
        )
        candidate = score_snapshot(snap, self._settings())
        self.assertEqual(candidate.selection_status, CandidateStatus.REJECTED)
        self.assertEqual(candidate.data_status, DataStatus.STALE)

    def test_missing_candles_lowers_confidence(self):
        # With candles
        snap_full = MarketSnapshot(
            symbol="ETH_USDT",
            contract=None,
            ticker=_make_ticker("ETH_USDT"),
            candles=_make_candles("ETH_USDT"),
            funding=_make_funding("ETH_USDT"),
            open_interest=_make_oi("ETH_USDT"),
        )
        # Without candles
        snap_empty = MarketSnapshot(
            symbol="ETH_USDT",
            contract=None,
            ticker=_make_ticker("ETH_USDT"),
            candles=[],
            funding=_make_funding("ETH_USDT"),
            open_interest=_make_oi("ETH_USDT"),
        )
        c_full = score_snapshot(snap_full, self._settings())
        c_empty = score_snapshot(snap_empty, self._settings())
        self.assertIsNotNone(c_full.confidence)
        self.assertIsNotNone(c_empty.confidence)
        self.assertGreater(c_full.confidence, c_empty.confidence)

    def test_unknown_oi_lowers_confidence(self):
        snap_with_oi = MarketSnapshot(
            symbol="SOL_USDT",
            contract=None,
            ticker=_make_ticker("SOL_USDT"),
            candles=_make_candles("SOL_USDT"),
            funding=_make_funding("SOL_USDT"),
            open_interest=_make_oi("SOL_USDT"),
        )
        snap_no_oi = MarketSnapshot(
            symbol="SOL_USDT",
            contract=None,
            ticker=_make_ticker("SOL_USDT"),
            candles=_make_candles("SOL_USDT"),
            funding=_make_funding("SOL_USDT"),
            open_interest=None,
        )
        c_with = score_snapshot(snap_with_oi, self._settings())
        c_without = score_snapshot(snap_no_oi, self._settings())
        self.assertGreaterEqual(c_with.confidence, c_without.confidence)

    def test_trend_and_funding_agreement_raises_confidence(self):
        # Long trend + negative funding (long bias) → agreement → higher confidence
        snap_agree = MarketSnapshot(
            symbol="XRP_USDT",
            contract=None,
            ticker=_make_ticker("XRP_USDT"),
            candles=_make_candles("XRP_USDT", trend="long"),
            funding=_make_funding("XRP_USDT", rate=-0.001),  # negative → long bias
            open_interest=_make_oi("XRP_USDT"),
        )
        snap_diverge = MarketSnapshot(
            symbol="XRP_USDT",
            contract=None,
            ticker=_make_ticker("XRP_USDT"),
            candles=_make_candles("XRP_USDT", trend="long"),
            funding=_make_funding("XRP_USDT", rate=0.001),   # positive → short bias
            open_interest=_make_oi("XRP_USDT"),
        )
        c_agree = score_snapshot(snap_agree, self._settings())
        c_diverge = score_snapshot(snap_diverge, self._settings())
        self.assertGreater(c_agree.confidence, c_diverge.confidence)


class TestSignalDirection(unittest.TestCase):
    def _settings(self):
        return CryptoSettings()

    def test_long_signal_from_uptrend_and_negative_funding(self):
        snap = MarketSnapshot(
            symbol="BTC_USDT",
            contract=None,
            ticker=_make_ticker("BTC_USDT"),
            candles=_make_candles("BTC_USDT", trend="long"),
            funding=_make_funding("BTC_USDT", rate=-0.001),
            open_interest=_make_oi("BTC_USDT"),
        )
        candidate = score_snapshot(snap, self._settings())
        self.assertEqual(candidate.signal_status, SignalStatus.LONG)

    def test_unknown_signal_without_candles(self):
        snap = MarketSnapshot(
            symbol="BTC_USDT",
            contract=None,
            ticker=_make_ticker("BTC_USDT"),
            candles=[],
            funding=_make_funding("BTC_USDT"),
            open_interest=_make_oi("BTC_USDT"),
        )
        candidate = score_snapshot(snap, self._settings())
        self.assertEqual(candidate.signal_status, SignalStatus.UNKNOWN)

    def test_qualified_signal_gets_coin_specific_plan(self):
        settings = replace(self._settings(), stop_atr_period=3)
        primary = _make_zone_candles("BTC_USDT", trend="long")
        context = _make_zone_candles("BTC_USDT", trend="long")
        for candle in context:
            candle.interval = "Hour4"
        snap = MarketSnapshot(
            symbol="BTC_USDT",
            contract=_make_contract("BTC_USDT"),
            ticker=_make_ticker("BTC_USDT", price=100.3),
            candles=primary,
            context_candles=context,
            funding=_make_funding("BTC_USDT", rate=-0.001),
            open_interest=_make_oi("BTC_USDT"),
        )
        candidate = score_snapshot(snap, settings)

        self.assertEqual(candidate.signal_status, SignalStatus.LONG)
        self.assertEqual(candidate.entry_status, EntryStatus.CONFIRMED)
        self.assertIsNotNone(candidate.planned_quantity)
        self.assertIsNotNone(candidate.planned_stop_price)
        self.assertIsNotNone(candidate.planned_take_profit_price)
        self.assertIsNotNone(candidate.planned_margin_usdt)
        self.assertIsNotNone(candidate.profit_lock_trigger_price)
        self.assertIsNotNone(candidate.profit_lock_stop_price)
        self.assertIsNotNone(candidate.planned_target_profit_usdt)

        gross_target = (
            (candidate.planned_take_profit_price - 100.3)
            * candidate.planned_quantity
            * 0.1
        )
        # Exchange price increments can conservatively round the gross target
        # a few ten-thousandths above the fixed $3 objective.
        self.assertAlmostEqual(gross_target, 3.0, places=4)
        self.assertGreaterEqual(candidate.planned_target_profit_usdt, 3.0)

    def test_setup_stays_pending_when_price_has_run_from_the_entry_zone(self):
        settings = replace(self._settings(), stop_atr_period=3)
        primary = _make_zone_candles("BTC_USDT")
        context = _make_zone_candles("BTC_USDT")
        for candle in context:
            candle.interval = "Hour4"
        candidate = score_snapshot(
            MarketSnapshot(
                symbol="BTC_USDT",
                contract=_make_contract("BTC_USDT"),
                ticker=_make_ticker("BTC_USDT", price=101.5),
                candles=primary,
                context_candles=context,
                funding=_make_funding("BTC_USDT", rate=-0.001),
                open_interest=_make_oi("BTC_USDT"),
            ),
            settings,
        )
        self.assertEqual(candidate.entry_status, EntryStatus.PENDING)
        self.assertIsNone(candidate.planned_quantity)
        self.assertIsNotNone(candidate.entry_expires_at)

    def test_failed_breakout_blocks_an_otherwise_qualified_long_plan(self):
        settings = replace(self._settings(), stop_atr_period=3)
        primary = _make_zone_candles("BTC_USDT")
        primary[-1].high = 103.0
        primary[-1].close = 102.1
        primary[-1].open = 101.4
        context = _make_zone_candles("BTC_USDT")
        for candle in context:
            candle.interval = "Hour4"
        candidate = score_snapshot(
            MarketSnapshot(
                symbol="BTC_USDT",
                contract=_make_contract("BTC_USDT"),
                ticker=_make_ticker("BTC_USDT", price=100.3),
                candles=primary,
                context_candles=context,
                funding=_make_funding("BTC_USDT", rate=-0.001),
                open_interest=_make_oi("BTC_USDT"),
            ),
            settings,
        )
        self.assertEqual(candidate.entry_status, EntryStatus.REJECTED)
        self.assertTrue(candidate.fake_reversal_detected)
        self.assertIsNone(candidate.planned_quantity)


if __name__ == "__main__":
    unittest.main()
