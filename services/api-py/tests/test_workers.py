"""Payout runner, settler, listener matching and keepers, against fake chain/settler objects."""

from __future__ import annotations

import asyncio
import os
import sys
import time

import pytest

from app.chain import DecodedEvent, Deadlines
from app.commitment import hex32, qr_commitment
from app.keepers import Keeper, choose_action
from app.listener import Listener, match_quote
from app.payout_runner import PayoutRunner, payout_ref_from_output, run_black_box
from app.qr import parse_upi_qr
from app.settler import Settler, default_payout_ref

from .conftest import QR, FakeRunner, FakeSettler, store_quote

ID = "0x" + "c1" * 32
STUB = os.path.join(os.path.dirname(__file__), "..", "scripts", "dev_payout_stub.py")


def locked_row(store, chain, user, *, id_=ID, locked_at=None, commitment=None, amount=100666, inr=1000):
    commitment = commitment or hex32(qr_commitment("0x" + "ab" * 32, QR))
    t = chain.now if locked_at is None else locked_at
    chain.lock(id_, user, amount, inr, commitment, locked_at=t)
    store.upsert_locked({"id": id_, "chain": "arc", "user": user, "amount": amount, "inr_paise": inr,
                         "rate_e6": 99338880, "qr_commitment": commitment, "lock_tx": "0x" + "ee" * 32,
                         "lock_block": 10, "locked_at": t, "completable_at": t + 15, "reclaimable_at": t + 86400,
                         "dispute_window": 86400})
    return store.get_payment(id_)


class CountingBox:
    def __init__(self, ok=True, delay=0.05, stdout=""):
        self.calls, self.ok, self.delay, self.stdout = [], ok, delay, stdout

    async def __call__(self, cmd, job, timeout):
        self.calls.append(job)
        await asyncio.sleep(self.delay)
        return (self.ok, "exit 0" if self.ok else "exit 1", self.stdout)


def make_runner(store, settler, box, clock=time.time):
    return PayoutRunner(store, settler, command="unused", timeout_sec=5, max_lock_age_sec=600, black_box=box, clock=clock)


PARSED = parse_upi_qr(QR, 1000)


# ---- payout runner ----------------------------------------------------------------------------


async def test_exactly_once_under_double_dispatch(store, chain, user):
    p = locked_row(store, chain, user.address)
    box = CountingBox()
    runner = make_runner(store, FakeSettler(), box)
    results = await asyncio.gather(*(runner.dispatch(p, PARSED) for _ in range(5)))
    assert sorted(results) == ["dispatched"] + ["skipped"] * 4
    assert len(box.calls) == 1
    assert box.calls[0]["idempotencyKey"] == ID and box.calls[0]["upiId"] == "chaipoint@okaxis"
    row = store.get_payment(ID)
    assert row["dispatch_state"] == "DISPATCHED" and row["payout_ref"] == default_payout_ref(ID)
    assert await runner.dispatch(store.get_payment(ID), PARSED) == "skipped" and len(box.calls) == 1


async def test_crash_in_dispatching_becomes_needs_operator_and_never_reruns(store, chain, user):
    p = locked_row(store, chain, user.address)
    assert store.cas_dispatch(ID, "NONE", "DISPATCHING")  # simulate: written, then the process died
    box = CountingBox()
    runner = make_runner(store, FakeSettler(), box)  # "reboot"
    assert runner.recover() == [ID]
    assert store.get_payment(ID)["dispatch_state"] == "NEEDS_OPERATOR"
    assert await runner.dispatch(p, PARSED) == "skipped"
    assert box.calls == []
    settler = Settler({"arc": chain}, store, object())
    await settler.tick()  # NEEDS_OPERATOR is neither completed nor refunded automatically
    assert chain.sent == []


