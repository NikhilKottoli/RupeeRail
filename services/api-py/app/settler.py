"""Settler: completeOfframp, refundOfframp, reverseOfframp, onramp.

Every action first reads the onchain status, so calling it twice (or after a crash) is a no-op
rather than a second transfer. The loop retries pending refunds and completions until they land.

Actions on the same offramp are serialised by a per-id lock, and the status is read inside it.
Without this, a direct refund (e.g. from the payout runner) and the background loop could both
see LOCKED while the first transaction was still in flight and both send.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from eth_utils import keccak

from .chain import ChainError, DryRunError
from .commitment import hex32, to_bytes32
from .common import EventHub
from .store import STATUS_NAMES, Store

log = logging.getLogger("rupeerail.settler")

LOCKED, COMPLETED, REFUNDED, REVERSED, RELEASED, DISPUTED = 1, 2, 3, 4, 5, 6


def default_payout_ref(id_: str) -> str:
    """keccak256(id): unique per offramp and non-zero, used when the payout gives no UTR."""
    return hex32(keccak(to_bytes32(id_)))


class Settler:
    def __init__(self, clients: dict[str, Any], store: Store, account: Any, *, alert_after_sec: int = 3600,
                 hub: EventHub | None = None, clock=time.time) -> None:
        self.clients, self.store, self.account = clients, store, account
        self.alert_after_sec = alert_after_sec
        self.hub = hub
        self.clock = clock
        self.last_error: str | None = None
        self._busy: set[str] = set()
        self._locks: dict[str, asyncio.Lock] = {}

    def _lock(self, id_: str) -> asyncio.Lock:
        """One lock per offramp id, shared by every settler action on it."""
        return self._locks.setdefault(id_.lower(), asyncio.Lock())

    def busy(self, id_: str) -> bool:
        """True while an action on this offramp is running (its transaction may be in flight)."""
        lock = self._locks.get(id_.lower())
        return bool(lock and lock.locked())

    def _publish(self, kind: str, **data: Any) -> None:
        if self.hub:
            self.hub.publish({"type": kind, **data})

    async def _mirror(self, chain: str, id_: str, status: int) -> None:
        self.store.ensure_payment(id_, chain)
        self.store.update_payment(id_, status=STATUS_NAMES[status])

    # ---- complete ---------------------------------------------------------------------------
    async def complete(self, chain: str, id_: str) -> str:
        async with self._lock(id_):
            return await self._complete(chain, id_)

    async def _complete(self, chain: str, id_: str) -> str:
        c = self.clients[chain]
        p = self.store.get_payment(id_) or {}
        payout_ref = p.get("payout_ref") or default_payout_ref(id_)
        st = await c.offramp_state(id_)
        if st.status != LOCKED:
            if st.status != 0:
                await self._mirror(chain, id_, st.status)
            return f"not_locked:{st.status_name}"
        dl = await c.deadlines(id_)
        t = await c.chain_time()
        if t < dl.completable_at:
            return "too_early"
        if t >= dl.reclaimable_at:
            log.error("ALERT [%s] %s completion window closed at %d; vendor was paid but escrow is reclaimable",
                      chain, id_, dl.reclaimable_at)
            return "window_closed"
        res = await c.send(self.account, "completeOfframp", [to_bytes32(id_), to_bytes32(payout_ref)])
        self.store.update_payment(id_, status="COMPLETED", payout_ref=payout_ref,
                                  releasable_at=t + dl.dispute_window)
        self.store.add_tx(id_, "complete", res.hash)
        self._publish("settler", action="completeOfframp", chain=chain, id=id_, tx=res.hash,
                      explorer=c.tx_url(res.hash))
        log.info("[settler:%s] completed %s %s", chain, id_[:10], c.tx_url(res.hash))
        return "completed"

    # ---- refund -----------------------------------------------------------------------------
    async def refund(self, chain: str, id_: str, reason: int) -> str:
        if not 1 <= reason <= 4:
            raise ValueError("refund reason must be 1-4")
        async with self._lock(id_):
            return await self._refund(chain, id_, reason)

    async def _refund(self, chain: str, id_: str, reason: int) -> str:
        c = self.clients[chain]
        self.store.ensure_payment(id_, chain)
        st = await c.offramp_state(id_)
        if st.status != LOCKED:
            self.store.update_payment(id_, pending_refund=None)
            if st.status != 0:
                await self._mirror(chain, id_, st.status)
            return f"not_locked:{st.status_name}"
        self.store.update_payment(id_, pending_refund=reason)
        res = await c.send(self.account, "refundOfframp", [to_bytes32(id_), reason])
        self.store.update_payment(id_, status="REFUNDED", refund_reason=reason, pending_refund=None)
        self.store.add_tx(id_, "refund", res.hash)
        self._publish("settler", action="refundOfframp", chain=chain, id=id_, reason=reason, tx=res.hash,
                      explorer=c.tx_url(res.hash))
        log.info("[settler:%s] refunded %s reason %d %s", chain, id_[:10], reason, c.tx_url(res.hash))
        return "refunded"

    # ---- reverse ----------------------------------------------------------------------------
    async def reverse(self, chain: str, id_: str) -> str:
        async with self._lock(id_):
            return await self._reverse(chain, id_)

    async def _reverse(self, chain: str, id_: str) -> str:
        c = self.clients[chain]
        st = await c.offramp_state(id_)
        if st.status != COMPLETED:
            return f"not_completed:{st.status_name}"
        dl = await c.deadlines(id_)
        if await c.chain_time() >= dl.releasable_at:
            return "window_closed"
        res = await c.send(self.account, "reverseOfframp", [to_bytes32(id_)])
        self.store.update_payment(id_, status="REVERSED")
        self.store.add_tx(id_, "reverse", res.hash)
        self._publish("settler", action="reverseOfframp", chain=chain, id=id_, tx=res.hash,
                      explorer=c.tx_url(res.hash))
        return "reversed"

    # ---- onramp -----------------------------------------------------------------------------
    async def onramp(self, chain: str, order_id: str, wallet: str, amount: int, inr_paise: int) -> tuple[str, str | None]:
        c = self.clients[chain]
        if await c.call("onrampDone", to_bytes32(order_id)):
            return "already_paid", None
        res = await c.send(self.account, "onramp", [to_bytes32(order_id), wallet, int(amount), int(inr_paise)])
        self._publish("settler", action="onramp", chain=chain, id=order_id, tx=res.hash, explorer=c.tx_url(res.hash))
        return "paid", res.hash

    # ---- loop -------------------------------------------------------------------------------
    async def _guarded(self, key: str, coro) -> str | None:
        if key in self._busy or self.busy(key):  # skip ids with an action already in flight; retry next tick
            coro.close()
            return None
        self._busy.add(key)
        try:
            return await coro
        except DryRunError as e:
            log.info("[settler] %s", e)
            return "dry_run"
        except ChainError as e:
            self.last_error = str(e)
            log.warning("[settler] %s", e)
            return f"error:{e.name}"
        except Exception as e:
            self.last_error = f"{type(e).__name__}: {e}"
            log.warning("[settler] %s failed: %s", key, self.last_error)
            return "error"
        finally:
            self._busy.discard(key)

    async def tick(self) -> dict[str, str | None]:
        out: dict[str, str | None] = {}
        for p in self.store.pending_refunds():
            if p["chain"] in self.clients:
                out[p["id"]] = await self._guarded(p["id"], self.refund(p["chain"], p["id"], int(p["pending_refund"])))
        for p in self.store.payments_where(statuses=["LOCKED"], dispatch=["DISPATCHED"]):
            if p["chain"] not in self.clients:
                continue
            out[p["id"]] = await self._guarded(p["id"], self.complete(p["chain"], p["id"]))
            since = p.get("dispatched_at") or p.get("locked_at") or p["created_at"]
            if out[p["id"]] != "completed" and not p["alerted"] and self.clock() - since > self.alert_after_sec:
                log.error("ALERT [%s] %s paid out %ds ago but still not completed onchain (last: %s)",
                          p["chain"], p["id"], int(self.clock() - since), out[p["id"]])
                self.store.update_payment(p["id"], alerted=1)
        return out

    async def run(self, interval: float) -> None:
        while True:
            try:
                await self.tick()
            except Exception as e:
                log.warning("[settler] tick failed: %s", e)
            await asyncio.sleep(interval)
