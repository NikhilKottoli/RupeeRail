"""Razorpay collect rail: payment link per onramp, signed callback + webhook, pay only on a verified link."""

from __future__ import annotations

import json

import httpx
import pytest
from fastapi.testclient import TestClient

from app.chain import ChainError
from app.context import build_context
from app.main import create_app
from app.onramp import check_paid_link
from app.razorpay import (RazorpayClient, RazorpayCreds, RazorpayError, callback_message, hmac_hex,
                          verify_callback_signature, verify_webhook_signature)

from .conftest import ARC

CREDS = RazorpayCreds("rzp_test_abc", "key-secret", "hook-secret")
BASE = "https://example.ngrok-free.app"


class FakeRazorpay:
    """Stands in for RazorpayClient: remembers links, lets a test mark them paid/expired."""

    def __init__(self) -> None:
        self.creds = CREDS
        self.links: dict[str, dict] = {}
        self.created: list[dict] = []
        self.fail_create = self.fail_fetch = False

    async def create_payment_link(self, **kw) -> dict:
        if self.fail_create:
            raise RazorpayError("POST /payment_links: HTTP 500")
        self.created.append(kw)
        link_id = f"plink_T{len(self.links) + 1:05d}"
        self.links[link_id] = {"id": link_id, "reference_id": kw["reference_id"], "amount": kw["amount_paise"],
                               "amount_paid": 0, "currency": "INR", "status": "created", "expire_by": kw["expire_by"],
                               "short_url": f"https://rzp.io/rzp/{link_id}", "payments": None}
        return dict(self.links[link_id])

    async def fetch_payment_link(self, link_id: str) -> dict:
        if self.fail_fetch:
            raise RazorpayError("GET /payment_links: ConnectError")
        return dict(self.links[link_id])

    def pay(self, link_id: str, amount: int | None = None, payment_id: str = "pay_TEST1") -> None:
        link = self.links[link_id]
        link.update(status="paid", amount_paid=link["amount"] if amount is None else amount,
                    payments=[{"payment_id": payment_id, "amount": link["amount"], "status": "captured"}])

    async def close(self) -> None:
        pass


@pytest.fixture
def rz():
    return FakeRazorpay()


@pytest.fixture
def renv(monkeypatch, settings, store, fx, keys, chain, rz):
    for name, value in (("RAZORPAY_API_KEY", CREDS.key_id), ("RAZORPAY_API_SECRET", CREDS.key_secret),
                        ("RAZORPAY_WEBHOOK_SECRET", CREDS.webhook_secret)):
        monkeypatch.setenv(name, value)  # presence only: the startup check (environment.py) wants them set
    settings.collect_rail, settings.public_base_url, settings.web_app_url = "razorpay", BASE, BASE
    ctx = build_context(settings, chains={"arc": ARC}, clients={"arc": chain}, store=store, fx=fx, keys=keys,
                        razorpay=rz)
    with TestClient(create_app(ctx, workers=False, fx=False)) as client:
        yield client, ctx, chain, rz


def create(client, wallet="0x" + "11" * 20, inr="10"):
    r = client.post("/onramp", json={"chain": "arc", "wallet": wallet, "inr": inr})
    assert r.status_code == 200, r.text
    return r.json()


def callback_params(link_id: str, ref: str, payment_id="pay_TEST1", status="paid", secret=CREDS.key_secret):
    p = {"razorpay_payment_id": payment_id, "razorpay_payment_link_id": link_id,
         "razorpay_payment_link_reference_id": ref, "razorpay_payment_link_status": status}
    p["razorpay_signature"] = hmac_hex(secret, callback_message(p))
    return p


def webhook(client, link_id: str, event="payment_link.paid", event_id="evt_1", secret=CREDS.webhook_secret,
            sig=None):
    raw = json.dumps({"entity": "event", "event": event,
                      "payload": {"payment_link": {"entity": {"id": link_id}}}}).encode()
    headers = {"x-razorpay-signature": hmac_hex(secret, raw) if sig is None else sig, "x-razorpay-event-id": event_id,
               "content-type": "application/json"}
    return client.post("/onramp/razorpay/webhook", content=raw, headers=headers)


# ---- signatures (pure) -----------------------------------------------------------------------


def test_webhook_signature():
    body = b'{"event":"payment_link.paid"}'
    good = hmac_hex("hook-secret", body)
    assert verify_webhook_signature(body, good, "hook-secret")
    assert verify_webhook_signature(body, good.upper(), "hook-secret")
    assert not verify_webhook_signature(body + b" ", good, "hook-secret")
    assert not verify_webhook_signature(body, good, "other")
    assert not verify_webhook_signature(body, None, "hook-secret")
    assert not verify_webhook_signature(body, hmac_hex("", body), "")  # an empty secret never verifies