async def test_failure_refunds_reason_3(store, chain, user, keys):
    p = locked_row(store, chain, user.address)
    settler = Settler({"arc": chain}, store, keys.settler)
    runner = make_runner(store, settler, CountingBox(ok=False))
    assert await runner.dispatch(p, PARSED) == "failed"
    assert chain.sent == [("refundOfframp", [bytes.fromhex(ID[2:]), 3])]
    row = store.get_payment(ID)
    assert row["dispatch_state"] == "FAILED" and row["status"] == "REFUNDED" and row["refund_reason"] == 3
    assert row["pending_refund"] is None


async def test_refund_retried_by_settler_loop_when_first_attempt_fails(store, chain, user, keys):
    from app.chain import ChainError
    p = locked_row(store, chain, user.address)
    settler = Settler({"arc": chain}, store, keys.settler)
    chain.fail["refundOfframp"] = ChainError("refundOfframp", "RpcDown")
    runner = make_runner(store, settler, CountingBox(ok=False))
    assert await runner.dispatch(p, PARSED) == "failed"
    assert store.get_payment(ID)["pending_refund"] == 3 and chain.sent == []
    del chain.fail["refundOfframp"]
    await settler.tick()
    assert chain.sent == [("refundOfframp", [bytes.fromhex(ID[2:]), 3])]
    await settler.tick()  # idempotent: status is now REFUNDED
    assert len(chain.sent) == 1


async def test_too_late_lock_is_not_paid_and_refunds_3(store, chain, user, keys):
    p = locked_row(store, chain, user.address, locked_at=int(time.time()) - 601)
    box = CountingBox()
    runner = make_runner(store, Settler({"arc": chain}, store, keys.settler), box)
    assert await runner.dispatch(p, PARSED) == "too_late"
    assert box.calls == [] and chain.sent[0][0] == "refundOfframp" and chain.sent[0][1][1] == 3


async def test_completes_only_after_completable_at(store, chain, user, keys):
    p = locked_row(store, chain, user.address)
    settler = Settler({"arc": chain}, store, keys.settler)
    await make_runner(store, settler, CountingBox()).dispatch(p, PARSED)
    chain.now = p["locked_at"] + 14
    assert (await settler.tick())[ID] == "too_early" and chain.sent == []
    chain.now = p["locked_at"] + 15
    assert (await settler.tick())[ID] == "completed"
    fn, args = chain.sent[0]
    assert fn == "completeOfframp" and hex32(args[1]) == default_payout_ref(ID) and int(hex32(args[1]), 16) != 0
    assert store.get_payment(ID)["status"] == "COMPLETED"
    await settler.tick()
    assert len(chain.sent) == 1  # no second completion


async def test_complete_refused_after_reclaimable_at(store, chain, user, keys):
    p = locked_row(store, chain, user.address)
    store.cas_dispatch(ID, "NONE", "DISPATCHED")
    chain.now = p["locked_at"] + 86400
    assert await Settler({"arc": chain}, store, keys.settler).complete("arc", ID) == "window_closed"
    assert chain.sent == []


async def test_alert_logged_after_one_hour(store, chain, user, keys, caplog):
    p = locked_row(store, chain, user.address)
    store.cas_dispatch(ID, "NONE", "DISPATCHED", dispatched_at=int(time.time()) - 3601)
    chain.now = p["locked_at"] + 5  # still too early -> not completed
    await Settler({"arc": chain}, store, keys.settler).tick()
    assert "ALERT" in caplog.text and store.get_payment(ID)["alerted"] == 1


async def test_real_subprocess_stub_success_and_failure(monkeypatch):
    cmd = f"{sys.executable} {STUB}"
    job = {"idempotencyKey": ID, "upiId": "chaipoint@okaxis", "payeeName": "Chai Point", "amountInr": "10.00"}
    ok, detail, out = await run_black_box(cmd, job, 10)
    assert ok and "pay" in out and "chaipoint@okaxis" in out
    monkeypatch.setenv("STUB_FAIL", "1")
    ok, detail, _ = await run_black_box(cmd, job, 10)
    assert not ok and detail.startswith("exit 1")
    ok, detail, _ = await run_black_box(f"{sys.executable} -c 'import time; time.sleep(5)'", job, 0.3)
    assert not ok and "timeout" in detail


