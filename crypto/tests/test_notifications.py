"""Regression coverage for outbound-only Crypto Tier 1 Telegram reporting."""

import os
import sys
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from config.settings import CryptoSettings
from domain.models import Candidate, DailyGuardStatus, FrameworkMode, FrameworkState
from runtime.notifications import DUBAI, emit_cycle_notifications, notify_cycle
from runtime.orchestrator import run_cycle
from runtime.paper_positions import daily_realized_pnl
from storage.state import read_json_safe, write_json_atomic


def paper_position(**overrides):
    position = {
        "id": "paper-1",
        "symbol": "BTC_USDT",
        "side": "long",
        "status": "closed",
        "entry_price": 100.0,
        "exit_price": 101.05,
        "quantity": 1.0,
        "contract_size": 1.0,
        "stop_price": 101.05,
        "take_profit_price": 103.0,
        "gross_pnl_usdt": 1.05,
        "fees_usdt": 0.08,
        "net_pnl_usdt": 0.97,
        "close_reason": "profit_lock",
        "confidence": 87,
    }
    position.update(overrides)
    return position


class CryptoNotificationTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.settings = replace(
            CryptoSettings(),
            runtime_dir=self.temp_dir.name,
            telegram_bot_token="test-token",
            telegram_chat_id="test-chat",
            ai_explanations_enabled=False,
        )
        self.sent = []

    def tearDown(self):
        self.temp_dir.cleanup()

    def sender(self, _settings, message):
        self.sent.append(message)
        return True

    def state(self, **overrides):
        state = FrameworkState(
            mode=FrameworkMode.PAPER,
            daily_guard_status=DailyGuardStatus.ACTIVE,
            daily_pnl_usdt=0.97,
            paper_summary={"realized_pnl_today_usdt": 0.97, "open_count": 0},
        )
        for key, value in overrides.items():
            setattr(state, key, value)
        return state

    def test_close_message_includes_full_net_result_and_daily_total(self):
        event = {"event": "paper_position_closed", "status": "closed", "position": paper_position()}
        emitted = emit_cycle_notifications(self.settings, self.state(), [event], sender=self.sender)

        self.assertEqual(emitted, 1)
        message = self.sent[0]
        self.assertIn("Paper Trade Closed", message)
        self.assertIn("PROFIT_LOCK", message)
        self.assertIn("Gross: +1.05 USDT", message)
        self.assertIn("Fees: -0.08 USDT", message)
        self.assertIn("Net result: +0.97 USDT", message)
        self.assertIn("Today realised: +0.97 USDT", message)

    def test_paper_events_are_sent_once_even_when_cycle_repeats(self):
        event = {
            "event": "paper_profit_lock",
            "status": "applied",
            "position": paper_position(status="open", exit_price=None, net_pnl_usdt=None),
        }
        first = emit_cycle_notifications(self.settings, self.state(), [event], sender=self.sender)
        second = emit_cycle_notifications(self.settings, self.state(), [event], sender=self.sender)

        self.assertEqual(first, 1)
        self.assertEqual(second, 0)
        self.assertEqual(len(self.sent), 1)
        self.assertIn("65% progress", self.sent[0])

    def test_opening_and_losing_close_messages_include_required_values(self):
        opening = paper_position(
            id="paper-open",
            status="open",
            exit_price=None,
            gross_pnl_usdt=None,
            net_pnl_usdt=None,
        )
        losing = paper_position(
            id="paper-loss",
            exit_price=98.0,
            gross_pnl_usdt=-2.0,
            fees_usdt=0.08,
            net_pnl_usdt=-2.08,
            close_reason="stop",
        )
        events = [
            {"event": "paper_position_opened", "status": "opened", "position": opening},
            {"event": "paper_position_closed", "status": "closed", "position": losing},
        ]

        self.assertEqual(emit_cycle_notifications(self.settings, self.state(), events, sender=self.sender), 2)
        self.assertIn("Paper Trade Opened", self.sent[0])
        self.assertIn("Entry: 100.0000", self.sent[0])
        self.assertIn("Confidence: 87%", self.sent[0])
        self.assertIn("Net result: -2.08 USDT", self.sent[1])
        self.assertIn("Reason: STOP", self.sent[1])

    def test_signal_mode_reports_qualified_signal_once_per_day(self):
        candidate = Candidate(
            rank=1,
            symbol="ETH_USDT",
            planned_side="short",
            planned_quantity=2.0,
            last_price=2000.0,
            planned_stop_price=2020.0,
            planned_take_profit_price=1970.0,
            confidence=82,
        )
        state = self.state(mode=FrameworkMode.SIGNAL, candidates=[candidate])

        self.assertEqual(emit_cycle_notifications(self.settings, state, [], sender=self.sender), 1)
        self.assertEqual(emit_cycle_notifications(self.settings, state, [], sender=self.sender), 0)
        self.assertIn("Qualified Crypto Signal", self.sent[0])
        self.assertIn("SHORT ETH_USDT", self.sent[0])

    def test_paper_mode_also_reports_qualified_signal_once_per_day(self):
        candidate = Candidate(
            rank=1,
            symbol="SOL_USDT",
            planned_side="long",
            planned_quantity=1.0,
            last_price=150.0,
            planned_stop_price=145.0,
            planned_take_profit_price=157.0,
            confidence=80,
        )
        state = self.state(candidates=[candidate])

        self.assertEqual(emit_cycle_notifications(self.settings, state, [], sender=self.sender), 1)
        self.assertEqual(emit_cycle_notifications(self.settings, state, [], sender=self.sender), 0)
        self.assertIn("Qualified Crypto Signal", self.sent[0])
        self.assertIn("LONG SOL_USDT", self.sent[0])

    def test_guard_change_reports_locked_paper_entries(self):
        emit_cycle_notifications(self.settings, self.state(), [], sender=self.sender)
        halted = self.state(
            daily_guard_status=DailyGuardStatus.LOSS_LIMIT_REACHED,
            daily_pnl_usdt=-20.0,
        )

        self.assertEqual(emit_cycle_notifications(self.settings, halted, [], sender=self.sender), 1)
        self.assertIn("Entries Locked", self.sent[0])
        self.assertIn("loss_limit_reached", self.sent[0])

    def test_daily_report_counts_closed_local_paper_trades(self):
        today = datetime(2026, 8, 20, 23, 59, tzinfo=DUBAI)
        position = paper_position(closed_at=today.isoformat())
        write_json_atomic(
            os.path.join(self.temp_dir.name, "paper_positions.json"),
            {"positions": [position]},
        )

        emitted = emit_cycle_notifications(
            self.settings, self.state(), [], now=today, sender=self.sender
        )

        self.assertEqual(emitted, 1)
        self.assertIn("Daily (through 20 Aug 2026 23:59)", self.sent[0])
        self.assertIn("Net P&L: +0.97 USDT", self.sent[0])
        self.assertIn(
            "Gross +1.05 USDT | Fees -0.08 USDT | Net +0.97 USDT",
            self.sent[0],
        )

    def test_disabled_telegram_never_calls_sender(self):
        disabled = replace(self.settings, telegram_bot_token="", telegram_chat_id="")
        event = {"event": "paper_position_closed", "status": "closed", "position": paper_position()}

        self.assertEqual(emit_cycle_notifications(disabled, self.state(), [event], sender=self.sender), 0)
        self.assertEqual(self.sent, [])

    def test_guard_transition_retries_after_a_delivery_failure(self):
        emit_cycle_notifications(self.settings, self.state(), [], sender=self.sender)
        halted = self.state(daily_guard_status=DailyGuardStatus.LOSS_LIMIT_REACHED)
        failed_sender = lambda _settings, _message: False

        self.assertEqual(emit_cycle_notifications(self.settings, halted, [], sender=failed_sender), 0)
        self.assertEqual(emit_cycle_notifications(self.settings, halted, [], sender=self.sender), 1)
        self.assertIn("Entries Locked", self.sent[0])

    def test_paper_close_retries_from_persisted_outbox_on_the_next_cycle(self):
        event = {"event": "paper_position_closed", "status": "closed", "position": paper_position()}
        failed_sender = lambda _settings, _message: False

        self.assertEqual(emit_cycle_notifications(self.settings, self.state(), [event], sender=failed_sender), 0)
        self.assertEqual(emit_cycle_notifications(self.settings, self.state(), [], sender=self.sender), 1)
        self.assertEqual(len(self.sent), 1)
        self.assertIn("Paper Trade Closed", self.sent[0])

    def test_initial_halted_guard_sends_entry_lock_alert(self):
        halted = self.state(daily_guard_status=DailyGuardStatus.PROFIT_TARGET_REACHED)

        self.assertEqual(emit_cycle_notifications(self.settings, halted, [], sender=self.sender), 1)
        self.assertIn("Entries Locked", self.sent[0])
        self.assertIn("profit_target_reached", self.sent[0])

    def test_early_halted_cycle_still_emits_the_guard_alert(self):
        with patch("runtime.notifications.send_message", return_value=True) as sender:
            state = run_cycle(
                self.settings,
                daily_pnl_usdt=-self.settings.daily_loss_limit_usdt,
            )

        self.assertEqual(state.daily_guard_status, DailyGuardStatus.LOSS_LIMIT_REACHED)
        self.assertIn("Daily guard halted", state.last_error)
        self.assertTrue(
            any("Entries Locked" in call.args[1] for call in sender.call_args_list)
        )

    def test_daily_pnl_uses_the_same_dubai_business_day_as_reporting(self):
        closed_at = "2026-08-20T20:30:00+00:00"  # 00:30 on 21 Aug in Dubai.
        position = paper_position(closed_at=closed_at)
        dubai_morning = datetime(2026, 8, 21, 8, 0, tzinfo=DUBAI)

        self.assertEqual(daily_realized_pnl([position], now=dubai_morning), 0.97)

    def test_heartbeat_and_error_messages_use_safe_operator_text(self):
        state = self.state(cycle_count=30)
        with patch("runtime.notifications.send_message", return_value=True) as sender:
            self.assertTrue(notify_cycle(self.settings, state))
            self.assertTrue(notify_cycle(self.settings, None, error="market source unavailable"))

        self.assertEqual(sender.call_count, 2)
        self.assertIn("heartbeat", sender.call_args_list[0].args[1])
        self.assertIn("cycle alert", sender.call_args_list[1].args[1])

    def test_weekly_summary_runs_on_sunday_and_excludes_the_next_monday(self):
        sunday = datetime(2026, 8, 23, 23, 30, tzinfo=DUBAI)
        sunday_trade = paper_position(
            id="sunday",
            closed_at=datetime(2026, 8, 23, 20, 0, tzinfo=DUBAI).isoformat(),
        )
        monday_trade = paper_position(
            id="monday",
            closed_at=datetime(2026, 8, 24, 10, 0, tzinfo=DUBAI).isoformat(),
            gross_pnl_usdt=5.0,
            fees_usdt=0.0,
            net_pnl_usdt=5.0,
        )
        write_json_atomic(
            os.path.join(self.temp_dir.name, "paper_positions.json"),
            {"positions": [sunday_trade, monday_trade]},
        )
        write_json_atomic(
            os.path.join(self.temp_dir.name, "notification_state.json"),
            {"daily_report_date": sunday.date().isoformat()},
        )

        self.assertEqual(
            emit_cycle_notifications(
                self.settings, self.state(), [], now=sunday, sender=self.sender
            ),
            1,
        )
        self.assertIn("Weekly (16 Aug 23:30–23 Aug 23:30)", self.sent[0])
        self.assertIn("Net P&L: +0.97 USDT", self.sent[0])

    def test_monthly_summary_runs_at_last_day_time_with_trade_details(self):
        month_end = datetime(2026, 8, 31, 23, 45, tzinfo=DUBAI)
        position = paper_position(
            id="month-end",
            closed_at=datetime(2026, 8, 31, 19, 0, tzinfo=DUBAI).isoformat(),
        )
        open_position = paper_position(
            id="open-month-end",
            status="open",
            exit_price=None,
            net_pnl_usdt=None,
            last_mark_price=101.2,
            marked_net_pnl_usdt=1.1,
        )
        write_json_atomic(
            os.path.join(self.temp_dir.name, "paper_positions.json"),
            {"positions": [position, open_position]},
        )

        self.assertEqual(
            emit_cycle_notifications(
                self.settings, self.state(), [], now=month_end, sender=self.sender
            ),
            1,
        )
        message = self.sent[0]
        self.assertIn("Monthly (31 Jul 23:45–31 Aug 23:45)", message)
        self.assertIn("BTC_USDT LONG | WIN | PROFIT_LOCK", message)
        self.assertIn("Open exposure: 1 paper position(s)", message)
        self.assertIn("Open paper positions:", message)

    def test_long_report_retries_only_undelivered_chunks(self):
        now = datetime(2026, 8, 31, 23, 45, tzinfo=DUBAI)
        positions = [
            paper_position(
                id=f"trade-{index}",
                closed_at=now.isoformat(),
                net_pnl_usdt=0.97,
                close_reason="profit_lock",
            )
            for index in range(160)
        ]
        write_json_atomic(
            os.path.join(self.temp_dir.name, "paper_positions.json"),
            {"positions": positions},
        )
        sent_chunks = []

        def fail_after_first(_settings, message):
            sent_chunks.append(message)
            return len(sent_chunks) == 1

        self.assertEqual(
            emit_cycle_notifications(
                self.settings, self.state(), [], now=now, sender=fail_after_first
            ),
            1,
        )
        first_chunk = sent_chunks[0]
        self.assertGreater(len(first_chunk), 1000)
        self.assertGreater(
            emit_cycle_notifications(
                self.settings, self.state(), [], now=now, sender=self.sender
            ),
            0,
        )
        self.assertNotEqual(first_chunk, self.sent[0])

    def test_long_report_stops_at_failed_middle_chunk(self):
        now = datetime(2026, 8, 31, 23, 45, tzinfo=DUBAI)
        positions = [
            paper_position(
                id=f"trade-{index}",
                closed_at=now.isoformat(),
                net_pnl_usdt=0.97,
            )
            for index in range(240)
        ]
        write_json_atomic(
            os.path.join(self.temp_dir.name, "paper_positions.json"),
            {"positions": positions},
        )
        attempts = []

        def fail_middle(_settings, message):
            attempts.append(message)
            return len(attempts) != 2

        self.assertEqual(
            emit_cycle_notifications(
                self.settings, self.state(), [], now=now, sender=fail_middle
            ),
            1,
        )
        self.assertEqual(len(attempts), 2)
        delivered = emit_cycle_notifications(
            self.settings, self.state(), [], now=now, sender=self.sender
        )
        self.assertGreaterEqual(delivered, 2)
        self.assertEqual(self.sent[0], attempts[1])
        self.assertNotIn(attempts[0], self.sent)

    def test_report_part_ack_survives_global_dedup_eviction(self):
        report_text = "R" * 7800
        write_json_atomic(
            os.path.join(self.temp_dir.name, "notification_state.json"),
            {
                "sent_keys": [f"unrelated:{index}" for index in range(1001)],
                "report_outbox": [
                    {
                        "id": "daily-report:2026-08-20",
                        "persistence_key": "daily_report_date",
                        "identifier": "2026-08-20",
                        "text": report_text,
                        "delivered_parts": [1],
                    }
                ],
            },
        )
        self.assertEqual(
            emit_cycle_notifications(
                self.settings,
                self.state(),
                [],
                now=datetime(2026, 8, 21, 0, 1, tzinfo=DUBAI),
                sender=self.sender,
            ),
            1,
        )
        self.assertEqual(self.sent, [report_text[3900:7800]])

    def test_daily_report_retries_after_midnight(self):
        due = datetime(2026, 8, 20, 23, 59, tzinfo=DUBAI)
        position = paper_position(closed_at=due.isoformat())
        write_json_atomic(
            os.path.join(self.temp_dir.name, "paper_positions.json"),
            {"positions": [position]},
        )
        failed_sender = lambda _settings, _message: False

        self.assertEqual(
            emit_cycle_notifications(
                self.settings, self.state(), [], now=due, sender=failed_sender
            ),
            0,
        )
        self.assertEqual(
            emit_cycle_notifications(
                self.settings,
                self.state(),
                [],
                now=datetime(2026, 8, 21, 0, 1, tzinfo=DUBAI),
                sender=self.sender,
            ),
            1,
        )
        self.assertIn("Daily (through 20 Aug 2026 23:59)", self.sent[0])

    def test_report_manifest_is_persisted_before_first_send(self):
        due = datetime(2026, 8, 20, 23, 59, tzinfo=DUBAI)
        observed_manifest = []

        def inspect_then_fail(_settings, _message):
            persisted = read_json_safe(
                os.path.join(self.temp_dir.name, "notification_state.json")
            )
            observed_manifest.extend(persisted.get("report_outbox") or [])
            return False

        self.assertEqual(
            emit_cycle_notifications(
                self.settings, self.state(), [], now=due, sender=inspect_then_fail
            ),
            0,
        )
        self.assertEqual(len(observed_manifest), 1)
        self.assertEqual(observed_manifest[0]["id"], "daily-report:2026-08-20")

    def test_weekly_report_retries_after_sunday_boundary(self):
        due = datetime(2026, 8, 23, 23, 30, tzinfo=DUBAI)
        position = paper_position(closed_at=due.isoformat())
        write_json_atomic(
            os.path.join(self.temp_dir.name, "paper_positions.json"),
            {"positions": [position]},
        )
        failed_sender = lambda _settings, _message: False

        self.assertEqual(
            emit_cycle_notifications(
                self.settings, self.state(), [], now=due, sender=failed_sender
            ),
            0,
        )
        self.assertEqual(
            emit_cycle_notifications(
                self.settings,
                self.state(),
                [],
                now=datetime(2026, 8, 24, 0, 1, tzinfo=DUBAI),
                sender=self.sender,
            ),
            1,
        )
        self.assertIn("Weekly (16 Aug 23:30–23 Aug 23:30)", self.sent[0])

    def test_monthly_report_retries_after_month_boundary(self):
        due = datetime(2026, 8, 31, 23, 45, tzinfo=DUBAI)
        position = paper_position(closed_at=due.isoformat())
        write_json_atomic(
            os.path.join(self.temp_dir.name, "paper_positions.json"),
            {"positions": [position]},
        )
        failed_sender = lambda _settings, _message: False

        self.assertEqual(
            emit_cycle_notifications(
                self.settings, self.state(), [], now=due, sender=failed_sender
            ),
            0,
        )
        self.assertEqual(
            emit_cycle_notifications(
                self.settings,
                self.state(),
                [],
                now=datetime(2026, 9, 1, 0, 1, tzinfo=DUBAI),
                sender=self.sender,
            ),
            1,
        )
        self.assertIn("Monthly (31 Jul 23:45–31 Aug 23:45)", self.sent[0])

    def test_late_sunday_close_is_included_in_the_next_weekly_window(self):
        current_cutoff = datetime(2026, 8, 23, 23, 30, tzinfo=DUBAI)
        late_close = paper_position(
            id="late-sunday",
            closed_at=datetime(2026, 8, 23, 23, 45, tzinfo=DUBAI).isoformat(),
        )
        write_json_atomic(
            os.path.join(self.temp_dir.name, "paper_positions.json"),
            {"positions": [late_close]},
        )

        emit_cycle_notifications(
            self.settings, self.state(), [], now=current_cutoff, sender=self.sender
        )
        self.assertNotIn("BTC_USDT LONG | WIN | PROFIT_LOCK", "\n".join(self.sent))
        self.sent.clear()

        next_cutoff = datetime(2026, 8, 30, 23, 30, tzinfo=DUBAI)
        self.assertEqual(
            emit_cycle_notifications(
                self.settings, self.state(), [], now=next_cutoff, sender=self.sender
            ),
            1,
        )
        self.assertIn("BTC_USDT LONG | WIN | PROFIT_LOCK", self.sent[0])
        self.assertIn("Weekly (23 Aug 23:30–30 Aug 23:30)", self.sent[0])

    def test_late_daily_close_is_included_in_the_next_daily_window(self):
        current_cutoff = datetime(2026, 8, 20, 23, 59, tzinfo=DUBAI)
        late_close = paper_position(
            id="late-daily",
            closed_at=datetime(2026, 8, 20, 23, 59, 30, tzinfo=DUBAI).isoformat(),
        )
        write_json_atomic(
            os.path.join(self.temp_dir.name, "paper_positions.json"),
            {"positions": [late_close]},
        )

        emit_cycle_notifications(
            self.settings, self.state(), [], now=current_cutoff, sender=self.sender
        )
        self.assertNotIn("BTC_USDT LONG | WIN | PROFIT_LOCK", "\n".join(self.sent))
        self.sent.clear()

        next_cutoff = datetime(2026, 8, 21, 23, 59, tzinfo=DUBAI)
        self.assertEqual(
            emit_cycle_notifications(
                self.settings, self.state(), [], now=next_cutoff, sender=self.sender
            ),
            1,
        )
        self.assertIn("BTC_USDT LONG | WIN | PROFIT_LOCK", self.sent[0])
        self.assertIn("Daily (through 21 Aug 2026 23:59)", self.sent[0])

    def test_late_month_end_close_is_included_in_the_next_monthly_window(self):
        current_cutoff = datetime(2026, 8, 31, 23, 45, tzinfo=DUBAI)
        late_close = paper_position(
            id="late-month-end",
            closed_at=datetime(2026, 8, 31, 23, 55, tzinfo=DUBAI).isoformat(),
        )
        write_json_atomic(
            os.path.join(self.temp_dir.name, "paper_positions.json"),
            {"positions": [late_close]},
        )

        emit_cycle_notifications(
            self.settings, self.state(), [], now=current_cutoff, sender=self.sender
        )
        self.assertNotIn("BTC_USDT LONG | WIN | PROFIT_LOCK", "\n".join(self.sent))
        self.sent.clear()

        next_cutoff = datetime(2026, 9, 30, 23, 45, tzinfo=DUBAI)
        self.assertEqual(
            emit_cycle_notifications(
                self.settings, self.state(), [], now=next_cutoff, sender=self.sender
            ),
            1,
        )
        self.assertIn("BTC_USDT LONG | WIN | PROFIT_LOCK", self.sent[0])
        self.assertIn("Monthly (31 Aug 23:45–30 Sep 23:45)", self.sent[0])


if __name__ == "__main__":
    unittest.main()