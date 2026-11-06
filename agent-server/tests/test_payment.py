"""The payment path with a fake backend. No network, wallet or phone."""

from __future__ import annotations

import copy
import json
import subprocess
from pathlib import Path

import pytest

from app.payment.flow import NO_PERMIT, Observation, PaymentFlow
from app.payment.qr import QrError, decode_screenshot, find_payment, find_total, parse_upi
from app.payment.verify import Constraints, origin_allowed
from app.signers.circle_cli import CircleCliSigner, CircleSignerError
from app.signers.local_key import LocalKeySigner

ROOT = Path(__file__).resolve().parents[2]
QR_IMAGE = (ROOT / "apps/mock-store/upi.png").read_bytes()
QR_TEXT = "upi://pay?pa=vatsalgandhi239@okicici&pn=Vatsal%20Gandhi&aid=uGICAgMDu9M3OUg"
PAYEE = "vatsalgandhi239@okicici"
WALLET = "0x179dfbc07b23541849a2cb6d38d991d9ec7cda88"
VAULT = "0x878734374e6131FCeEEB83FA8605Ab49f4D21836"
TOKEN = "0x3600000000000000000000000000000000000000"
NOW = 1_800_000_000
PAGE = "Pay with UPI\nAmount to pay\n₹1\nScan with any UPI app and enter the amount shown."
URL = "http://127.0.0.1:5180/"
CHAIN = {"chainId": 5042002, "vault": VAULT, "token": {"address": TOKEN},
         "vaultDomain": {"name": "ScanPayVault", "version": "1", "chainId": 5042002, "verifyingContract": VAULT}}


def make_quote(wallet=WALLET, inr="100", payee=PAYEE, **over):
    intent = {"id": "0x" + "7a" * 32, "user": wallet, "amount": "10106", "inrPaise": inr, "rateE6": "98960000",
              "qrCommitment": "0x" + "ab" * 32, "deadline": str(NOW + 90)}
    q = {
        "id": intent["id"], "intent": intent, "viewToken": "vt", "payee": {"upiId": payee, "name": "Vatsal Gandhi"},
        "display": {"amount": "0.010106", "symbol": "USDC", "rate": "98.96", "marketRate": "99.76"},
        "signing": {
            "intent": {"domain": dict(CHAIN["vaultDomain"]), "primaryType": "OfframpIntent", "message": dict(intent),
                       "types": {"EIP712Domain": [{"name": "name", "type": "string"}, {"name": "version", "type": "string"}, {"name": "chainId", "type": "uint256"}, {"name": "verifyingContract", "type": "address"}],
                                 "OfframpIntent": [{"name": "id", "type": "bytes32"}, {"name": "user", "type": "address"}, {"name": "amount", "type": "uint256"}, {"name": "inrPaise", "type": "uint64"}, {"name": "rateE6", "type": "uint64"}, {"name": "qrCommitment", "type": "bytes32"}, {"name": "deadline", "type": "uint64"}]}},
            "permit": {"domain": {"name": "USDC", "version": "2", "chainId": 5042002, "verifyingContract": TOKEN}, "primaryType": "Permit",
                       "types": {"EIP712Domain": [{"name": "name", "type": "string"}, {"name": "version", "type": "string"}, {"name": "chainId", "type": "uint256"}, {"name": "verifyingContract", "type": "address"}],
                                 "Permit": [{"name": "owner", "type": "address"}, {"name": "spender", "type": "address"}, {"name": "value", "type": "uint256"}, {"name": "nonce", "type": "uint256"}, {"name": "deadline", "type": "uint256"}]},
                       "message": {"owner": wallet, "spender": VAULT, "value": "10106", "nonce": "0", "deadline": str(NOW + 90)}},
        },
    }
    q.update(over)
    return q


