"""Shared bits: the API error type, the live event hub (feeds /events/stream), signature checks."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from .eip712 import ecdsa_try_recover, normalize_v

log = logging.getLogger("rupeerail")


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str, **extra: Any) -> None:
        super().__init__(message)
        self.status, self.code, self.message, self.extra = status, code, message, extra

    def to_json(self) -> dict[str, Any]:
        return {"error": self.code, "message": self.message, **self.extra}


class EventHub:
    """Fan-out of live events to SSE subscribers. Slow subscribers drop events, never block."""

    def __init__(self, maxsize: int = 200) -> None:
        self._subs: set[asyncio.Queue] = set()
        self._maxsize = maxsize

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(self._maxsize)
        self._subs.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subs.discard(q)

    def publish(self, event: dict[str, Any]) -> None:
        for q in list(self._subs):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                pass

    @property
    def subscribers(self) -> int:
        return len(self._subs)


async def verify_user_signature(client: Any, user: str, digest: bytes, sig: bytes) -> tuple[bool, bytes, str]:
    """Mirror of OZ 5.1 SignatureChecker.isValidSignatureNow.

    No code at `user` -> ECDSA (65 bytes, low s, v 27/28; v 0/1 is normalised to 27/28);
    code at `user` (smart wallet, or an EIP-7702 delegated EOA) -> ERC-1271 isValidSignature eth_call.
    Returns (ok, signature to relay, how it was checked).
    """
    code = await client.get_code(user)
    if len(code) == 0:
        sig_n = normalize_v(sig)
        rec = ecdsa_try_recover(digest, sig_n)
        return (rec is not None and rec.lower() == user.lower()), sig_n, "ecdsa"
    ok = await client.is_valid_1271(user, digest, sig)
    return ok, sig, "erc1271"


class RateLimiter:
    """Sliding one-minute window per key (user address or client IP). In-memory, per process."""

    def __init__(self, per_minute: int, clock=None) -> None:
        import time as _t

        self.per_minute = per_minute
        self.clock = clock or _t.time
        self._hits: dict[str, list[float]] = {}

    def check(self, key: str) -> None:
        if self.per_minute <= 0:
            return
        now = self.clock()
        hits = [t for t in self._hits.get(key, []) if now - t < 60]
        if len(hits) >= self.per_minute:
            self._hits[key] = hits
            raise ApiError(429, "RATE_LIMITED", f"more than {self.per_minute} requests per minute; slow down")
        hits.append(now)
        self._hits[key] = hits
        if len(self._hits) > 10_000:  # bound memory
            self._hits = {k: v for k, v in self._hits.items() if v and now - v[-1] < 60}
