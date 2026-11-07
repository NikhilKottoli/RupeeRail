"""Publishes an agent's public record to Arkiv, through the local publisher in services/arkiv-board.

Requests wait in a table and are delivered in order by one background thread, so a slow or stopped publisher can't
affect a run, and nothing is lost if this server restarts. Every public method here swallows its own failures, because
a public record must never affect a payment. machine.py and payment/ must never import this.

Only card_fields and receipt_fields decide what leaves this machine. A prompt, profile, instruction, site, payee or
failure detail has no way in, and the publisher refuses any field it doesn't know.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Callable

from .store import AgentStore

log = logging.getLogger("rupeerail.agent")

# Only a run that locked funds has a receipt, and a dry run has nothing to report.
OUTCOMES = {"PAID": "paid", "NOT_PAID": "not_paid", "CHECK_THE_PHONE": "unknown", "NO_RESULT": "unknown"}
REQUEST_TIMEOUT_SEC = 120.0  # a write waits for its block
RETRY_MIN_SEC = 5.0
RETRY_MAX_SEC = 60.0
IDLE_SEC = 30.0


def card_fields(agent: dict) -> dict:
    return {"wallet": agent["wallet"], "category": agent["category"], "name": agent["name"], "avatar": agent["avatar"], "colour": agent["colour"]}


def receipt_fields(run: dict, chain: str) -> dict | None:
    """What a run's public receipt says, or None if it doesn't get one."""
    outcome = OUTCOMES.get(run.get("status") or "")
    if run.get("rehearse") or not outcome or not run.get("offramp_id") or not run.get("tx_hash") or not run.get("amount_paise") or not run.get("finished"):
        return None
    return {"offrampId": run["offramp_id"], "outcome": outcome, "inrPaise": int(run["amount_paise"]), "finishedAt": int(run["finished"] * 1000), "chain": chain, "lockTx": run["tx_hash"]}


def post_op(url: str, token: str, op: dict) -> int:
    request = urllib.request.Request(f"{url}/ops", json.dumps(op).encode(), {"content-type": "application/json", "x-publisher-token": token}, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SEC) as response:
            return response.status
    except urllib.error.HTTPError as e:
        return e.code


def _never_raises(method: Callable[..., None]) -> Callable[..., None]:
    def wrapped(self: "ArkivOutbox", *args: Any, **kwargs: Any) -> None:
        try:
            method(self, *args, **kwargs)
        except Exception as e:
            log.warning("couldn't queue a public record: %s", type(e).__name__)
    return wrapped


class ArkivOutbox:
    def __init__(self, store: AgentStore, url: str, token: str, chain: str, post: Callable[[str, str, dict], int] = post_op, clock: Callable[[], float] = time.time) -> None:
        self.store, self.url, self.token, self.chain, self.post, self.clock = store, url.rstrip("/"), token, chain, post, clock
        self.enabled = bool(url and token)
        self.wake = threading.Event()
        self.counter = 0

    def start(self) -> None:
        if self.enabled:
            threading.Thread(target=self._run, daemon=True).start()

    # ---- what happened ----------------------------------------------------------------------------

    def _op(self, label: str, **fields: Any) -> None:
        self.counter += 1
        op_id = f"{label}:{int(self.clock() * 1000)}.{self.counter}"
        self.store.outbox_add(op_id, json.dumps({"id": op_id, **fields}))
        self.wake.set()

    def _publishing(self, agent: dict | None) -> bool:
        return bool(self.enabled and agent and agent["published"] and agent["wallet"])

    @_never_raises
    def card_changed(self, agent: dict) -> None:
        if self._publishing(agent):
            self._op("card", kind="card", card=card_fields(agent))

    @_never_raises
    def publish_history(self, agent: dict) -> None:
        """When an agent is first published: its card, then its receipts so far, oldest first."""
        if not self._publishing(agent):
            return
        self._op("card", kind="card", card=card_fields(agent))
        for run in self.store.locked_runs(agent["id"]):
            receipt = receipt_fields(run, self.chain)
            if receipt:
                self._op("receipt", kind="run_finished", card=card_fields(agent), receipt=receipt)

    @_never_raises
    def run_started(self, agent: dict | None) -> None:
        if self._publishing(agent):
            self._op("started", kind="run_started", card=card_fields(agent), startedAt=int(self.clock() * 1000))

    @_never_raises
    def run_finished(self, agent: dict | None, run: dict | None) -> None:
        if not self._publishing(agent) or not run:
            return
        receipt = receipt_fields(run, self.chain)
        if receipt:
            self._op("receipt", kind="run_finished", card=card_fields(agent), receipt=receipt)
        else:
            self._op("ended", kind="run_ended", wallet=agent["wallet"])

    @_never_raises
    def withdraw(self, agent: dict, receipts: bool) -> None:
        """Removes what's live. Copies already read, and the chain's history, can't be taken back."""
        if self.enabled and agent["wallet"]:
            self._op("withdraw", kind="withdraw", wallet=agent["wallet"], receipts=receipts)

    # ---- delivery ----------------------------------------------------------------------------------

    def deliver_one(self) -> str:
        """Tries the oldest waiting request once. Returns idle, sent, dropped, or retry."""
        waiting = self.store.outbox_next()
        if not waiting:
            return "idle"
        try:
            status = self.post(self.url, self.token, json.loads(waiting["op"]))
        except Exception as e:
            log.warning("the Arkiv publisher can't be reached: %s", type(e).__name__)
            return "retry"
        if status == 200:
            self.store.outbox_done(waiting["id"])
            return "sent"
        if status == 400:  # it will never be accepted, and mustn't hold up the rest
            log.warning("the Arkiv publisher refused %s", waiting["id"].split(":")[0])
            self.store.outbox_done(waiting["id"])
            return "dropped"
        if status == 409:
            log.warning("an Arkiv write has an uncertain outcome. Run npm run reconcile in services/arkiv-board")
        return "retry"

    def _run(self) -> None:
        delay = RETRY_MIN_SEC
        while True:
            try:
                result = self.deliver_one()
            except Exception as e:
                log.warning("the Arkiv outbox failed: %s", type(e).__name__)
                result = "retry"
            if result == "idle":
                self.wake.wait(IDLE_SEC)
                self.wake.clear()
                delay = RETRY_MIN_SEC
            elif result == "retry":
                time.sleep(delay)
                delay = min(delay * 2, RETRY_MAX_SEC)
            else:
                delay = RETRY_MIN_SEC
