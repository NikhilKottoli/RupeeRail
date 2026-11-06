"""FastAPI TestClient over the real app with a fake chain (no RPC, no broadcast).

Covers the read and quote endpoints: /health, /config, /quote, GET /offramp/{id}.

Asserts the exact v1 API contract shapes (HANDOFF-payment-cycle-api-metamask.md sections 3.1-3.8):
field names, string-encoded integers, error codes, and the status and payout enums.
"""

from __future__ import annotations

import json
import re
import time

import pytest
from eth_account import Account
from eth_account.messages import encode_typed_data
from eth_utils import to_checksum_address
from fastapi.testclient import TestClient

from app.commitment import hex32, qr_commitment
from app.context import build_context
from app.eip712 import OfframpIntent, ecdsa_try_recover, intent_digest, vault_domain
from app.main import create_app

from .conftest import ARC, QR

DOM = vault_domain(ARC.chain_id, ARC.vault)
VAULT = to_checksum_address(ARC.vault)
INT_STR = re.compile(r"^\d+$")
HEX32 = re.compile(r"^0x[0-9a-f]{64}$")
STATUSES = {"QUOTED", "SUBMITTED", "LOCKED", "COMPLETED", "DISPUTED", "RELEASED", "REFUNDED", "REVERSED"}
PAYOUTS = {"NONE", "DISPATCHING", "DISPATCHED", "FAILED", "NEEDS_OPERATOR", "SKIPPED"}


@pytest.fixture
def env(settings, store, fx, keys, chain, user):
    chain.balances[user.address.lower()] = 10**9
    chain.nonces[user.address.lower()] = 3
    ctx = build_context(settings, chains={"arc": ARC}, clients={"arc": chain}, store=store, fx=fx, keys=keys)
    app = create_app(ctx, workers=False, fx=False)
    with TestClient(app) as client:
        yield client, ctx, chain, user


def quote(client, user, inr="10", qr=QR, chain="arc"):
    return client.post("/quote", json={"chain": chain, "user": user.address, "inr": inr, "qrText": qr})


def assert_error(r, status, code):
    assert r.status_code == status, r.text
    j = r.json()
    assert j["error"] == code and isinstance(j["message"], str) and j["message"], j


# ---- 3.1 /health, 3.2 /config ------------------------------------------------------------------


def test_health_shape(env):
    client, ctx, _, _ = env
    h = client.get("/health").json()
    assert h["ok"] is True and h["chains"] == ["arc"]
    assert h["paymaster"] == ctx.keys.paymaster.address
    assert h["settler"] == ctx.keys.settler.address
    assert h["quoteSigner"] == ctx.keys.quote_signer.address
    assert h["fx"]["USDCINR"] == {"bid": "100.14", "ask": "101", "ageSec": 0}


def test_config_shape(env):
    client, ctx, _, _ = env
    c = client.get("/config").json()
    assert c["paymaster"] == ctx.keys.paymaster.address
    arc = c["chains"]["arc"]
    assert {k: arc[k] for k in ("chainId", "name", "rpc", "explorer", "vault", "token")} == {
        "chainId": 5042002, "name": "Arc Testnet", "rpc": ARC.rpc, "explorer": "https://explorer.testnet.arc.io",
        "vault": VAULT, "token": {"symbol": "USDC", "address": "0x3600000000000000000000000000000000000000",
                                  "decimals": 6}}
    assert arc["vaultDomain"] == {"name": "ScanPayVault", "version": "1", "chainId": 5042002, "verifyingContract": VAULT}
    assert arc["permitDomain"] == {"name": "USDC", "version": "2", "chainId": 5042002,
                                   "verifyingContract": "0x3600000000000000000000000000000000000000"}
    assert set(arc["types"]) >= {"OfframpIntent", "DisputeIntent", "Permit"}
    assert [f["name"] for f in arc["types"]["OfframpIntent"]] == [
        "id", "user", "amount", "inrPaise", "rateE6", "qrCommitment", "deadline"]
    assert [f["name"] for f in arc["types"]["DisputeIntent"]] == ["id", "user", "deadline"]
    assert [f["name"] for f in arc["types"]["Permit"]] == ["owner", "spender", "value", "nonce", "deadline"]