def test_payout_ref_from_utr():
    from eth_utils import keccak
    assert payout_ref_from_output('noise\n{"utr": "UTR123"}\n', ID) == hex32(keccak(text="UTR123"))
    assert payout_ref_from_output("pay Rs 10 to x", ID) == default_payout_ref(ID)


# ---- listener ---------------------------------------------------------------------------------


def lock_event(id_, user, *, commitment, amount=100666, inr=1000, rate=99338880, block=50):
    return DecodedEvent("OfframpLocked", {"id": id_, "user": user, "amount": amount, "inrPaise": inr, "rateE6": rate,
                                          "qrCommitment": commitment}, "0x" + "ee" * 32, 0, block)


def make_listener(store, chain):
    s, r = FakeSettler(), FakeRunner()
    return Listener("arc", chain, store, s, r, inline_dispatch=True), s, r


async def test_listener_matching_quote_dispatches(store, chain, user):
    q = store_quote(store, id_=ID, user=user.address)
    lst, s, r = make_listener(store, chain)
    await lst.handle(lock_event(ID, user.address, commitment=q["qr_commitment"]))
    assert s.refunds == [] and len(r.dispatched) == 1
    assert r.dispatched[0][1].upi_id == "chaipoint@okaxis"
    assert store.get_payment(ID)["match_ok"] == 1 and store.get_quote(ID)["status"] == "USED"
    await lst.handle(lock_event(ID, user.address, commitment=q["qr_commitment"]))  # replayed log
    assert len(r.dispatched) == 1


async def test_listener_commitment_mismatch_refunds_4(store, chain, user):
    store_quote(store, id_=ID, user=user.address)
    lst, s, r = make_listener(store, chain)
    other = hex32(qr_commitment("0x" + "ab" * 32, "upi://pay?pa=attacker@okaxis"))
    await lst.handle(lock_event(ID, user.address, commitment=other))
    assert s.refunds == [("arc", ID, 4)] and r.dispatched == []
    row = store.get_payment(ID)
    assert row["match_ok"] == 0 and "qrCommitment" in row["match_detail"] and row["dispatch_state"] == "SKIPPED"


async def test_listener_tampered_stored_qr_refunds_4(store, chain, user):
    q = store_quote(store, id_=ID, user=user.address)
    store._exec("UPDATE quotes SET qr_text = ? WHERE id = ?", ("upi://pay?pa=attacker@okaxis", ID))
    lst, s, r = make_listener(store, chain)
    await lst.handle(lock_event(ID, user.address, commitment=q["qr_commitment"]))
    assert s.refunds == [("arc", ID, 4)] and r.dispatched == []
    assert "recomputed" in store.get_payment(ID)["match_detail"]


async def test_listener_unknown_quote_refunds_4(store, chain, user):
    lst, s, r = make_listener(store, chain)
    await lst.handle(lock_event(ID, user.address, commitment="0x" + "99" * 32))
    assert s.refunds == [("arc", ID, 4)] and r.dispatched == []


@pytest.mark.parametrize("field,value", [("amount", 100667), ("inr", 1001), ("rate", 1), ("user", "0x" + "12" * 20)])
async def test_listener_field_mismatch_refunds_4(store, chain, user, field, value):
    q = store_quote(store, id_=ID, user=user.address)
    lst, s, r = make_listener(store, chain)
    kw = {"amount": 100666, "inr": 1000, "rate": 99338880}
    u = user.address
    if field == "user":
        u = value
    else:
        kw[field] = value
    await lst.handle(lock_event(ID, u, commitment=q["qr_commitment"], **kw))
    assert s.refunds == [("arc", ID, 4)] and r.dispatched == []


async def test_listener_amount_mismatch_in_qr_refunds_2(store, chain, user):
    qr = "upi://pay?pa=shop@ybl&am=20.00&cu=INR"
    q = store_quote(store, id_=ID, user=user.address, qr_text=qr)
    lst, s, r = make_listener(store, chain)
    await lst.handle(lock_event(ID, user.address, commitment=q["qr_commitment"]))
    assert s.refunds == [("arc", ID, 2)] and r.dispatched == []


