"""Durable, non-blocking Telegram notification delivery."""
from __future__ import annotations

import asyncio
import hashlib
import json
import time
from pathlib import Path
from typing import Awaitable, Callable


Sender = Callable[[str], Awaitable[None]]


class NotificationOutbox:
    """Persist pending alerts before delivery so broker monitoring never waits."""

    def __init__(self, path: Path, retry_base_seconds: float = 2.0):
        self.path = path
        self.retry_base_seconds = retry_base_seconds
        self._lock = asyncio.Lock()
        self._running = False
        self._task: asyncio.Task | None = None
        self._data = self._load()

    def _load(self) -> dict:
        try:
            data = json.loads(self.path.read_text())
            if isinstance(data, dict):
                data.setdefault("pending", [])
                data.setdefault("delivered", {})
                return data
        except (OSError, ValueError, TypeError):
            pass
        return {"pending": [], "delivered": {}}

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self._data, ensure_ascii=False, indent=2))
        temporary.replace(self.path)

    def _prune_delivered(self, now: float) -> None:
        retention = 7 * 24 * 60 * 60
        self._data["delivered"] = {
            key: timestamp
            for key, timestamp in self._data["delivered"].items()
            if now - float(timestamp) < retention
        }

    def enqueue(self, message: str, event_key: str | None = None) -> bool:
        """Record the event synchronously and return immediately without network I/O."""
        now = time.time()
        key = event_key or hashlib.sha256(message.encode("utf-8")).hexdigest()
        self._prune_delivered(now)
        if key in self._data["delivered"] or any(
            item["key"] == key for item in self._data["pending"]
        ):
            return False

        self._data["pending"].append(
            {
                "key": key,
                "message": message,
                "attempts": 0,
                "next_attempt_at": now,
                "created_at": now,
            }
        )
        self._save()
        return True

    @property
    def pending_count(self) -> int:
        return len(self._data["pending"])

    async def deliver_due_once(self, sender: Sender) -> bool:
        """Try one due alert. A failure stays on disk and backs off for retry."""
        async with self._lock:
            now = time.time()
            due = next(
                (item for item in self._data["pending"] if item["next_attempt_at"] <= now),
                None,
            )
            if due is None:
                return False
            key = due["key"]
            message = due["message"]

        try:
            await sender(message)
        except Exception as error:
            async with self._lock:
                current = next(
                    (item for item in self._data["pending"] if item["key"] == key),
                    None,
                )
                if current:
                    current["attempts"] += 1
                    delay = min(
                        300.0,
                        self.retry_base_seconds * (2 ** min(current["attempts"] - 1, 7)),
                    )
                    current["next_attempt_at"] = time.time() + delay
                    current["last_error"] = str(error)[:300]
                    self._save()
            print(f"[Telegram] Delivery deferred ({key[:8]}): {error}")
            return False

        async with self._lock:
            self._data["pending"] = [
                item for item in self._data["pending"] if item["key"] != key
            ]
            self._data["delivered"][key] = time.time()
            self._prune_delivered(time.time())
            self._save()
        return True

    async def _run(self, sender: Sender) -> None:
        while self._running:
            delivered = await self.deliver_due_once(sender)
            await asyncio.sleep(0.1 if delivered else 0.5)

    async def start(self, sender: Sender) -> None:
        if self._task and not self._task.done():
            return
        self._running = True
        self._task = asyncio.create_task(self._run(sender), name="telegram-outbox")

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None