class FakeBackend:
    def __init__(self, quote=None, balance="20000000", allowance="5000000", statuses=("LOCKED", "COMPLETED")):
        self.quote_body = quote or make_quote()
        self.balance, self.allowance, self.statuses = balance, allowance, list(statuses)
        self.quotes, self.submits, self.polls = [], [], []

    def config(self):
        return {"chains": {"arc": CHAIN}}

    def account(self, chain, address):
        return {"balance": self.balance, "allowance": self.allowance}

    def quote(self, chain, user, inr, qr_text):
        self.quotes.append((chain, user, inr, qr_text))
        return self.quote_body

    def submit(self, chain, intent, user_sig, permit):
        self.submits.append((intent, user_sig, permit))
        return {"txHash": "0xabc"}

    def status(self, id_, view_token=None):
        self.polls.append(view_token)
        return {"status": self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]}


class FakeSigner:
    uses_allowance = True

    def __init__(self, wallet=WALLET):
        self.wallet, self.signed = wallet, []

    def address(self):
        return self.wallet

    def sign_typed_data(self, typed):
        self.signed.append(typed["primaryType"])
        return "0x" + "ab" * 32 + "cd" * 32 + "1b"


def run(backend=None, signer=None, observation=None, max_paise=100, domains=("127.0.0.1",), **kw):
    backend, signer = backend or FakeBackend(), signer or FakeSigner()
    flow = PaymentFlow(backend, signer, "arc", Constraints(max_paise, list(domains)), clock=lambda: NOW, sleep=lambda _: None, **kw)
    result = flow.pay(observation or Observation(URL, PAGE, QR_IMAGE))
    return result, backend, signer, flow


# ---- reading the page ---------------------------------------------------------------------------


def test_reads_the_real_qr_image_with_its_logo():
    assert decode_screenshot(QR_IMAGE) == {QR_TEXT}


def test_static_qr_has_no_amount_like_a_real_merchant():
    payment = parse_upi(QR_TEXT)
    assert payment.upi_id == PAYEE and payment.name == "Vatsal Gandhi" and payment.amount_paise is None


@pytest.mark.parametrize("text,paise", [
    ("Amount to pay\n₹1\n", 100), ("Total ₹1,250.50", 125050), ("Grand total: Rs. 300", 30000), ("TO PAY ₹ 12.5", 1250),
    ("Order total INR 99", 9900), ("Total\n₹20\nAmount to pay\n₹20", 2000),
])
def test_finds_the_total(text, paise):
    assert find_total(text) == paise


@pytest.mark.parametrize("text", ["No price here", "Masala chai · ₹20", "Total ₹20\nAmount to pay ₹25", ""])
def test_no_clear_total_means_none(text):
    assert find_total(text) is None


def test_exact_link_on_the_page_is_used_without_a_screenshot():
    assert find_payment(f"Pay here: {QR_TEXT} now", None).qr_text == QR_TEXT


@pytest.mark.parametrize("text,screenshot", [("nothing", None), ("nothing", b"not an image"), ("nothing", b"")])
def test_no_qr_is_refused(text, screenshot):
    with pytest.raises(QrError):
        find_payment(text, screenshot)


def test_a_link_and_a_qr_for_different_payees_are_refused():
    with pytest.raises(QrError, match="more than one"):
        find_payment("upi://pay?pa=evil@ybl", QR_IMAGE)


@pytest.mark.parametrize("text", ["https://x.example", "upi://pay?pn=x", "upi://pay?pa=bad", "upi://pay?pa=ab@ybl&cu=USD", "upi://pay?pa=ab@ybl&am=1.234"])
def test_bad_upi_links_are_refused(text):
    with pytest.raises(QrError):
        parse_upi(text)


def test_first_payee_wins_as_in_the_backend():
    assert parse_upi("upi://pay?pa=first@ybl&pa=evil@ybl").upi_id == "first@ybl"


@pytest.mark.parametrize("url,ok", [
    ("http://127.0.0.1:5180/", True), ("https://store.example/x", True), ("https://shop.store.example/", True),
    ("https://store.example.evil.io/", False), ("https://notstore.example/", False), ("https://evil.io/store.example", False),
])
def test_origin_check(url, ok):
    assert origin_allowed(url, ["127.0.0.1", "store.example"]) is ok


# ---- the happy path ------------------------------------------------------------------------------