# ---- 3.3 /quote --------------------------------------------------------------------------------


def test_quote_shape(env):
    client, ctx, _, user = env
    r = quote(client, user)
    assert r.status_code == 200, r.text
    q = r.json()
    assert {"id", "intent", "quoteSig", "payee", "display", "signing"} <= set(q)
    it = q["intent"]
    assert set(it) == {"id", "user", "amount", "inrPaise", "rateE6", "qrCommitment", "deadline"}
    assert q["id"] == it["id"] and HEX32.match(it["id"]) and HEX32.match(it["qrCommitment"])
    assert it == {**it, "user": user.address, "amount": "100666", "inrPaise": "1000", "rateE6": "99338880"}
    assert INT_STR.match(it["deadline"]) and int(it["deadline"]) - time.time() == pytest.approx(90, abs=3)
    assert re.match(r"^0x[0-9a-f]{130}$", q["quoteSig"])
    assert q["payee"] == {"upiId": "chaipoint@okaxis", "name": "Chai Point", "amountInQr": None}
    assert q["display"] == {"amount": "0.100666", "symbol": "USDC", "rate": "99.34", "marketRate": "100.14",
                            "globalRef": None, "spreadBps": 80, "networkFee": "₹0",
                            "expiresAt": it["deadline"]}
    # signing.permit: pre-filled, nonce from the token, deadline = intent.deadline
    p = q["signing"]["permit"]
    assert p["primaryType"] == "Permit" and set(p) == {"domain", "types", "primaryType", "message"}
    assert p["domain"] == {"name": "USDC", "version": "2", "chainId": 5042002,
                           "verifyingContract": "0x3600000000000000000000000000000000000000"}
    assert set(p["types"]) == {"EIP712Domain", "Permit"}
    assert p["message"] == {"owner": user.address, "spender": VAULT, "value": "100666", "nonce": "3",
                            "deadline": it["deadline"]}
    i = q["signing"]["intent"]
    assert i["primaryType"] == "OfframpIntent" and set(i["types"]) == {"EIP712Domain", "OfframpIntent"}
    assert i["domain"] == {"name": "ScanPayVault", "version": "1", "chainId": 5042002, "verifyingContract": VAULT}
    assert i["message"] == it
    # the quote signature recovers to the quote signer over the same digest the wallet will sign
    digest = intent_digest(DOM, OfframpIntent.from_json(it))
    assert ecdsa_try_recover(digest, bytes.fromhex(q["quoteSig"][2:])) == ctx.keys.quote_signer.address
    # stored privately; qrText and salt never in the response
    stored = ctx.store.get_quote(it["id"])
    assert stored["status"] == "QUOTED" and stored["qr_text"] == QR
    assert hex32(qr_commitment(stored["salt"], QR)) == it["qrCommitment"]
    assert QR not in r.text and stored["salt"] not in r.text


def test_quote_dynamic_qr_amount_in_qr(env):
    client, _, _, user = env
    q = quote(client, user, qr="upi://pay?pa=shop@ybl&pn=Shop&am=10.00&cu=INR").json()
    assert q["payee"] == {"upiId": "shop@ybl", "name": "Shop", "amountInQr": "10.00"}


@pytest.mark.parametrize("body,status,code", [
    ({"qrText": "000201010211abcdef"}, 400, "INVALID_QR"),
    ({"qrText": "upi://pay?pa=x@y"}, 400, "INVALID_QR"),
    ({"qrText": "upi://pay?pa=shop@ybl&cu=USD"}, 400, "INVALID_QR"),
    ({"qrText": "upi://pay?pa=shop@ybl&pn=" + "A" * 600}, 400, "INVALID_QR"),
    ({"qrText": "upi://pay?pa=shop@ybl&am=20.00"}, 400, "AMOUNT_MISMATCH"),
    ({"chain": "eth"}, 400, "BAD_CHAIN"),
    ({"user": "0x1234"}, 400, "BAD_REQUEST"),
    ({"inr": "10.001"}, 400, "BAD_REQUEST"),
    ({"inr": "0.50"}, 400, "AMOUNT_OUT_OF_RANGE"),
    ({"inr": "1000.01"}, 400, "AMOUNT_OUT_OF_RANGE"),
    ({"qrText": None}, 400, "BAD_REQUEST"),
])
def test_quote_errors(env, body, status, code):
    client, ctx, _, user = env
    base = {"chain": "arc", "user": user.address, "inr": "10", "qrText": QR}
    assert_error(client.post("/quote", json={**base, **body}), status, code)
    assert ctx.store._one("SELECT COUNT(*) AS n FROM quotes")["n"] == 0  # nothing signed or stored


