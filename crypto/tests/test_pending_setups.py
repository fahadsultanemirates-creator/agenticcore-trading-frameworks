"""Regression tests for durable pending-entry expiry across market scan cycles."""

import os
import sys
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from config.settings import CryptoSettings
from domain.models import Candidate, EntryStatus
from runtime.pending_setups import apply_pending_setup_expiry


def pending_candidate(status: str = EntryStatus.PENDING) -> Candidate:
    return Candidate(
        rank=1,
        symbol="BTC_USDT",
        planned_side="long",
        planned_quantity=1.0,
        planned_margin_usdt=1.0,
        planned_stop_price=99.0,
        planned_take_profit_price=103.0,
        entry_status=status,
        entry_zone_low=100.0,
        entry_zone_high=101.0,
        entry_invalidation_price=99.5,
        entry_structure_id="Min15:support:1000|2000|3000",
    )


class TestPendingSetupExpiry(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.temp_dir.name, "pending_setups.json")
        self.settings = replace(CryptoSettings(), entry_setup_expiry_candles=2)
        self.started = datetime(2026, 8, 22, 12, 0, tzinfo=timezone.utc)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_pending_setup_keeps_original_expiry_and_cannot_revert_to_pending(self):
        first = pending_candidate()
        apply_pending_setup_expiry(self.path, [first], self.settings, now=self.started)
        self.assertEqual(first.entry_status, EntryStatus.PENDING)
        expiry = first.entry_expires_at
        self.assertIsNotNone(expiry)

        later = pending_candidate()
        apply_pending_setup_expiry(
            self.path,
            [later],
            self.settings,
            now=self.started + timedelta(minutes=31),
        )
        self.assertEqual(later.entry_status, EntryStatus.EXPIRED)
        self.assertEqual(later.entry_expires_at, expiry)
        self.assertIsNone(later.planned_quantity)

        still_later = pending_candidate()
        apply_pending_setup_expiry(
            self.path,
            [still_later],
            self.settings,
            now=self.started + timedelta(minutes=45),
        )
        self.assertEqual(still_later.entry_status, EntryStatus.EXPIRED)
        self.assertIsNone(still_later.planned_quantity)

    def test_fresh_confirmed_retest_clears_expired_observation_and_keeps_plan(self):
        apply_pending_setup_expiry(self.path, [pending_candidate()], self.settings, now=self.started)
        confirmed = pending_candidate(EntryStatus.CONFIRMED)
        apply_pending_setup_expiry(
            self.path,
            [confirmed],
            self.settings,
            now=self.started + timedelta(minutes=31),
        )
        self.assertEqual(confirmed.entry_status, EntryStatus.CONFIRMED)
        self.assertIsNotNone(confirmed.planned_quantity)

    def test_expired_structure_survives_scan_gaps_and_zone_width_drift(self):
        apply_pending_setup_expiry(self.path, [pending_candidate()], self.settings, now=self.started)

        # The same completed swing timestamps remain the identity even though
        # the adaptive zone boundaries have widened in a later scan.
        drifted = pending_candidate()
        drifted.entry_zone_low = 99.7
        drifted.entry_zone_high = 101.3
        apply_pending_setup_expiry(
            self.path,
            [drifted],
            self.settings,
            now=self.started + timedelta(minutes=31),
        )
        self.assertEqual(drifted.entry_status, EntryStatus.EXPIRED)

        # A universe/ranking gap must not erase the expired observation.
        apply_pending_setup_expiry(
            self.path,
            [],
            self.settings,
            now=self.started + timedelta(days=3),
        )
        reappeared = pending_candidate()
        apply_pending_setup_expiry(
            self.path,
            [reappeared],
            self.settings,
            now=self.started + timedelta(days=3, minutes=1),
        )
        self.assertEqual(reappeared.entry_status, EntryStatus.EXPIRED)


if __name__ == "__main__":
    unittest.main()