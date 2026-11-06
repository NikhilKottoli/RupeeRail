"""One running goal. Safe to emit events from the payment thread."""

from __future__ import annotations

import asyncio
import threading
import time
import uuid
from typing import Callable

Send = Callable[[dict], None]
MAX_EVENTS = 300


class AgentSession:
    def __init__(self, goal: str, max_paise: int, allowed_domains: list[str]) -> None:
        self.id = uuid.uuid4().hex
        self.goal, self.max_paise, self.allowed_domains = goal, max_paise, allowed_domains
        self.state = "created"
        self.result: dict = {}
        self.events: list[dict] = []
        self.stopped = threading.Event()  # read by the payment thread as its kill switch
        self._resume = asyncio.Event()
        self._send: Send | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    def attach(self, send: Send, loop: asyncio.AbstractEventLoop) -> None:
        self._send, self._loop = send, loop

    def detach(self) -> None:
        self._send = None

    def emit(self, stage: str, message: str, **extra: object) -> None:
        screenshot = extra.pop("screenshot", None)
        event = {"type": "event", "stage": stage, "message": message, "at": time.time(), **extra}
        self.events.append(event)
        del self.events[:-MAX_EVENTS]
        if self._send and self._loop:
            outgoing = {**event, "screenshot": screenshot} if screenshot else event
            self._loop.call_soon_threadsafe(self._send, outgoing)

    def post(self, message: dict) -> None:
        """Queues a message behind every event already emitted, so the order is kept across threads."""
        if self._send and self._loop:
            self._loop.call_soon_threadsafe(self._send, message)

    def stop(self) -> None:
        self.stopped.set()
        if self._loop:
            self._loop.call_soon_threadsafe(self._resume.set)
        else:
            self._resume.set()

    def resume(self) -> None:
        self._resume.set()

    async def wait_for_resume(self, timeout: float) -> bool:
        self._resume.clear()
        if self.stopped.is_set():
            return False
        try:
            await asyncio.wait_for(self._resume.wait(), timeout)
        except asyncio.TimeoutError:
            return False
        return not self.stopped.is_set()
