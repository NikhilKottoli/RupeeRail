"""Exactly-once INR payout through a black-box command.

  NONE --CAS--> DISPATCHING --exit 0--> DISPATCHED --settler--> completeOfframp
                     |---- non-zero / timeout / spawn error --> FAILED --> refundOfframp(id, 3)
  On boot, a row still DISPATCHING (we crashed mid hand-off; the vendor MAY have been paid)
  becomes NEEDS_OPERATOR and is never re-run.

The command gets one JSON object on stdin, with the offramp id as `idempotencyKey`. Success is
exit code 0 within PAYOUT_TIMEOUT_SEC. If a stdout line is JSON with `utr` (or a 0x `payoutRef`),
the payoutRef becomes keccak256(utr) (or that value); otherwise keccak256(id).
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import shlex
import time
from typing import Any, Awaitable, Callable

from eth_utils import keccak

from .commitment import hex32
from .common import EventHub
from .qr import REASON_DISPATCH_FAILED, ParsedQr
from .quote_math import format_paise
from .settler import default_payout_ref
from .store import Store

log = logging.getLogger("rupeerail.payout")

BlackBox = Callable[[str, dict, float], Awaitable[tuple[bool, str, str]]]
_REF_RE = re.compile(r"^0x[0-9a-fA-F]{64}$")


async def run_black_box(command: str, job: dict, timeout: float) -> tuple[bool, str, str]:
    """Runs the payout command with `job` as JSON on stdin. Returns (ok, detail, stdout)."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *shlex.split(command), stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except Exception as e:
        return False, f"spawn failed: {type(e).__name__}: {e}", ""
    try:
        out, err = await asyncio.wait_for(proc.communicate(json.dumps(job).encode()), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return False, f"timeout after {timeout:g}s", ""
    stdout = out.decode(errors="replace")
    if proc.returncode == 0:
        return True, "exit 0", stdout
    tail = err.decode(errors="replace").strip().splitlines()[-1:] or [""]
    return False, f"exit {proc.returncode} {tail[0][:200]}".strip(), stdout


def payout_ref_from_output(stdout: str, id_: str) -> str:
    for line in reversed(stdout.strip().splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            j = json.loads(line)
        except ValueError:
            continue
        ref = j.get("payoutRef")
        if isinstance(ref, str) and _REF_RE.match(ref) and int(ref, 16) != 0:
            return ref.lower()
        utr = j.get("utr")
        if isinstance(utr, str) and utr.strip():
            return hex32(keccak(text=utr.strip()))
    return default_payout_ref(id_)


class PayoutRunner:
    def __init__(self, store: Store, settler: Any, *, command: str, timeout_sec: float = 30.0,
                 max_lock_age_sec: int = 600, hub: EventHub | None = None,
                 black_box: BlackBox = run_black_box, clock: Callable[[], float] = time.time) -> None:
        self.store, self.settler = store, settler
        self.command, self.timeout_sec, self.max_lock_age_sec = command, timeout_sec, max_lock_age_sec
        self.hub, self.black_box, self.clock = hub, black_box, clock

    def _publish(self, **data: Any) -> None:
        if self.hub:
            self.hub.publish({"type": "payout", **data})

    def recover(self) -> list[str]:
        """Boot-time: rows left DISPATCHING are flagged for a human and never re-run."""
        flagged = []
        for p in self.store.payments_where(dispatch=["DISPATCHING"]):
            if self.store.cas_dispatch(p["id"], "DISPATCHING", "NEEDS_OPERATOR",
                                       "crashed during dispatch; vendor may already be paid; not re-run"):
                flagged.append(p["id"])
                log.error("ALERT [payout] %s was DISPATCHING at startup -> NEEDS_OPERATOR (not re-run)", p["id"])
        return flagged

    async def dispatch(self, payment: dict, parsed: ParsedQr) -> str:
        id_, chain = payment["id"], payment["chain"]
        locked_at = payment.get("locked_at")
        if locked_at is not None and self.clock() - int(locked_at) > self.max_lock_age_sec:
            if self.store.cas_dispatch(id_, "NONE", "SKIPPED",
                                       f"lock older than {self.max_lock_age_sec}s; payout not started",
                                       pending_refund=REASON_DISPATCH_FAILED):
                await self._refund(chain, id_)
                return "too_late"
            return "skipped"

        # Exactly-once gate: the DISPATCHING write happens BEFORE the black box runs.
        if not self.store.cas_dispatch(id_, "NONE", "DISPATCHING", "handing to payout provider",
                                       dispatch_started_at=int(self.clock())):
            return "skipped"

        inr_paise = int(payment["inr_paise"])
        job = {
            "idempotencyKey": id_,
            "offrampId": id_,
            "chain": chain,
            "upiId": parsed.upi_id,
            "payeeName": parsed.payee_name,
            "inrPaise": inr_paise,
            "amountInr": format_paise(inr_paise),
            "lockTx": payment.get("lock_tx"),
        }
        ok, detail, stdout = await self.black_box(self.command, job, self.timeout_sec)
        if ok:
            ref = payout_ref_from_output(stdout, id_)
            self.store.cas_dispatch(id_, "DISPATCHING", "DISPATCHED", detail, payout_ref=ref,
                                    dispatched_at=int(self.clock()))
            log.info("[payout] %s dispatched Rs %s -> %s", id_[:10], job["amountInr"], parsed.upi_id)
            self._publish(id=id_, chain=chain, state="DISPATCHED")
            return "dispatched"

        self.store.cas_dispatch(id_, "DISPATCHING", "FAILED", detail, pending_refund=REASON_DISPATCH_FAILED)
        log.warning("[payout] %s hand-off failed (%s) -> refund DISPATCH_FAILED", id_[:10], detail)
        self._publish(id=id_, chain=chain, state="FAILED", detail=detail)
        await self._refund(chain, id_)
        return "failed"

    async def _refund(self, chain: str, id_: str) -> None:
        try:
            await self.settler.refund(chain, id_, REASON_DISPATCH_FAILED)
        except Exception as e:  # the settler loop retries rows with pending_refund set
            log.warning("[payout] refund %s deferred to settler loop: %s", id_[:10], e)