def test_pays_from_the_screenshot_and_the_page_total():
    result, backend, signer, _ = run()
    assert result.status == "COMPLETED" and result.payee == PAYEE and result.amount_paise == 100
    assert backend.quotes == [("arc", WALLET, "1.00", QR_TEXT)]  # the raw QR text, unchanged
    assert signer.signed == ["OfframpIntent"]  # the Circle wallet doesn't sign a permit
    assert backend.polls[0] == "vt"


def test_circle_wallet_sends_the_invalid_permit_so_the_allowance_stays():
    _, backend, _, _ = run()
    _, _, permit = backend.submits[0]
    assert permit == {**NO_PERMIT, "deadline": str(NOW + 90)}


def test_a_local_key_signs_a_real_permit():
    key = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"  # Foundry's public test key 0
    signer = LocalKeySigner(key)
    result, backend, _, _ = run(FakeBackend(make_quote(wallet=signer.address())), signer)
    assert result.status == "COMPLETED"
    _, user_sig, permit = backend.submits[0]
    assert len(user_sig) == 132 and permit["v"] in (27, 28) and permit["r"] != NO_PERMIT["r"]


# ---- every check stops the payment before signing ----------------------------------------------


def refused(result, backend, signer, text):
    assert result.status == "REFUSED" and text in result.detail, result.detail
    assert backend.submits == [] and signer.signed == []


def tamper(path, value):
    q = make_quote()
    target = q
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    return q


@pytest.mark.parametrize("quote,text", [
    (make_quote(payee="evil@ybl"), "quote pays evil@ybl"),
    (tamper(["intent", "inrPaise"], "90"), "quote is for 90"),
    (tamper(["intent", "user"], "0x" + "11" * 20), "isn't for this wallet"),
    (tamper(["signing", "permit", "message", "owner"], "0x" + "11" * 20), "permit isn't for this wallet"),
    (tamper(["signing", "permit", "message", "spender"], "0x" + "11" * 20), "spender isn't the vault"),
    (tamper(["signing", "permit", "message", "value"], "999"), "permit's value differs"),
    (tamper(["signing", "permit", "domain", "chainId"], 1), "permit's domain"),
    (tamper(["signing", "permit", "domain", "verifyingContract"], "0x" + "11" * 20), "permit's domain"),
    (tamper(["signing", "intent", "domain", "chainId"], 1), "intent's domain"),
    (tamper(["signing", "intent", "domain", "verifyingContract"], "0x" + "11" * 20), "intent's domain"),
    (tamper(["signing", "intent", "domain", "name"], "Other"), "name or version"),
    (tamper(["signing", "intent", "message", "amount"], "999999"), "differs from the intent"),
    (tamper(["intent", "deadline"], str(NOW + 5)), "about to expire"),
    (tamper(["signing", "permit", "message", "deadline"], str(NOW + 5)), "permit is about to expire"),
    (tamper(["display", "rate"], "50"), "too far from market"),
])
def test_a_bad_quote_is_never_signed(quote, text):
    refused(*run(FakeBackend(quote))[:3], text)


@pytest.mark.parametrize("observation,text", [
    (Observation(URL, "Pay with UPI", QR_IMAGE), "one clear total"),
    (Observation(URL, "Amount to pay ₹2", QR_IMAGE), "over the 100 paise limit"),
    (Observation("https://evil.example/", PAGE, QR_IMAGE), "not in the allowed domains"),
    (Observation(URL, PAGE, None), "no UPI QR"),
])
def test_a_bad_page_never_reaches_a_quote(observation, text):
    result, backend, signer, _ = run(observation=observation)
    refused(result, backend, signer, text)
    assert backend.quotes == []


def test_a_qr_amount_must_equal_the_page_total():
    page = "Amount to pay ₹1\nupi://pay?pa=ab@ybl&am=2.00"
    result, backend, signer, _ = run(observation=Observation(URL, page, None), max_paise=1000)
    refused(result, backend, signer, "QR asks for 200")


@pytest.mark.parametrize("over,text", [({"balance": "5"}, "balance"), ({"allowance": "5"}, "allowance")])
def test_too_little_balance_or_allowance_stops_before_signing(over, text):
    refused(*run(FakeBackend(**over))[:3], text)


