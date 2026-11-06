"""Vault event listener: eth_getLogs polling with a saved cursor (ranges <= 2,000 blocks).

On OfframpLocked it matches id, user, amount, inrPaise, rateE6 and qrCommitment against the
stored quote, recomputing keccak256(abi.encode(salt, qrText)). It NEVER pays on the event alone:
unknown quote or any mismatch -> settler refund reason 4; bad QR -> reason 1/2; else -> payout.
Every other vault event is mirrored into the payments table and pushed to the SSE hub.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from .commitment import hex32, qr_commitment
from .common import EventHub
from .qr import REASON_OPERATOR, parse_upi_qr
from .store import Store

log = logging.getLogger("rupeerail.listener")

ID_EVENTS = {
    "OfframpLocked", "OfframpCompleted", "OfframpRefunded", "OfframpReversed", "OfframpDisputed",
    "OfframpReleased", "DisputeResolved", "DisputeTimedOut",
}


def match_quote(quote: dict | None, *, chain: str, id_: str, user: str, amount: int, inr_paise: int,
                rate_e6: int, commitment: str) -> list[str]:
    """Returns the list of mismatches between a lock event and our stored quote (empty = match)."""
    if quote is None:
        return ["unknown quote id (not issued by this backend)"]
    problems = []
    if quote["id"].lower() != id_.lower():
        problems.append("id")
    if quote["chain"] != chain:
        problems.append(f"chain (quote {quote['chain']})")
    if quote["user"].lower() != user.lower():
        problems.append("user")
    if int(quote["amount"]) != int(amount):
        problems.append("amount")
    if int(quote["inr_paise"]) != int(inr_paise):
        problems.append("inrPaise")
    if int(quote["rate_e6"]) != int(rate_e6):
        problems.append("rateE6")
    if quote["qr_commitment"].lower() != commitment.lower():
        problems.append("qrCommitment (event vs stored)")
    recomputed = hex32(qr_commitment(quote["salt"], quote["qr_text"]))
    if recomputed.lower() != commitment.lower():
        problems.append("qrCommitment (recomputed keccak(salt, qrText) differs)")
    return problems


class Listener:
    def __init__(self, chain: str, client: Any, store: Store, settler: Any, runner: Any, *,
                 hub: EventHub | None = None, poll_sec: float = 3.0, max_range: int = 2000,
                 confirmations: int = 1, max_ranges_per_poll: int = 25, inline_dispatch: bool = False) -> None:
        self.chain, self.client, self.store, self.settler, self.runner = chain, client, store, settler, runner
        self.hub = hub
        self.poll_sec, self.max_range = poll_sec, min(max_range, 2000)
        self.confirmations, self.max_ranges_per_poll = confirmations, max_ranges_per_poll
        self.inline_dispatch = inline_dispatch
        self.tasks: set[asyncio.Task] = set()
        self.last_poll_at: float | None = None
        self.last_error: str | None = None
        self.head: int | None = None
        self.polls = 0
        self._polling = asyncio.Lock()

    # ---- lifecycle --------------------------------------------------------------------------
    async def init_cursor(self) -> int:
        cur = self.store.get_cursor(self.chain)
        if cur is None:
            cur = await self.client.start_block() - 1
            self.store.set_cursor(self.chain, cur)
        return cur

    async def recover(self) -> None:
        """Re-drive LOCKED payments that never got a decision (crash between insert and decide)."""
        for p in self.store.payments_where(chain=self.chain, statuses=["LOCKED"], dispatch=["NONE"]):
            if p["pending_refund"] is None:
                await self.decide(p)

    async def run(self) -> None:
        await self.init_cursor()
        await self.recover()
        while True:
            await self.poll()
            await asyncio.sleep(self.poll_sec)

    async def drain(self) -> None:
        while self.tasks:
            await asyncio.gather(*list(self.tasks), return_exceptions=True)

    # ---- polling ----------------------------------------------------------------------------
    async def poll(self) -> int:
        """Processes up to max_ranges_per_poll ranges; returns the number of events handled."""
        if self._polling.locked():
            return 0
        handled = 0
        async with self._polling:
            try:
                cursor = await self.init_cursor()
                head = await self.client.block_number() - self.confirmations
                self.head = head
                frm = cursor + 1
                ranges = 0
                while frm <= head and ranges < self.max_ranges_per_poll:
                    to = min(frm + self.max_range - 1, head)
                    events = await self.client.get_logs(frm, to)
                    for ev in events:
                        await self.handle(ev)
                        handled += 1
                    self.store.set_cursor(self.chain, to)
                    frm = to + 1
                    ranges += 1
                self.last_error = None
            except Exception as e:
                self.last_error = f"{type(e).__name__}: {str(e).splitlines()[0][:300] if str(e) else ''}"
                log.warning("[listener:%s] poll error: %s", self.chain, self.last_error)
            finally:
                self.last_poll_at = time.time()
                self.polls += 1
        return handled

    # ---- event handling ---------------------------------------------------------------------
    async def handle(self, ev: Any) -> None:
        a = ev.args
        ref = a.get("id") or a.get("orderId")
        live = {  # same shape as a GET /events item (API v1 3.7)
            "type": "chain_event", "chain": self.chain, "name": ev.name, "id": ref, "block": str(ev.block),
            "txHash": ev.tx, "explorer": self.client.tx_url(ev.tx),
            "args": {k: (str(v) if isinstance(v, int) and not isinstance(v, bool) else v) for k, v in a.items()},
        }
        is_new = self.store.insert_event({"chain": self.chain, "tx": ev.tx, "log_index": ev.log_index,
                                          "block": ev.block, "name": ev.name, "ref_id": ref, "args": live["args"]})
        if is_new:
            log.info("[listener:%s] %s %s %s", self.chain, ev.name, (ref or "")[:10], live["explorer"])
            if self.hub:
                self.hub.publish(live)

        name = ev.name
        if name == "OfframpLocked":
            await self.on_locked(ev)
            return
        if name not in ID_EVENTS and name != "OnrampPaid":
            return
        if name == "OnrampPaid":
            order = self.store.get_onramp(ref)
            if order and order["status"] != "PAID":
                self.store.cas_onramp(ref, order["status"], "PAID", tx=ev.tx)
            return

        self.store.ensure_payment(ref, self.chain)
        if name == "OfframpCompleted":
            dl, ts = await asyncio.gather(self._deadlines(ref), self.client.block_timestamp(ev.block))
            # releasableAt = completion time + the dispute window fixed at lock. Computed, not read:
            # a later dispute overwrites the onchain slot with the arbiter's deadline.
            self.store.update_payment(ref, status="COMPLETED", payout_ref=a.get("payoutRef"), completed_at=ts,
                                      **({"releasable_at": ts + dl.dispute_window} if dl else {}))
            self.store.add_tx(ref, "complete", ev.tx)
        elif name == "OfframpRefunded":
            self.store.update_payment(ref, status="REFUNDED", refund_reason=int(a["reason"]), pending_refund=None)
            self.store.add_tx(ref, "refund", ev.tx)
        elif name == "OfframpReversed":
            self.store.update_payment(ref, status="REVERSED")
            self.store.add_tx(ref, "reverse", ev.tx)
        elif name == "OfframpDisputed":
            dl = await self._deadlines(ref)
            # onchain releasableAt now holds the arbiter's deadline: store it in its own column
            self.store.update_payment(ref, status="DISPUTED", **({"dispute_deadline": dl.releasable_at} if dl else {}))
            self.store.add_tx(ref, "dispute", ev.tx)
        elif name == "OfframpReleased":
            self.store.update_payment(ref, status="RELEASED")
            self.store.add_tx(ref, "release", ev.tx)
        elif name == "DisputeResolved":
            self.store.update_payment(ref, status="REVERSED" if a.get("refundedUser") else "RELEASED")
            self.store.add_tx(ref, "resolve", ev.tx)
        elif name == "DisputeTimedOut":
            self.store.update_payment(ref, status="REVERSED")
            self.store.add_tx(ref, "disputeTimeout", ev.tx)

    async def _deadlines(self, id_: str):
        try:
            return await self.client.deadlines(id_)
        except Exception as e:
            log.warning("[listener:%s] deadlines(%s) failed: %s", self.chain, id_[:10], e)
            return None

    async def on_locked(self, ev: Any) -> None:
        a = ev.args
        id_ = a["id"].lower()
        ts, dl = await asyncio.gather(self.client.block_timestamp(ev.block), self._deadlines(id_))
        self.store.upsert_locked({
            "id": id_, "chain": self.chain, "user": a["user"], "amount": a["amount"], "inr_paise": a["inrPaise"],
            "rate_e6": a["rateE6"], "qr_commitment": a["qrCommitment"], "lock_tx": ev.tx, "lock_block": ev.block,
            "locked_at": ts,
            "completable_at": dl.completable_at if dl else None,
            "reclaimable_at": dl.reclaimable_at if dl else None,
            "dispute_window": dl.dispute_window if dl else None,
        })
        q = self.store.get_quote(id_)
        if q and q["status"] != "USED":
            self.store.set_quote_status(id_, "USED", offramp_tx=ev.tx)
        p = self.store.get_payment(id_)
        if p and p["status"] == "LOCKED" and p["dispatch_state"] == "NONE" and p["pending_refund"] is None \
                and p["match_ok"] is None:
            await self.decide(p)

    async def decide(self, p: dict) -> str:
        """Pay or refund one LOCKED offramp. Returns 'refund:<reason>' or 'dispatch'."""
        id_ = p["id"]
        q = self.store.get_quote(id_)
        problems = match_quote(q, chain=self.chain, id_=id_, user=p["user"] or "", amount=int(p["amount"] or 0),
                               inr_paise=int(p["inr_paise"] or 0), rate_e6=int(p["rate_e6"] or 0),
                               commitment=p["qr_commitment"] or "0x")
        if problems:
            detail = "; ".join(problems)
            log.warning("[listener:%s] %s does not match a stored quote (%s) -> refund reason 4",
                        self.chain, id_[:10], detail)
            self.store.cas_dispatch(id_, "NONE", "SKIPPED", detail, match_ok=0, match_detail=detail,
                                    pending_refund=REASON_OPERATOR)
            await self._refund(id_, REASON_OPERATOR)
            return f"refund:{REASON_OPERATOR}"

        parsed = parse_upi_qr(q["qr_text"], int(p["inr_paise"]))
        if not parsed.ok:
            self.store.cas_dispatch(id_, "NONE", "SKIPPED", parsed.detail, match_ok=1, match_detail="matched",
                                    pending_refund=parsed.reason)
            await self._refund(id_, int(parsed.reason))
            return f"refund:{parsed.reason}"

        self.store.update_payment(id_, match_ok=1, match_detail="matched")
        fresh = self.store.get_payment(id_)
        coro = self.runner.dispatch(fresh, parsed)
        if self.inline_dispatch:
            await coro
        else:
            task = asyncio.create_task(coro)
            self.tasks.add(task)
            task.add_done_callback(self.tasks.discard)
        return "dispatch"

    async def _refund(self, id_: str, reason: int) -> None:
        try:
            outcome = await self.settler.refund(self.chain, id_, reason)
            log.info("[listener:%s] refund %s (reason %d): %s", self.chain, id_[:10], reason, outcome)
        except Exception as e:  # retried by the settler loop (pending_refund is set)
            log.warning("[listener:%s] refund %s deferred to settler loop: %s", self.chain, id_[:10], e)

    def status(self) -> dict:
        return {
            "cursor": self.store.get_cursor(self.chain),
            "head": self.head,
            "polls": self.polls,
            "lastPollAgeSec": round(time.time() - self.last_poll_at, 1) if self.last_poll_at else None,
            "lastError": self.last_error,
        }