def test_callback_signature():
    p = callback_params("plink_A", "ref")
    assert callback_message(p) == b"plink_A|ref|paid|pay_TEST1"
    assert verify_callback_signature(p, CREDS.key_secret)
    assert not verify_callback_signature({**p, "razorpay_payment_link_status": "created"}, CREDS.key_secret)
    assert not verify_callback_signature(p, "")
    assert not verify_callback_signature({k: v for k, v in p.items() if k != "razorpay_payment_id"}, CREDS.key_secret)


def test_check_paid_link():
    link, order = {"link_id": "plink_A", "reference_id": "r"}, {"inr_paise": 1000}
    paid = {"id": "plink_A", "reference_id": "r", "status": "paid", "currency": "INR", "amount": 1000,
            "amount_paid": 1000, "payments": [{"payment_id": "pay_X", "status": "captured"}]}
    assert check_paid_link(paid, link, order) == ("paid", "pay_X")
    assert check_paid_link({**paid, "status": "created"}, link, order)[0] == "pending"
    assert check_paid_link({**paid, "status": "expired"}, link, order)[0] == "closed"
    assert check_paid_link({**paid, "amount_paid": 999}, link, order)[0].startswith("mismatch")
    assert check_paid_link({**paid, "currency": "USD"}, link, order)[0].startswith("mismatch")
    assert check_paid_link({**paid, "reference_id": "other"}, link, order)[0].startswith("mismatch")


async def test_client_sends_basic_auth_and_hides_secrets_in_errors():
    seen = []

    def handler(req: httpx.Request):
        seen.append(req)
        if req.method == "POST":
            return httpx.Response(200, json={"id": "plink_Z1", "short_url": "https://rzp.io/x", "status": "created"})
        return httpx.Response(400, json={"error": {"code": "BAD_REQUEST_ERROR", "description": "bad id"}})

    c = RazorpayClient(CREDS, transport=httpx.MockTransport(handler))
    r = await c.create_payment_link(amount_paise=1000, reference_id="r", description="d", expire_by=1,
                                    callback_url=BASE + "/cb", notes={"orderId": "0x1"})
    assert r["id"] == "plink_Z1"
    body = json.loads(seen[0].content)
    assert body["amount"] == 1000 and body["currency"] == "INR" and body["callback_method"] == "get"
    assert body["accept_partial"] is False and seen[0].headers["authorization"].startswith("Basic ")
    with pytest.raises(RazorpayError) as e:
        await c.fetch_payment_link("plink_Z1")
    assert "bad id" in str(e.value) and CREDS.key_secret not in str(e.value)
    with pytest.raises(RazorpayError):
        await c.fetch_payment_link("../orders")  # never builds an arbitrary path
    assert "key-secret" not in repr(CREDS)
    await c.close()


# ---- create ----------------------------------------------------------------------------------


def test_create_returns_a_checkout_link(renv):
    client, ctx, _, rz = renv
    o = create(client)
    assert o["status"] == "AWAITING_FIAT" and o["payTo"] is None
    assert o["checkout"]["provider"] == "razorpay" and o["checkout"]["url"].startswith("https://rzp.io/")
    kw = rz.created[0]
    assert kw["amount_paise"] == 1000 and kw["reference_id"] == o["orderId"][2:42]
    assert kw["callback_url"] == BASE + "/onramp/razorpay/callback" and kw["notes"]["orderId"] == o["orderId"]
    assert client.get(f"/onramp/{o['orderId']}").json()["checkout"] == o["checkout"]


@pytest.mark.parametrize("attr,code", [("pool_balance", "POOL_INSUFFICIENT"), ("daily_cap_left", "DAILY_CAP")])
def test_create_refuses_inr_the_vault_cannot_pay_out(renv, attr, code):
    client, _, chain, rz = renv
    setattr(chain, attr, 1)
    r = client.post("/onramp", json={"chain": "arc", "wallet": "0x" + "11" * 20, "inr": "10"})
    assert r.status_code == 409 and r.json()["error"] == code and rz.created == []


def test_create_fails_cleanly_when_razorpay_is_down(renv):
    client, ctx, _, rz = renv
    rz.fail_create = True
    r = client.post("/onramp", json={"chain": "arc", "wallet": "0x" + "11" * 20, "inr": "10"})
    assert r.status_code == 502 and r.json()["error"] == "PAYMENT_PROVIDER"
    assert ctx.store.onramps_by_status("AWAITING_FIAT") == []  # no order without a link


# ---- webhook ---------------------------------------------------------------------------------


def test_webhook_pays_once(renv):
    client, ctx, chain, rz = renv
    o = create(client)
    link_id = o["checkout"]["linkId"]
    rz.pay(link_id)
    r = webhook(client, link_id)
    assert r.status_code == 200 and r.json() == {"ok": True}
    g = client.get(f"/onramp/{o['orderId']}").json()
    assert g["status"] == "PAID" and g["paidTx"] and g["checkout"]["paymentId"] == "pay_TEST1"
    assert [s[0] for s in chain.sent] == ["onramp"] and chain.sent[0][1][1] == o["wallet"]
    assert chain.sent[0][1][2] == int(o["amountBaseUnits"]) and chain.sent[0][1][3] == 1000
    assert webhook(client, link_id).json()["duplicate"] is True  # same event id
    assert webhook(client, link_id, event_id="evt_2").status_code == 200  # redelivered under a new id
    assert len(chain.sent) == 1