def test_quote_fx_stale(env):
    client, ctx, _, user = env
    ctx.fx._rates["USDCINR"].fetched_at = time.time() - 121
    assert_error(quote(client, user), 503, "FX_STALE")


def test_quote_daily_limit(env):
    client, _, _, user = env
    for _ in range(5):
        assert quote(client, user, inr="1000").status_code == 200
    assert_error(quote(client, user, inr="1"), 429, "DAILY_LIMIT")


def test_quote_rate_limited(env):
    client, ctx, _, user = env
    ctx.limiter.per_minute = 2
    assert quote(client, user).status_code == 200 and quote(client, user).status_code == 200
    assert_error(quote(client, user), 429, "RATE_LIMITED")


# ---- 3.4 /offramp ------------------------------------------------------------------------------


def _locked_payment(ctx, chain, q, *, status="LOCKED"):
    it = q["intent"]
    t = chain.now
    ctx.store.upsert_locked({"id": it["id"], "chain": "arc", "user": it["user"], "amount": int(it["amount"]),
                             "inr_paise": int(it["inrPaise"]), "rate_e6": int(it["rateE6"]),
                             "qr_commitment": it["qrCommitment"], "lock_tx": "0x" + "ee" * 32, "lock_block": 9,
                             "locked_at": t, "completable_at": t + 15, "reclaimable_at": t + 86400,
                             "dispute_window": 86400})
    ctx.store.update_payment(it["id"], status=status)
    return it["id"]


def test_offramp_status_shape(env):
    client, ctx, chain, user = env
    q = quote(client, user).json()
    assert client.get(f"/offramp/{q['id']}").json()["status"] == "QUOTED"
    id_ = _locked_payment(ctx, chain, q)
    o = client.get(f"/offramp/{id_}").json()
    assert set(o) >= {"id", "chain", "user", "status", "payout", "payee", "amount", "inrPaise", "refundReason",
                      "timers", "canDispute", "txs", "explorer"}
    assert o["status"] in STATUSES and o["payout"] in PAYOUTS
    assert (o["id"], o["chain"], o["user"], o["status"], o["payout"]) == (id_, "arc", user.address, "LOCKED", "NONE")
    assert (o["amount"], o["inrPaise"], o["refundReason"]) == ("100666", "1000", None)
    assert o["timers"] == {"completableAt": str(chain.now + 15), "reclaimableAt": str(chain.now + 86400),
                           "releasableAt": None, "disputeDeadline": None}
    assert o["canDispute"] is False
    assert o["txs"] == {"lock": "0x" + "ee" * 32, "complete": None, "dispute": None, "final": None}
    assert o["explorer"] == "https://explorer.testnet.arc.io/tx/0x" + "ee" * 32


def test_offramp_status_payee_privacy(env):
    client, ctx, chain, user = env
    q = quote(client, user).json()
    id_ = _locked_payment(ctx, chain, q)
    anon = client.get(f"/offramp/{id_}")
    assert anon.json()["payee"] is None and "chaipoint" not in anon.text and "Chai" not in anon.text
    mine = client.get(f"/offramp/{id_}", params={"viewToken": q["viewToken"]}).json()
    assert mine["payee"] == {"upiId": "chaipoint@okaxis", "name": "Chai Point"}
    assert client.get(f"/offramp/{id_}", headers={"x-view-token": q["viewToken"]}).json()["payee"]
    assert client.get(f"/offramp/{id_}", params={"viewToken": "nope"}).json()["payee"] is None
    ctx.settings.expose_payee_by_id = True
    assert client.get(f"/offramp/{id_}").json()["payee"]["upiId"] == "chaipoint@okaxis"


