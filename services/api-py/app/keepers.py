"""Permissionless keepers, run from the paymaster key.

  LOCKED    and now >= reclaimableAt -> reclaimOfframp       (user gets the escrow back, reason 5)
  COMPLETED and now >= releasableAt  -> releaseOfframp       (held funds -> pool)
  DISPUTED  and now >= releasableAt  -> claimDisputeTimeout  (held funds -> user)

While DISPUTED, `releasableAt` holds the arbiter's resolve deadline, not the dispute-window end;
the status must always be read first. The stored mirror is only a pre-filter: every action
re-reads status and deadlines onchain before sending.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from .chain import ChainError, Deadlines, DryRunError
from .commitment import to_bytes32
from .common import EventHub
from .store import STATUS_NAMES, Store

log = logging.getLogger("rupeerail.keepers")

LOCKED, COMPLETED, DISPUTED = 1, 2, 6
ACTION_FN = {"reclaim": "reclaimOfframp", "release": "releaseOfframp", "claim_timeout": "claimDisputeTimeout"}
ACTION_RESULT = {"reclaim": "REFUNDED", "release": "RELEASED", "claim_timeout": "REVERSED"}


def choose_action(status: int, dl: Deadlines, now: int) -> str | None:
    if status == LOCKED:
        return "reclaim" if dl.reclaimable_at and now >= dl.reclaimable_at else None
    if status == COMPLETED:
        # releasableAt = end of the dispute window
        return "release" if dl.releasable_at and now >= dl.releasable_at else None
    if status == DISPUTED:
        # releasableAt = the arbiter's resolve deadline (disputedAt + disputeTimeout)
        return "claim_timeout" if dl.releasable_at and now >= dl.releasable_at else None
    return None


def _due_by_mirror(p: dict, now: int) -> bool:
    if p["status"] == "LOCKED":
        t = p["reclaimable_at"]
    elif p["status"] == "COMPLETED":
        t = p["releasable_at"]
    elif p["status"] == "DISPUTED":
        t = p["dispute_deadline"]
    else:
        return False
    return t is None or now >= int(t)  # unknown timers: check onchain


class Keeper:
    def __init__(self, clients: dict[str, Any], store: Store, account: Any, hub: EventHub | None = None) -> None:
        self.clients, self.store, self.account, self.hub = clients, store, account, hub
        self.last_error: str | None = None

    async def act(self, chain: str, id_: str) -> str | None:
        c = self.clients[chain]
        st = await c.offramp_state(id_)
        dl = await c.deadlines(id_)
        now = await c.chain_time()
        timers = {"reclaimable_at": dl.reclaimable_at or None}
        if st.status == DISPUTED:
            timers["dispute_deadline"] = dl.releasable_at or None  # overloaded slot while DISPUTED
        elif st.status == COMPLETED:
            timers["releasable_at"] = dl.releasable_at or None
        self.store.update_payment(id_, status=STATUS_NAMES[st.status], **timers)
        action = choose_action(st.status, dl, now)
        if action is None:
            return None
        res = await c.send(self.account, ACTION_FN[action], [to_bytes32(id_)])
        self.store.update_payment(id_, status=ACTION_RESULT[action],
                                  **({"refund_reason": 5} if action == "reclaim" else {}))
        self.store.add_tx(id_, action, res.hash)
        if self.hub:
            self.hub.publish({"type": "keeper", "action": ACTION_FN[action], "chain": chain, "id": id_,
                              "tx": res.hash, "explorer": c.tx_url(res.hash)})
        log.info("[keeper:%s] %s %s %s", chain, ACTION_FN[action], id_[:10], c.tx_url(res.hash))
        return action

    async def tick(self) -> dict[str, str | None]:
        out: dict[str, str | None] = {}
        for chain, c in self.clients.items():
            rows = self.store.payments_where(chain=chain, statuses=["LOCKED", "COMPLETED", "DISPUTED"])
            if not rows:
                continue
            try:
                now = await c.chain_time()
            except Exception as e:
                self.last_error = f"{chain}: {e}"
                continue
            for p in rows:
                if not _due_by_mirror(p, now):
                    continue
                try:
                    out[p["id"]] = await self.act(chain, p["id"])
                except DryRunError as e:
                    log.info("[keeper] %s", e)
                    out[p["id"]] = "dry_run"
                except ChainError as e:
                    self.last_error = str(e)
                    log.warning("[keeper:%s] %s", chain, e)
                    out[p["id"]] = f"error:{e.name}"
                except Exception as e:
                    self.last_error = f"{type(e).__name__}: {e}"
                    log.warning("[keeper:%s] %s failed: %s", chain, p["id"][:10], self.last_error)
                    out[p["id"]] = "error"
        return out

    async def run(self, interval: float) -> None:
        while True:
            try:
                await self.tick()
            except Exception as e:
                log.warning("[keeper] tick failed: %s", e)
            await asyncio.sleep(interval)
