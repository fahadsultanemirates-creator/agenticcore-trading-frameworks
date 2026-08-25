"""
Tests for analysis/scanner.py – filtering and ranking.
"""

import sys
import os
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from config.settings import CryptoSettings
from domain.models import ContractDetail, Ticker
from analysis.scanner import filter_universe, rank_candidates


def _contract(symbol: str, active: bool = True) -> ContractDetail:
    return ContractDetail(
        symbol=symbol,
        display_name=symbol,
        base_coin=symbol.replace("_USDT", ""),
        quote_coin="USDT",
        contract_size=0.001,
        volume_step=1.0,
        min_quantity=1.0,
        max_quantity=100000.0,
        price_precision=2,
        quantity_precision=0,
        is_active=active,
        fetched_at="2024-01-01T00:00:00+00:00",
        contract_type=1,
        concept_plates=["mc-trade-zone-layer1"],
    )


def _ticker(
    symbol: str,
    last: float = 100.0,
    spread: float = 0.05,
    turnover: float = 10_000_000.0,
) -> Ticker:
    return Ticker(
        symbol=symbol,
        last_price=last,
        bid=last * 0.9995,
        ask=last * 1.0005,
        spread_pct=spread,
        volume_24h=100000.0,
        turnover_24h_usdt=turnover,
        change_pct_24h=0.01,
        fetched_at="2024-01-01T00:00:00+00:00",
    )


class TestFilterUniverse(unittest.TestCase):
    def _settings(self, **kwargs) -> CryptoSettings:
        # Build with defaults then test overrides
        import dataclasses
        base = CryptoSettings()
        return dataclasses.replace(base, **kwargs)

    def test_passes_valid_usdt_symbol(self):
        contracts = [_contract("BTC_USDT")]
        tickers = {"BTC_USDT": _ticker("BTC_USDT")}
        settings = self._settings()
        result = filter_universe(contracts, tickers, settings)
        self.assertIn("BTC_USDT", result)

    def test_rejects_non_usdt_symbol(self):
        contracts = [_contract("BTC_USD")]
        tickers = {"BTC_USD": _ticker("BTC_USD")}
        settings = self._settings()
        result = filter_universe(contracts, tickers, settings)
        self.assertNotIn("BTC_USD", result)

    def test_rejects_inactive_contract(self):
        contracts = [_contract("SOL_USDT", active=False)]
        tickers = {"SOL_USDT": _ticker("SOL_USDT")}
        settings = self._settings()
        result = filter_universe(contracts, tickers, settings)
        self.assertNotIn("SOL_USDT", result)

    def test_rejects_missing_ticker(self):
        contracts = [_contract("ETH_USDT")]
        tickers = {}
        settings = self._settings()
        result = filter_universe(contracts, tickers, settings)
        self.assertNotIn("ETH_USDT", result)

    def test_rejects_spread_too_wide(self):
        contracts = [_contract("DOGE_USDT")]
        tickers = {"DOGE_USDT": _ticker("DOGE_USDT", spread=0.5)}  # 0.5% > 0.15% default
        settings = self._settings()
        result = filter_universe(contracts, tickers, settings)
        self.assertNotIn("DOGE_USDT", result)

    def test_rejects_low_turnover(self):
        contracts = [_contract("RARE_USDT")]
        tickers = {"RARE_USDT": _ticker("RARE_USDT", turnover=100_000.0)}  # below 5M default
        settings = self._settings()
        result = filter_universe(contracts, tickers, settings)
        self.assertNotIn("RARE_USDT", result)

    def test_rejects_null_spread(self):
        contracts = [_contract("XRP_USDT")]
        t = _ticker("XRP_USDT")
        t = Ticker(
            symbol=t.symbol, last_price=t.last_price,
            bid=None, ask=None, spread_pct=None,
            volume_24h=t.volume_24h, turnover_24h_usdt=t.turnover_24h_usdt,
            change_pct_24h=t.change_pct_24h, fetched_at=t.fetched_at,
        )
        tickers = {"XRP_USDT": t}
        settings = self._settings()
        result = filter_universe(contracts, tickers, settings)
        self.assertNotIn("XRP_USDT", result)

    def test_rejects_unknown_contract_classification(self):
        import dataclasses
        contract = dataclasses.replace(_contract("UNKNOWN_USDT"), concept_plates=[])
        result = filter_universe(
            [contract],
            {"UNKNOWN_USDT": _ticker("UNKNOWN_USDT")},
            self._settings(),
        )
        self.assertNotIn("UNKNOWN_USDT", result)

    def test_rejects_metal_contract_from_exchange_metadata(self):
        import dataclasses
        contract = dataclasses.replace(
            _contract("XAU_USDT"),
            base_coin="XAU",
            concept_plates=["mc-trade-zone-metals", "mc-trade-zone-tradfi"],
        )
        result = filter_universe(
            [contract],
            {"XAU_USDT": _ticker("XAU_USDT")},
            self._settings(),
        )
        self.assertNotIn("XAU_USDT", result)

    def test_rejects_stock_contract_from_exchange_metadata(self):
        import dataclasses
        contract = dataclasses.replace(
            _contract("MUSTOCK_USDT"),
            base_coin="MUSTOCK",
            contract_type=2,
            concept_plates=["mc-trade-zone-stock", "mc-trade-zone-tradfi"],
        )
        result = filter_universe(
            [contract],
            {"MUSTOCK_USDT": _ticker("MUSTOCK_USDT")},
            self._settings(),
        )
        self.assertNotIn("MUSTOCK_USDT", result)

    def test_respects_scan_limit(self):
        contracts = [_contract(f"COIN{i}_USDT") for i in range(10)]
        tickers = {f"COIN{i}_USDT": _ticker(f"COIN{i}_USDT") for i in range(10)}
        import dataclasses
        settings = dataclasses.replace(CryptoSettings(), scan_limit=3)
        result = filter_universe(contracts, tickers, settings)
        self.assertLessEqual(len(result), 3)


class TestRankCandidates(unittest.TestCase):
    def test_ranks_by_turnover_descending(self):
        tickers = {
            "A_USDT": _ticker("A_USDT", turnover=1_000_000.0),
            "B_USDT": _ticker("B_USDT", turnover=50_000_000.0),
            "C_USDT": _ticker("C_USDT", turnover=10_000_000.0),
        }
        ranked = rank_candidates(list(tickers.keys()), tickers, limit=3)
        self.assertEqual(ranked[0], "B_USDT")
        self.assertEqual(ranked[1], "C_USDT")

    def test_respects_limit(self):
        tickers = {f"X{i}_USDT": _ticker(f"X{i}_USDT", turnover=float(i * 1e6)) for i in range(10)}
        ranked = rank_candidates(list(tickers.keys()), tickers, limit=3)
        self.assertEqual(len(ranked), 3)

    def test_sorts_null_turnover_last(self):
        t_no_turnover = Ticker(
            symbol="NONE_USDT", last_price=1.0, bid=None, ask=None,
            spread_pct=None, volume_24h=None, turnover_24h_usdt=None,
            change_pct_24h=None, fetched_at=None,
        )
        tickers = {
            "NONE_USDT": t_no_turnover,
            "GOOD_USDT": _ticker("GOOD_USDT", turnover=10_000_000.0),
        }
        ranked = rank_candidates(list(tickers.keys()), tickers, limit=2)
        self.assertEqual(ranked[0], "GOOD_USDT")


if __name__ == "__main__":
    unittest.main()