def test_match_quote_pure():
    q = {"id": ID, "chain": "arc", "user": "0x" + "aa" * 20, "amount": "5", "inr_paise": 1, "rate_e6": "2",
         "salt": "0x" + "ab" * 32, "qr_text": QR, "qr_commitment": hex32(qr_commitment("0x" + "ab" * 32, QR))}
    ok = dict(chain="arc", id_=ID, user="0x" + "AA" * 20, amount=5, inr_paise=1, rate_e6=2,
              commitment=q["qr_commitment"])
    assert match_quote(q, **ok) == []
    assert match_quote(q, **{**ok, "chain": "whitechain"}) == ["chain (quote arc)"]
    assert match_quote(None, **ok)


async def test_listener_poll_uses_cursor_and_small_ranges(store, chain, user):
    chain.head = 4501
    lst, s, r = make_listener(store, chain)
    lst.confirmations = 1
    await lst.poll()
    assert chain.get_logs_calls == [(1, 2000), (2001, 4000), (4001, 4500)]
    assert store.get_cursor("arc") == 4500
    chain.get_logs_calls.clear()
    await lst.poll()
    assert chain.get_logs_calls == []  # nothing new


async def test_listener_mirrors_status_events_including_disputed_deadline(store, chain, user):
    q = store_quote(store, id_=ID, user=user.address)
    lst, s, r = make_listener(store, chain)
    await lst.handle(lock_event(ID, user.address, commitment=q["qr_commitment"]))
    # Replaying history: by now the onchain slot already holds the dispute deadline, so the
    # completion handler must compute releasableAt (= completedAt + disputeWindow), not read it.
    chain.dls[ID] = Deadlines(1, 2, 86400, chain.now + 7 * 86400)
    await lst.handle(DecodedEvent("OfframpCompleted", {"id": ID, "inrPaise": 1000, "payoutRef": "0x" + "01" * 32},
                                  "0x" + "c0" * 32, 1, 51))
    row = store.get_payment(ID)
    assert row["status"] == "COMPLETED" and row["releasable_at"] == chain.now + 86400
    await lst.handle(DecodedEvent("OfframpDisputed", {"id": ID, "user": user.address, "amount": 100666},
                                  "0x" + "d0" * 32, 2, 52))
    row = store.get_payment(ID)
    assert row["status"] == "DISPUTED" and row["dispute_deadline"] == chain.now + 7 * 86400
    assert row["releasable_at"] == chain.now + 86400  # window end untouched by the dispute
    await lst.handle(DecodedEvent("DisputeResolved", {"id": ID, "refundedUser": True, "amount": 100666},
                                  "0x" + "d1" * 32, 3, 53))
    assert store.get_payment(ID)["status"] == "REVERSED"
    assert len(store.list_events(ref_id=ID)) == 4


# ---- keepers ----------------------------------------------------------------------------------


@pytest.mark.parametrize("status,dl,now,want", [
    (1, Deadlines(15, 100, 86400, 0), 99, None),
    (1, Deadlines(15, 100, 86400, 0), 100, "reclaim"),
    (2, Deadlines(15, 100, 86400, 500), 499, None),
    (2, Deadlines(15, 100, 86400, 500), 500, "release"),
    (6, Deadlines(15, 100, 86400, 900), 899, None),       # DISPUTED: releasableAt = resolve deadline
    (6, Deadlines(15, 100, 86400, 900), 900, "claim_timeout"),
    (6, Deadlines(15, 100, 86400, 900), 500, None),       # past the old window end, before resolve deadline
    (3, Deadlines(15, 100, 86400, 0), 10**10, None),
    (4, Deadlines(15, 100, 86400, 0), 10**10, None),
    (5, Deadlines(15, 100, 86400, 0), 10**10, None),
    (0, Deadlines(0, 0, 0, 0), 10**10, None),
])
def test_choose_action(status, dl, now, want):
    assert choose_action(status, dl, now) == want


