"""The event loop against a fake backend and a fake phone."""

from __future__ import annotations

import io
import urllib.error

import pytest

import agent as ag
import payer
from agent import Agent, AgentConfig, BackendRefused, check_api_url

from .test_payer import FakePhone, VPA

A, B = "0x" + "a1" * 32, "0x" + "b2" * 32
NOW = 1_700_000_000.0
VIEW_TOKEN_VECTOR = "d570cbe8bc6adfc240d6e7da47f5856e83789a36600820ee92eb1167c7aa1356"


def locked(id_):
    return {"name": "OfframpLocked", "id": id_, "chain": "arc"}


def payable(id_, locked_at=NOW - 5, paise="1000"):
    return {"id": id_, "chain": "arc", "status": "LOCKED", "lockedAt": str(int(locked_at)), "inrPaise": paise,
            "upiId": VPA, "payeeName": "Chai Point", "qrText": None}


class FakeBackend:
    def __init__(self, events=None, payments=None):
        self.ev, self.payments, self.lookups = events or [], payments or {}, []
        self.events_error = None

    def events(self, limit=100):
        if self.events_error:
            raise self.events_error
        return self.ev

    def payment(self, id_):
        self.lookups.append(id_)
        p = self.payments.get(id_, BackendRefused(404, "NOT_FOUND"))
        if isinstance(p, Exception):
            raise p
        return p


class Clock:
    def __init__(self):
        self.t = NOW

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


@pytest.fixture
def harness(tmp_path):
    def build(backend, phone=None, live=True, **cfg):
        clock, logs = Clock(), []
        phone = phone or FakePhone()
        pc = payer.Config(live=live, db_path=str(tmp_path / "ledger.sqlite"))
        return Agent(backend, AgentConfig(**cfg), pc, phone, clock=clock, sleep=clock.sleep, log=logs.append), phone, logs, clock
    return build


def test_a_locked_event_pays_once_across_ticks(harness):
    agent, phone, logs, _ = harness(FakeBackend([locked(A)], {A: payable(A)}))
    agent.tick()
    agent.tick()
    assert phone.pay_tapped and phone.taps.count(payer.DEFAULT_TAPS["pay"]) == 1
    assert any(l.startswith("PAID") for l in logs)


def test_a_restart_does_not_pay_again(harness, tmp_path):
    backend = FakeBackend([locked(A)], {A: payable(A)})
    first, phone, _, _ = harness(backend)
    first.tick()
    second, again, logs, _ = harness(backend)
    second.tick()
    assert phone.pay_tapped and again.calls == [] and logs == []


def test_other_events_are_ignored(harness):
    agent, phone, _, _ = harness(FakeBackend([{"name": "OfframpCompleted", "id": A}, {"name": "OfframpLocked"}]))
    agent.tick()
    assert agent.backend.lookups == [] and phone.calls == []


def test_history_older_than_the_limit_is_never_paid(harness):
    agent, phone, _, _ = harness(FakeBackend([locked(A)], {A: payable(A, locked_at=NOW - 601)}))
    agent.tick()
    agent.tick()
    assert phone.calls == [] and agent.backend.lookups == [A]


def test_a_lock_just_inside_the_limit_is_paid(harness):
    agent, phone, _, _ = harness(FakeBackend([locked(A)], {A: payable(A, locked_at=NOW - 600)}))
    agent.tick()
    assert phone.pay_tapped


def test_oldest_lock_is_paid_first(harness):
    order = []
    backend = FakeBackend([locked(B), locked(A)], {A: payable(A), B: payable(B)})  # newest first, as the API returns
    agent, _, _, _ = harness(backend, live=False)
    real = payer.run_job
    try:
        payer.run_job = lambda job, *a, **k: (order.append(job["idempotencyKey"]), real(job, *a, **k))[1]
        agent.tick()
    finally:
        payer.run_job = real
    assert order == [A, B]


def test_not_payable_is_final(harness):
    backend = FakeBackend([locked(A)], {A: BackendRefused(409, "NOT_PAYABLE")})
    agent, phone, _, _ = harness(backend)
    agent.tick()
    agent.tick()
    assert phone.calls == [] and backend.lookups == [A]


@pytest.mark.parametrize("code,status", [("NOT_READY", 409), ("NOT_FOUND", 404)])
def test_not_ready_is_retried_then_paid(harness, code, status):
    backend = FakeBackend([locked(A)], {A: BackendRefused(status, code)})
    agent, phone, _, clock = harness(backend)
    agent.tick()
    assert phone.calls == []
    clock.t += 2
    backend.payments[A] = payable(A)
    agent.tick()
    assert phone.pay_tapped


def test_not_ready_forever_gives_up_without_paying(harness):
    backend = FakeBackend([locked(A)], {A: BackendRefused(409, "NOT_READY")})
    agent, phone, logs, clock = harness(backend)
    agent.tick()
    clock.t += 61
    agent.tick()
    n = len(backend.lookups)
    agent.tick()
    assert len(backend.lookups) == n and phone.calls == [] and any("ALERT gave up" in l for l in logs)


