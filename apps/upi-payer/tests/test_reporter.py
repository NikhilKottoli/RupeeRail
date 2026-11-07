"""Reporting results to a hosted agent server, against a fake server and a real ledger file."""

from __future__ import annotations

import pytest

import payer
import reporter
from reporter import Reporter

A, B = "0x" + "a1" * 32, "0x" + "b2" * 32
NOW = 1_700_000_000.0
URL = "https://example.test/agent-api/payer-results"


def setup(tmp_path, post):
    path = str(tmp_path / "payer.sqlite")
    return payer.Ledger(path, clock=lambda: NOW), Reporter(path, URL, "secret", post=post, clock=lambda: NOW + 1)


def test_sends_only_the_id_and_status_of_finished_payments_once(tmp_path):
    sent = []
    ledger, report = setup(tmp_path, lambda url, token, body: sent.append((url, token, body)))
    ledger.reserve(A, "someone@ybl", 100, 100_000)
    ledger.reserve(B, "other@ybl", 100, 100_000)
    ledger.finish(A, "paid", "done", "UTR123")

    assert report.once() == 1  # B is still running
    assert sent == [(URL, "secret", {"key": A, "status": "paid"})]
    assert report.once() == 0


def test_a_settled_result_is_sent_again(tmp_path):
    sent = []
    ledger, report = setup(tmp_path, lambda url, token, body: sent.append(body["status"]))
    ledger.reserve(A, "someone@ybl", 100, 100_000)
    ledger.finish(A, "unknown", "unclear")
    report.once()
    ledger.finish(A, "paid", "settled")
    report.once()
    assert sent == ["unknown", "paid"]


def test_a_failed_send_is_retried(tmp_path):
    calls = {"fail": True}
    sent = []

    def post(url, token, body):
        if calls["fail"]:
            raise OSError("server restarting")
        sent.append(body["key"])

    ledger, report = setup(tmp_path, post)
    ledger.reserve(A, "someone@ybl", 100, 100_000)
    ledger.finish(A, "paid", "done")
    with pytest.raises(OSError):
        report.once()
    calls["fail"] = False
    assert report.once() == 1
    assert sent == [A]


def test_old_payments_are_left_alone(tmp_path):
    sent = []
    path = str(tmp_path / "payer.sqlite")
    ledger = payer.Ledger(path, clock=lambda: NOW)
    ledger.reserve(A, "someone@ybl", 100, 100_000)
    ledger.finish(A, "paid", "done")
    report = Reporter(path, URL, "secret", post=lambda url, token, body: sent.append(body), clock=lambda: NOW + reporter.RECENT_SEC + 1)
    assert report.once() == 0


def test_no_ledger_yet_is_not_an_error(tmp_path):
    report = Reporter(str(tmp_path / "missing.sqlite"), URL, "secret", post=lambda *_: None)
    assert report.once() == 0


def test_it_never_writes_to_the_ledger(tmp_path):
    ledger, report = setup(tmp_path, lambda *_: None)
    ledger.reserve(A, "someone@ybl", 100, 100_000)
    ledger.finish(A, "paid", "done", "UTR123")
    report.once()
    assert ledger.get(A) == {"status": "paid", "detail": "done", "utr": "UTR123"}


@pytest.mark.parametrize("env", [{}, {"AGENT_RESULTS_URL": URL}, {"AGENT_RESULTS_TOKEN": "secret"}])
def test_it_needs_both_settings(monkeypatch, env):
    monkeypatch.delenv("AGENT_RESULTS_URL", raising=False)
    monkeypatch.delenv("AGENT_RESULTS_TOKEN", raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    with pytest.raises(ValueError):
        reporter.from_env("unused")


def test_the_token_never_crosses_the_internet_unencrypted(monkeypatch):
    monkeypatch.setenv("AGENT_RESULTS_URL", "http://example.test/payer-results")
    monkeypatch.setenv("AGENT_RESULTS_TOKEN", "secret")
    with pytest.raises(ValueError):
        reporter.from_env("unused")