def test_webhook_rejects_bad_signatures(renv):
    client, _, chain, rz = renv
    link_id = create(client)["checkout"]["linkId"]
    rz.pay(link_id)
    r = webhook(client, link_id, secret="wrong")
    assert r.status_code == 400 and r.json()["error"] == "BAD_SIGNATURE"
    assert webhook(client, link_id, sig="").status_code == 400
    assert chain.sent == []


def test_a_signed_webhook_alone_does_not_pay(renv):
    """Even a correctly signed payment_link.paid pays nothing unless Razorpay's API agrees."""
    client, ctx, chain, rz = renv
    o = create(client)
    assert webhook(client, o["checkout"]["linkId"]).status_code == 200  # link still "created" at Razorpay
    assert client.get(f"/onramp/{o['orderId']}").json()["status"] == "AWAITING_FIAT" and chain.sent == []


def test_underpaid_link_is_not_paid_out(renv):
    client, ctx, chain, rz = renv
    o = create(client)
    rz.pay(o["checkout"]["linkId"], amount=999)
    webhook(client, o["checkout"]["linkId"])
    g = client.get(f"/onramp/{o['orderId']}").json()
    assert g["status"] == "AWAITING_FIAT" and g["lastError"].startswith("mismatch") and chain.sent == []


def test_expired_link_expires_the_order(renv):
    client, ctx, chain, rz = renv
    o = create(client)
    rz.links[o["checkout"]["linkId"]]["status"] = "expired"
    webhook(client, o["checkout"]["linkId"], event="payment_link.expired")
    assert client.get(f"/onramp/{o['orderId']}").json()["status"] == "EXPIRED" and chain.sent == []


def test_unknown_links_and_other_events_are_acknowledged(renv):
    client, _, chain, _ = renv
    assert webhook(client, "plink_NOTOURS").json()["ignored"] == "unknown link"
    assert webhook(client, "plink_X", event="payment.captured", event_id="e9").json()["ignored"] == "payment.captured"
    assert chain.sent == []


# ---- callback --------------------------------------------------------------------------------


def test_callback_pays_and_redirects_to_the_app(renv):
    client, ctx, chain, rz = renv
    o = create(client)
    link_id = o["checkout"]["linkId"]
    rz.pay(link_id)
    r = client.get("/onramp/razorpay/callback", params=callback_params(link_id, o["orderId"][2:42]),
                   follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == f"{BASE}/?onramp={o['orderId']}&payment=received"
    assert client.get(f"/onramp/{o['orderId']}").json()["status"] == "PAID" and len(chain.sent) == 1
    webhook(client, link_id)  # the webhook arriving after the callback is a no-op
    assert len(chain.sent) == 1


def test_forged_callback_pays_nothing(renv):
    client, ctx, chain, rz = renv
    o = create(client)
    rz.pay(o["checkout"]["linkId"])
    p = callback_params(o["checkout"]["linkId"], o["orderId"][2:42], secret="guess")
    r = client.get("/onramp/razorpay/callback", params=p, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].endswith("payment=unverified")
    assert chain.sent == [] and ctx.store.get_razorpay_link(o["orderId"])["notified_at"] is None


# ---- reconcile -------------------------------------------------------------------------------


async def test_reconcile_retries_after_provider_and_chain_errors(renv):
    client, ctx, chain, rz = renv
    o = create(client)
    link_id = o["checkout"]["linkId"]
    rz.pay(link_id)
    rz.fail_fetch = True
    webhook(client, link_id)
    assert client.get(f"/onramp/{o['orderId']}").json()["status"] == "AWAITING_FIAT"

    rz.fail_fetch = False
    chain.fail["onramp"] = ChainError("onramp", "InsufficientPoolBalance")
    assert await ctx.onramp.reconcile() == {o["orderId"]: "pay_failed"}
    assert client.get(f"/onramp/{o['orderId']}").json()["status"] == "PAYING"

    del chain.fail["onramp"]
    assert await ctx.onramp.reconcile() == {o["orderId"]: "paid"}
    assert client.get(f"/onramp/{o['orderId']}").json()["status"] == "PAID" and len(chain.sent) == 1
    assert await ctx.onramp.reconcile() == {}  # settled: nothing left to follow up


def test_routes_are_off_on_the_manual_rail(settings, store, fx, keys, chain):
    ctx = build_context(settings, chains={"arc": ARC}, clients={"arc": chain}, store=store, fx=fx, keys=keys)
    with TestClient(create_app(ctx, workers=False, fx=False)) as client:
        assert client.post("/onramp/razorpay/webhook", content=b"{}").json()["error"] == "RAIL_DISABLED"
        r = client.post("/onramp", json={"chain": "arc", "wallet": "0x" + "11" * 20, "inr": "10"})
        assert r.json()["checkout"] is None and r.json()["payTo"]["upiId"] == "rupeerail@okaxis"