def test_a_signing_wallet_doesnt_need_an_allowance():
    key = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"
    signer = LocalKeySigner(key)
    result, *_ = run(FakeBackend(make_quote(wallet=signer.address()), allowance="0"), signer)
    assert result.status == "COMPLETED"


# ---- stopping and one payment per session -------------------------------------------------------


def test_kill_switch_at_each_point_before_submitting():
    for stop_at in range(1, 5):
        calls = {"n": 0}

        def stop():
            calls["n"] += 1
            return calls["n"] >= stop_at
        result, backend, _, _ = run(should_stop=stop)
        assert result.status == "STOPPED" and backend.submits == []


def test_only_one_payment_per_session():
    result, backend, signer, flow = run()
    assert result.status == "COMPLETED"
    again = flow.pay(Observation(URL, PAGE, QR_IMAGE))
    assert again.status == "REFUSED" and "one payment" in again.detail and len(backend.submits) == 1


def test_a_failed_submit_still_counts_as_the_sessions_payment():
    class Failing(FakeBackend):
        def submit(self, *a):
            self.submits.append(a)
            raise RuntimeError("network died, the payment may have gone through")
    backend = Failing()
    flow = PaymentFlow(backend, FakeSigner(), "arc", Constraints(100, ["127.0.0.1"]), clock=lambda: NOW, sleep=lambda _: None)
    with pytest.raises(RuntimeError):
        flow.pay(Observation(URL, PAGE, QR_IMAGE))
    assert flow.pay(Observation(URL, PAGE, QR_IMAGE)).status == "REFUSED" and len(backend.submits) == 1


# ---- polling --------------------------------------------------------------------------------------


@pytest.mark.parametrize("final", ["REFUNDED", "REVERSED", "DISPUTED"])
def test_failure_statuses_are_reported(final):
    assert run(FakeBackend(statuses=["LOCKED", final]))[0].status == final


def test_poll_timeout_is_reported_with_the_last_status():
    now = {"t": float(NOW)}

    def tick(s):
        now["t"] += s
    backend = FakeBackend(statuses=["LOCKED"])
    flow = PaymentFlow(backend, FakeSigner(), "arc", Constraints(100, ["127.0.0.1"]), clock=lambda: now["t"], sleep=tick, poll_timeout_sec=9)
    assert flow.pay(Observation(URL, PAGE, QR_IMAGE)).status == "TIMED_OUT_LOCKED"


# ---- Circle CLI ------------------------------------------------------------------------------------


def fake_cli(stdout="", code=0):
    calls = []

    def run_cli(args, **kw):
        calls.append(args)
        return subprocess.CompletedProcess(args, code, stdout, "")
    return run_cli, calls


GOOD = "0x" + "ab" * 65


@pytest.mark.parametrize("stdout", [json.dumps({"signature": GOOD}), json.dumps({"data": {"signature": GOOD}}), "Warning: careful\n" + json.dumps({"signature": GOOD})])
def test_circle_output_forms(stdout):
    run_cli, calls = fake_cli(stdout)
    assert CircleCliSigner(WALLET, run=run_cli).sign_typed_data({"a": 1}) == GOOD
    assert calls[0][:4] == ["circle", "wallet", "sign", "typed-data"] and "ARC-TESTNET" in calls[0]


@pytest.mark.parametrize("stdout,code,text", [
    (json.dumps({"error": {"message": "This wallet isn't deployed"}}), 1, "isn't deployed"),
    ("not json", 0, "no valid signature"),
    (json.dumps({"signature": "0x12"}), 0, "no valid signature"),
])
def test_circle_failures_raise(stdout, code, text):
    run_cli, _ = fake_cli(stdout, code)
    with pytest.raises(CircleSignerError, match=text):
        CircleCliSigner(WALLET, run=run_cli).sign_typed_data({})


def test_quote_helpers_arent_shared_mutable():
    a = make_quote()
    b = copy.deepcopy(a)
    a["intent"]["amount"] = "1"
    assert b["intent"]["amount"] == "10106"
