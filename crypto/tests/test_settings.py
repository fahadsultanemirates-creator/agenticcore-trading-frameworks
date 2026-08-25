"""Tests that the agreed Tier 1 risk profile cannot be loosened by env/config."""

import os
import sys
import unittest
from dataclasses import replace

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from config.settings import CryptoSettings


class TestTierOneSettings(unittest.TestCase):
    def test_default_profile_is_valid(self):
        CryptoSettings().validate()

    def test_position_and_target_overrides_fail_closed(self):
        with self.assertRaises(ValueError):
            replace(CryptoSettings(), position_notional_usdt=50.01).validate()
        with self.assertRaises(ValueError):
            replace(CryptoSettings(), max_isolated_margin_per_position_usdt=2.51).validate()
        with self.assertRaises(ValueError):
            replace(CryptoSettings(), take_profit_usdt=3.01).validate()
        with self.assertRaises(ValueError):
            replace(CryptoSettings(), basket_profit_target_usdt=5.01).validate()
        with self.assertRaises(ValueError):
            replace(CryptoSettings(), telegram_daily_summary_time="23:45").validate()

    def test_leverage_stop_and_lock_overrides_fail_closed(self):
        with self.assertRaises(ValueError):
            replace(CryptoSettings(), leverage_max=19).validate()
        with self.assertRaises(ValueError):
            replace(CryptoSettings(), maximum_stop_pct=0.03).validate()
        with self.assertRaises(ValueError):
            replace(CryptoSettings(), profit_lock_protection_pct=34).validate()


if __name__ == "__main__":
    unittest.main()