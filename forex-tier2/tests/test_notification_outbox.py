import asyncio
import tempfile
import unittest
from pathlib import Path

from telegram_bot.notification_outbox import NotificationOutbox


class NotificationOutboxTests(unittest.TestCase):
    def test_retries_persistently_and_deduplicates_events(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "telegram-outbox.json"
            outbox = NotificationOutbox(path, retry_base_seconds=0)
            self.assertTrue(outbox.enqueue("Trade closed #42", event_key="close-42"))
            self.assertFalse(outbox.enqueue("Trade closed #42", event_key="close-42"))

            attempts = []

            async def sender(message):
                attempts.append(message)
                if len(attempts) == 1:
                    raise RuntimeError("temporary Telegram outage")

            async def deliver():
                self.assertFalse(await outbox.deliver_due_once(sender))
                self.assertEqual(outbox.pending_count, 1)
                self.assertTrue(await outbox.deliver_due_once(sender))

            asyncio.run(deliver())
            self.assertEqual(attempts, ["Trade closed #42", "Trade closed #42"])
            self.assertEqual(outbox.pending_count, 0)

            restarted = NotificationOutbox(path)
            self.assertFalse(restarted.enqueue("Trade closed #42", event_key="close-42"))


if __name__ == "__main__":
    unittest.main()