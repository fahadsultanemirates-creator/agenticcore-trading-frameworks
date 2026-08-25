import unittest
from types import SimpleNamespace

from mt5_bridge.bridge import _aggregate_position_exit_deals, _verify_connected_account


class MT5IdentityTests(unittest.TestCase):
    def test_matching_terminal_identity_is_accepted(self):
        _verify_connected_account(
            SimpleNamespace(login=12345, server="Broker-Demo"), 12345, "Broker-Demo"
        )

    def test_mismatched_terminal_identity_is_rejected(self):
        with self.assertRaises(ConnectionError):
            _verify_connected_account(
                SimpleNamespace(login=99999, server="Other-Broker"), 12345, "Broker-Demo"
            )

    def test_partial_and_final_exit_deals_are_fully_aggregated(self):
        partial = SimpleNamespace(
            ticket=1001, position_id=77, entry=1, symbol="EURUSD",
            profit=2.0, swap=-0.1, commission=-0.2, fee=0.0, time=10, reason=4,
        )
        final = SimpleNamespace(
            ticket=1002, position_id=77, entry=1, symbol="EURUSD",
            profit=3.5, swap=-0.2, commission=-0.3, fee=-0.1, time=20, reason=4,
        )
        other = SimpleNamespace(
            ticket=1003, position_id=88, entry=1, symbol="GBPUSD",
            profit=99.0, swap=0.0, commission=0.0, fee=0.0, time=30, reason=4,
        )

        result = _aggregate_position_exit_deals([partial, final, other], 77, {1})
        self.assertEqual(result["deal_ids"], [1001, 1002])
        self.assertAlmostEqual(result["profit"], 4.6)


if __name__ == "__main__":
    unittest.main()