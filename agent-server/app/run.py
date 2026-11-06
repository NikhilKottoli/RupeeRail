"""One agent working on one prompt. Safe to emit events from the payment thread."""

from __future__ import annotations

import asyncio
import re
import threading
import time
import uuid
from typing import Callable

Send = Callable[[dict], None]
MAX_EVENTS = 300
MIN_TERM = 4


def profile_terms(profile: str) -> list[str]:
    """The values in a profile, such as "Delivery address: 1 Test Road" -> "1 Test Road". Longest first, so a longer
    value is hidden whole."""
    terms = set()
    for line in profile.splitlines():
        whole = line.strip()
        value = whole.partition(":")[2].strip() if ":" in whole else whole
        for term in (whole, value, *re.split(r"[,;]", value)):
            if len(term.strip()) >= MIN_TERM:
                terms.add(term.strip())
    return sorted(terms, key=len, reverse=True)


class AgentRun:
    def __init__(self, prompt: str, max_paise: int, allowed_domains: list[str], agent_id: str = "", agent_name: str = "", wallet: str = "",
                 rehearse: bool = False, past_runs: list[str] | None = None, private_terms: list[str] | None = None) -> None:
        self.id = uuid.uuid4().hex
        self.prompt, self.max_paise, self.allowed_domains = prompt, max_paise, allowed_domains
        self.agent_id, self.agent_name, self.wallet = agent_id, agent_name, wallet
        self.rehearse = rehearse
        self.past_runs = past_runs or []
        self.private_terms = private_terms or []
        self.site = ""  # the last allowed host the agent was on
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

    def redact(self, text: str) -> str:
        """Hides profile details that a model repeats. The model gets the profile, but the run's record doesn't."""
        for term in self.private_terms:
            text = re.sub(re.escape(term), "[from profile]", text, flags=re.IGNORECASE)
        return text

    def emit(self, stage: str, message: str, **extra: object) -> None:
        screenshot = extra.pop("screenshot", None)
        event = {"type": "event", "stage": stage, "message": self.redact(message), "at": time.time(), **extra}
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