def test_bad_token_is_loud_once_and_the_payment_is_not_dropped(harness):
    backend = FakeBackend([locked(A)], {A: BackendRefused(401, "UNAUTHORIZED", "bad token")})
    agent, phone, logs, _ = harness(backend)
    agent.tick()
    agent.tick()
    assert len([l for l in logs if "UNAUTHORIZED" in l]) == 1
    backend.payments[A] = payable(A)  # token fixed
    agent.tick()
    assert phone.pay_tapped


def test_network_errors_never_stop_the_loop_or_drop_a_payment(harness):
    backend = FakeBackend([locked(A)], {A: OSError("connection refused")})
    agent, phone, logs, _ = harness(backend)
    agent.tick()
    backend.events_error = OSError("down")
    agent.tick()
    backend.events_error = None
    backend.payments[A] = payable(A)
    agent.tick()
    assert phone.pay_tapped and len([l for l in logs if "WARNING" in l]) == 2


def test_unknown_and_failed_outcomes_alert(harness):
    agent, _, logs, _ = harness(FakeBackend([locked(A)], {A: payable(A)}), FakePhone(result=None))
    agent.tick()
    assert any("ALERT outcome UNKNOWN" in l and A in l for l in logs)
    agent2, phone2, logs2, _ = harness(FakeBackend([locked(B)], {B: payable(B)}), FakePhone(pay_screen=["other@ybl"]))
    agent2.tick()
    assert not phone2.pay_tapped and any("ALERT NOT PAID" in l for l in logs2)


def test_dry_run_uses_no_phone(harness):
    agent, phone, logs, _ = harness(FakeBackend([locked(A)], {A: payable(A)}), live=False)
    agent.tick()
    assert phone.calls == [] and any("dry run" in l for l in logs)


def test_the_local_caps_apply_whatever_the_backend_says(harness):
    agent, phone, logs, _ = harness(FakeBackend([locked(A)], {A: payable(A, paise="100001")}))
    agent.tick()
    assert phone.calls == [] and any("ALERT NOT PAID" in l for l in logs)


@pytest.mark.parametrize("url,ok", [
    ("https://api.example.com", True), ("https://api.example.com/", True), ("http://10.0.0.5:8788", True),
    ("http://127.0.0.1:8788", True), ("http://localhost:8788", True),
    ("http://api.example.com", False), ("http://8.8.8.8:8788", False), ("ftp://10.0.0.1", False), ("10.0.0.5", False),
])
def test_token_is_only_sent_over_https_or_a_private_address(url, ok):
    if ok:
        assert not check_api_url(url).endswith("/")
    else:
        with pytest.raises(ValueError):
            check_api_url(url)


def test_backend_turns_http_errors_into_api_codes(monkeypatch):
    def refuse(req, timeout):
        assert req.get_header("X-view-token") == ag.derive_view_token("tok", A)
        raise urllib.error.HTTPError(req.full_url, 409, "x", {}, io.BytesIO(b'{"error":"NOT_READY","message":"wait"}'))
    monkeypatch.setattr(ag.urllib.request, "urlopen", refuse)
    with pytest.raises(BackendRefused) as e:
        ag.Backend("https://x.example", "tok").payment(A)
    assert e.value.code == "NOT_READY" and e.value.status == 409


def test_view_token_matches_the_backend():
    # The same vector is in services/api-py/tests/test_api.py, so changing either side fails a test.
    assert ag.derive_view_token("test-secret", "0x" + "ab" * 32) == VIEW_TOKEN_VECTOR


def view(**over):
    v = {"payout": "DISPATCHED", "payee": {"upiId": VPA, "name": "Chai Point"},
         "onchain": {"status": "LOCKED", "lockedAt": "1700000000", "inrPaise": "1000"}}
    v.update(over)
    return v


def test_payment_comes_from_the_view_with_onchain_amount():
    p = ag.payment_from_view(view(inrPaise="999"))
    assert p == {"lockedAt": "1700000000", "inrPaise": "1000", "upiId": VPA, "payeeName": "Chai Point", "qrText": None}


@pytest.mark.parametrize("over,code", [
    ({"onchain": {"status": "NONE"}}, "NOT_READY"),
    ({"onchain": None}, "NOT_READY"),
    ({"payout": "NONE"}, "NOT_READY"),  # lock seen, match not decided
    ({"onchain": {"status": "REFUNDED"}}, "NOT_PAYABLE"),
    ({"onchain": {"status": "DISPUTED"}}, "NOT_PAYABLE"),
    ({"payout": "SKIPPED"}, "NOT_PAYABLE"),  # didn't match its quote
    ({"payout": "FAILED"}, "NOT_PAYABLE"),
    ({"payout": "NEEDS_OPERATOR"}, "NOT_PAYABLE"),
    ({"payee": None}, "NO_PAYEE"),  # wrong secret
])
def test_view_states_that_must_not_pay(over, code):
    with pytest.raises(BackendRefused) as e:
        ag.payment_from_view(view(**over))
    assert e.value.code == code


def test_wrong_secret_alerts_and_pays_nothing(harness):
    backend = FakeBackend([locked(A)], {A: BackendRefused(403, "NO_PAYEE", "is VIEW_TOKEN_SECRET the same")})
    agent, phone, logs, clock = harness(backend)
    agent.tick()
    clock.t += 61
    agent.tick()
    assert phone.calls == [] and any("ALERT" in l and "VIEW_TOKEN_SECRET" in l for l in logs)
