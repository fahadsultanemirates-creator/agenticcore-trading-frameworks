"""
Tests for risk/sizing.py – quantity calculation, stale/unknown rejection.
Never uses Forex lots.
"""

import sys
import os
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from domain.models import ContractDetail
from risk.sizing import SizingError, calculate_quantity, calculate_quantity_for_notional


def _contract(
    symbol: str = "BTC_USDT",
    contract_size: float = 0.001,
    volume_step: float = 1.0,
    min_qty: float = 1.0,
    max_qty: float = 100000.0,
) -> ContractDetail:
    return ContractDetail(
        symbol=symbol,
        display_name=symbol,
        base_coin="BTC",
        quote_coin="USDT",
        contract_size=contract_size,
        volume_step=volume_step,
        min_quantity=min_qty,
        max_quantity=max_qty,
        price_precision=2,
        quantity_precision=0,
        is_active=True,
        fetched_at="2024-01-01T00:00:00+00:00",
    )


class TestCalculateQuantity(unittest.TestCase):
    def test_basic_calculation(self):
        """
        entry=67000, stop=66500, risk=2 USDT, contract_size=0.001
        stop_distance=500, risk_per_contract=500*0.001=0.5 USDT
        raw_qty = 2/0.5 = 4 contracts
        """
        qty = calculate_quantity(
            entry_price=67000.0,
            stop_price=66500.0,
            risk_usdt=2.0,
            contract=_contract(),
            leverage=20,
            margin_mode="isolated",
        )
        self.assertAlmostEqual(qty, 4.0)

    def test_floors_to_volume_step(self):
        """Should floor, not round, to volume_step."""
        # stop_distance=100, risk_per_contract=100*0.001=0.1, raw_qty=20/0.1=200 contracts
        # With volume_step=1, qty=200 (exact)
        qty = calculate_quantity(
            entry_price=1000.0,
            stop_price=900.0,
            risk_usdt=20.0,
            contract=_contract(contract_size=0.001, volume_step=1.0),
            leverage=15,
        )
        self.assertAlmostEqual(qty, 200.0)

    def test_raises_on_non_isolated_margin(self):
        with self.assertRaises(SizingError) as ctx:
            calculate_quantity(
                entry_price=100.0, stop_price=95.0, risk_usdt=2.0,
                contract=_contract(), leverage=10, margin_mode="cross",
            )
        self.assertIn("isolated", str(ctx.exception))

    def test_raises_on_leverage_exceeding_ceiling(self):
        with self.assertRaises(SizingError):
            calculate_quantity(
                entry_price=100.0, stop_price=95.0, risk_usdt=2.0,
                contract=_contract(), leverage=25,
            )

    def test_raises_on_zero_entry_price(self):
        with self.assertRaises(SizingError):
            calculate_quantity(
                entry_price=0.0, stop_price=95.0, risk_usdt=2.0,
                contract=_contract(), leverage=15,
            )

    def test_raises_on_equal_entry_stop(self):
        with self.assertRaises(SizingError):
            calculate_quantity(
                entry_price=100.0, stop_price=100.0, risk_usdt=2.0,
                contract=_contract(), leverage=15,
            )

    def test_raises_on_missing_contract_size(self):
        c = _contract()
        c = ContractDetail(
            symbol=c.symbol, display_name=c.display_name, base_coin=c.base_coin,
            quote_coin=c.quote_coin, contract_size=None,  # missing
            volume_step=c.volume_step, min_quantity=c.min_quantity,
            max_quantity=c.max_quantity, price_precision=c.price_precision,
            quantity_precision=c.quantity_precision, is_active=c.is_active,
            fetched_at=c.fetched_at,
        )
        with self.assertRaises(SizingError) as ctx:
            calculate_quantity(
                entry_price=100.0, stop_price=95.0, risk_usdt=2.0,
                contract=c, leverage=15,
            )
        self.assertIn("contract_size", str(ctx.exception))

    def test_raises_when_below_min_quantity(self):
        """Tiny risk_usdt with wide stop leads to qty < min_quantity → SizingError."""
        # stop_distance=1000, risk_per_contract=1000*0.001=1.0 USDT, raw_qty=0.001/1.0=0.001
        # 0.001 < min_qty=1.0 → SizingError
        with self.assertRaises(SizingError) as ctx:
            calculate_quantity(
                entry_price=67000.0,
                stop_price=66000.0,  # 1000 point stop distance
                risk_usdt=0.001,     # tiny risk → qty=0.001/1.0=0.001 < min=1
                contract=_contract(min_qty=1.0),
                leverage=15,
            )
        self.assertIn("min_quantity", str(ctx.exception))

    def test_caps_at_max_quantity(self):
        """Very large risk should be capped at max_quantity."""
        qty = calculate_quantity(
            entry_price=100.0,
            stop_price=99.0,
            risk_usdt=1_000_000.0,
            contract=_contract(contract_size=0.001, max_qty=500.0),
            leverage=20,
        )
        self.assertEqual(qty, 500.0)

    def test_negative_leverage_rejected(self):
        with self.assertRaises(SizingError):
            calculate_quantity(
                entry_price=100.0, stop_price=95.0, risk_usdt=2.0,
                contract=_contract(), leverage=-1,
            )


class TestCalculateQuantityForNotional(unittest.TestCase):
    def test_floors_to_a_fixed_fifty_usdt_position_value(self):
        contract = _contract(
            symbol="XRP_USDT",
            contract_size=0.1,
            volume_step=1.0,
        )
        quantity = calculate_quantity_for_notional(
            entry_price=1.491,
            notional_usdt=50.0,
            contract=contract,
            leverage=20,
        )
        self.assertEqual(quantity, 335.0)
        self.assertLessEqual(1.491 * quantity * contract.contract_size, 50.0)
        self.assertAlmostEqual(
            (1.491 * quantity * contract.contract_size) / 20,
            2.497425,
        )

    def test_rejects_cross_margin(self):
        with self.assertRaises(SizingError):
            calculate_quantity_for_notional(
                entry_price=1.0,
                notional_usdt=50.0,
                contract=_contract(contract_size=1.0),
                leverage=20,
                margin_mode="cross",
            )

    def test_rejects_when_fifty_usdt_is_below_contract_minimum(self):
        with self.assertRaises(SizingError):
            calculate_quantity_for_notional(
                entry_price=1.0,
                notional_usdt=50.0,
                contract=_contract(contract_size=1.0, min_qty=100.0),
                leverage=20,
            )


if __name__ == "__main__":
    unittest.main()
