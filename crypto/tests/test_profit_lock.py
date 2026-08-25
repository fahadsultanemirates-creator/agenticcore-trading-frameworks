"""Tests for 65%-progress / 35%-protected-profit levels."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from risk.profit_lock import calculate_profit_lock_levels, should_activate_profit_lock


class TestProfitLock(unittest.TestCase):
    def test_long_lock_protects_35_percent_of_target_distance(self):
        levels = calculate_profit_lock_levels("long", 100.0, 103.0)
        self.assertAlmostEqual(levels.activation_price, 101.95)
        self.assertAlmostEqual(levels.protected_stop_price, 101.05)
        self.assertFalse(should_activate_profit_lock("long", 101.94, levels))
        self.assertTrue(should_activate_profit_lock("long", 101.95, levels))

    def test_short_lock_uses_the_same_rule_in_reverse(self):
        levels = calculate_profit_lock_levels("short", 100.0, 97.0)
        self.assertAlmostEqual(levels.activation_price, 98.05)
        self.assertAlmostEqual(levels.protected_stop_price, 98.95)
        self.assertFalse(should_activate_profit_lock("short", 98.06, levels))
        self.assertTrue(should_activate_profit_lock("short", 98.05, levels))


if __name__ == "__main__":
    unittest.main()