async def test_keeper_tick_reads_onchain_and_acts(store, chain, user, keys):
    t = chain.now
    ids = {s: "0x" + f"{i:02x}" * 32 for i, s in enumerate(["reclaim", "release", "claim", "early"], 1)}
    for name, id_ in ids.items():
        store.ensure_payment(id_, "arc")
    chain.lock(ids["reclaim"], user.address, 1, 1, "0x" + "01" * 32, locked_at=t - 86400)
    chain.lock(ids["release"], user.address, 1, 1, "0x" + "01" * 32, status=2)
    chain.dls[ids["release"]].releasable_at = t - 1
    chain.lock(ids["claim"], user.address, 1, 1, "0x" + "01" * 32, status=6)
    chain.dls[ids["claim"]].releasable_at = t  # resolve deadline reached
    chain.lock(ids["early"], user.address, 1, 1, "0x" + "01" * 32, status=6)
    chain.dls[ids["early"]].releasable_at = t + 3600
    for name, id_ in ids.items():
        store.update_payment(id_, status={"reclaim": "LOCKED", "release": "COMPLETED"}.get(name, "DISPUTED"))
    out = await Keeper({"arc": chain}, store, keys.paymaster).tick()
    assert out == {ids["reclaim"]: "reclaim", ids["release"]: "release", ids["claim"]: "claim_timeout",
                   ids["early"]: None}
    assert sorted(fn for fn, _ in chain.sent) == ["claimDisputeTimeout", "reclaimOfframp", "releaseOfframp"]
    assert store.get_payment(ids["reclaim"])["status"] == "REFUNDED"
    assert store.get_payment(ids["claim"])["status"] == "REVERSED"
    # the not-yet-due dispute got its resolve deadline mirrored (own column), so the next tick skips it
    assert store.get_payment(ids["early"])["dispute_deadline"] == t + 3600
    assert store.get_payment(ids["early"])["releasable_at"] is None
    chain.sent.clear()
    assert await Keeper({"arc": chain}, store, keys.paymaster).tick() == {}


# ---- settler: one action per offramp at a time (regression: double refund race) ---------------

from dataclasses import dataclass, field  # noqa: E402

from .conftest import FakeChain  # noqa: E402


@dataclass
class InFlightChain(FakeChain):
    """A send takes a moment to land, so the chain still shows LOCKED while the tx is in flight."""

    attempts: list = field(default_factory=list)

    async def send(self, account, fn, args):
        self.attempts.append(fn)
        await asyncio.sleep(0.05)
        return await super().send(account, fn, args)


async def test_direct_refund_and_settler_loop_never_double_send(store, user, keys):
    chain = InFlightChain()
    locked_row(store, chain, user.address)
    settler = Settler({"arc": chain}, store, keys.settler)
    direct = asyncio.create_task(settler.refund("arc", ID, 3))   # e.g. the payout runner after a failed payout
    await asyncio.sleep(0.01)                                     # pending_refund is set, refund tx in flight
    await settler.tick()                                          # background loop runs mid-flight
    assert await direct == "refunded"
    await settler.tick()                                          # and again after it landed
    assert chain.attempts == ["refundOfframp"], chain.attempts    # exactly one send attempt
    row = store.get_payment(ID)
    assert row["status"] == "REFUNDED" and row["pending_refund"] is None


async def test_direct_complete_and_settler_loop_never_double_send(store, user, keys):
    chain = InFlightChain()
    locked_row(store, chain, user.address, locked_at=chain.now - 20)   # already past completableAt
    store.update_payment(ID, dispatch_state="DISPATCHED")
    settler = Settler({"arc": chain}, store, keys.settler)
    direct = asyncio.create_task(settler.complete("arc", ID))
    await asyncio.sleep(0.01)
    await settler.tick()
    assert await direct == "completed"
    await settler.tick()
    assert chain.attempts == ["completeOfframp"], chain.attempts
