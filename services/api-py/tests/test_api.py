"""FastAPI TestClient over the real app with a fake chain (no RPC, no broadcast).

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

from app.chain import ChainError
from app.commitment import hex32, qr_commitment
from app.context import build_context
from app.eip712 import OfframpIntent, dispute_typed_data, ecdsa_try_recover, intent_digest, sign_digest_typed, vault_domain
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


def sign_payload(acct, payload: dict) -> bytes:
    """Signs exactly what the frontend would pass to eth_signTypedData_v4."""
    return bytes(acct.sign_message(encode_typed_data(full_message=payload)).signature)


def offramp_body(q, user, *, intent_signer=None, permit_signer=None, tamper=None, permit_nonce=None):
    permit_payload = json.loads(json.dumps(q["signing"]["permit"]))
    if permit_nonce is not None:
        permit_payload["message"]["nonce"] = str(permit_nonce)
    usig = sign_payload(intent_signer or user, q["signing"]["intent"])
    psig = sign_payload(permit_signer or user, permit_payload)
    intent = dict(q["intent"])
    if tamper:
        intent.update(tamper)
    return {"chain": "arc", "intent": intent, "userSig": "0x" + usig.hex(),
            "permit": {"deadline": permit_payload["message"]["deadline"], "v": psig[64],
                       "r": "0x" + psig[:32].hex(), "s": "0x" + psig[32:64].hex()}}


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


def test_offramp_success_shape_then_used(env):
    client, ctx, chain, user = env
    chain.nonces[user.address.lower()] = 0
    q = quote(client, user).json()
    body = offramp_body(q, user)
    r = client.post("/offramp", json=body)
    assert r.status_code == 200, r.text
    j = r.json()
    assert set(j) == {"id", "txHash", "explorer", "paidBy", "status"}
    assert j["id"] == q["id"] and j["status"] == "SUBMITTED" and j["paidBy"] == ctx.keys.paymaster.address
    assert HEX32.match(j["txHash"]) and j["explorer"] == f"https://explorer.testnet.arc.io/tx/{j['txHash']}"
    fn, args = chain.sent[0]
    assert fn == "offramp" and args[2] == bytes.fromhex(q["quoteSig"][2:])
    assert ctx.store.get_quote(q["id"])["status"] == "USED"
    assert_error(client.post("/offramp", json=body), 409, "QUOTE_USED")
    assert len(chain.sent) == 1
    s = client.get(f"/offramp/{q['id']}").json()
    assert s["status"] == "SUBMITTED" and s["payout"] == "NONE"


@pytest.mark.parametrize("tamper,field", [
    ({"amount": "100665"}, "amount"),
    ({"inrPaise": "999"}, "inr_paise"),
    ({"rateE6": "99338881"}, "rate_e6"),
    ({"qrCommitment": "0x" + "11" * 32}, "qr_commitment"),
    ({"deadline": "9999999999"}, "deadline"),
    ({"user": "0x" + "12" * 20}, "user"),
])
def test_offramp_quote_mismatch(env, tamper, field):
    client, _, chain, user = env
    q = quote(client, user).json()
    r = client.post("/offramp", json=offramp_body(q, user, tamper=tamper))
    assert_error(r, 400, "QUOTE_MISMATCH")
    assert field in r.json()["fields"] and chain.sent == []


def test_offramp_unknown_expired_bad_chain(env):
    client, ctx, chain, user = env
    q = quote(client, user).json()
    assert_error(client.post("/offramp", json=offramp_body(q, user, tamper={"id": "0x" + "77" * 32})), 404,
                 "UNKNOWN_QUOTE")
    past = int(time.time()) - 1
    ctx.store._exec("UPDATE quotes SET deadline = ? WHERE id = ?", (past, q["id"]))
    assert_error(client.post("/offramp", json=offramp_body(q, user, tamper={"deadline": str(past)})), 410,
                 "QUOTE_EXPIRED")
    assert_error(client.post("/offramp", json={**offramp_body(q, user), "chain": "whitechain"}), 400, "BAD_CHAIN")
    assert_error(client.post("/offramp", json={"chain": "arc"}), 400, "BAD_REQUEST")
    assert chain.sent == []


def test_offramp_bad_intent_sig(env):
    client, _, chain, user = env
    q = quote(client, user).json()
    assert_error(client.post("/offramp", json=offramp_body(q, user, intent_signer=Account.create())), 400,
                 "BAD_INTENT_SIG")
    assert chain.sent == []


def test_offramp_erc1271_wallet(env):
    client, _, chain, user = env
    q = quote(client, user).json()
    body = offramp_body(q, user)
    chain.code[user.address.lower()] = b"\x60\x80"  # user is a contract wallet now
    assert_error(client.post("/offramp", json=body), 400, "BAD_INTENT_SIG")
    chain.valid_1271.add((user.address.lower(), bytes.fromhex(body["userSig"][2:])))
    assert client.post("/offramp", json=body).status_code == 200


def test_offramp_bad_permit_sig(env):
    client, _, chain, user = env
    q = quote(client, user).json()  # permit pre-filled with nonce 3
    chain.nonces[user.address.lower()] = 4  # token nonce moved on (e.g. another permit)
    bad = offramp_body(q, user)
    assert_error(client.post("/offramp", json=bad), 400, "BAD_PERMIT_SIG")
    assert_error(client.post("/offramp", json=offramp_body(q, user, permit_signer=Account.create())), 400,
                 "BAD_PERMIT_SIG")
    chain.allowances[user.address.lower()] = 10**9  # an existing allowance makes a bad permit harmless
    assert client.post("/offramp", json=bad).status_code == 200


def test_offramp_insufficient_balance(env):
    client, _, chain, user = env
    chain.balances[user.address.lower()] = 5
    q = quote(client, user).json()
    assert_error(client.post("/offramp", json=offramp_body(q, user)), 400, "INSUFFICIENT_BALANCE")


def test_offramp_simulation_failed_releases_quote(env):
    client, ctx, chain, user = env
    q = quote(client, user).json()
    chain.fail["offramp"] = ChainError("offramp", "InvalidQuote")
    r = client.post("/offramp", json=offramp_body(q, user))
    assert_error(r, 400, "SIMULATION_FAILED")
    assert r.json()["revert"]["error"] == "InvalidQuote"
    assert ctx.store.get_quote(q["id"])["status"] == "QUOTED"  # can be retried


# ---- 3.5 GET /offramp/{id} ---------------------------------------------------------------------


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


def test_offramp_status_completed_then_disputed_timers(env):
    client, ctx, chain, user = env
    q = quote(client, user).json()
    id_ = _locked_payment(ctx, chain, q, status="COMPLETED")
    window_end = chain.now + 3600
    ctx.store.update_payment(id_, releasable_at=window_end, dispatch_state="DISPATCHED")
    ctx.store.add_tx(id_, "complete", "0x" + "c0" * 32)
    o = client.get(f"/offramp/{id_}").json()
    assert o["status"] == "COMPLETED" and o["payout"] == "DISPATCHED" and o["canDispute"] is True
    assert o["timers"]["releasableAt"] == str(window_end) and o["timers"]["disputeDeadline"] is None
    assert o["txs"]["complete"] == "0x" + "c0" * 32
    ctx.store.update_payment(id_, status="DISPUTED", dispute_deadline=chain.now + 7 * 86400)
    ctx.store.add_tx(id_, "dispute", "0x" + "d0" * 32)
    o = client.get(f"/offramp/{id_}").json()
    assert o["status"] == "DISPUTED" and o["canDispute"] is False
    assert o["timers"]["disputeDeadline"] == str(chain.now + 7 * 86400)
    assert o["timers"]["releasableAt"] == str(window_end)  # never the overloaded arbiter deadline
    ctx.store.update_payment(id_, status="REVERSED")
    ctx.store.add_tx(id_, "resolve", "0x" + "f0" * 32)
    o = client.get(f"/offramp/{id_}").json()
    assert o["txs"]["final"] == "0x" + "f0" * 32 and o["timers"]["disputeDeadline"] is None


def test_offramp_status_refund_reason_and_refresh(env):
    client, ctx, chain, user = env
    q = quote(client, user).json()
    id_ = _locked_payment(ctx, chain, q, status="REFUNDED")
    ctx.store.update_payment(id_, refund_reason=4, dispatch_state="SKIPPED")
    o = client.get(f"/offramp/{id_}").json()
    assert (o["refundReason"], o["payout"]) == ("4", "SKIPPED")
    chain.lock(id_, user.address, 100666, 1000, q["intent"]["qrCommitment"], status=6)
    chain.dls[id_].releasable_at = chain.now + 999
    oc = client.get(f"/offramp/{id_}", params={"refresh": "true"}).json()["onchain"]
    assert oc["status"] == "DISPUTED" and oc["releasableAt"] is None and oc["disputeDeadline"] == str(chain.now + 999)
    assert_error(client.get("/offramp/0x" + "55" * 32), 404, "NOT_FOUND")


# ---- 3.6 /dispute ------------------------------------------------------------------------------


def _completed(chain, user, id_="0x" + "d5" * 32):
    chain.lock(id_, user.address, 100666, 1000, "0x" + "01" * 32, status=2)
    chain.dls[id_].releasable_at = chain.now + 3600
    return id_


def _dispute_body(id_, user, deadline, signer=None):
    sig = sign_digest_typed(signer or user, dispute_typed_data(DOM, id_, user.address, deadline))
    return {"chain": "arc", "id": id_, "deadline": str(deadline), "userSig": "0x" + sig.hex()}


def test_dispute_success_shape(env):
    client, ctx, chain, user = env
    id_ = _completed(chain, user)
    body = _dispute_body(id_, user, chain.now + 600)
    r = client.post("/dispute", json=body)
    assert r.status_code == 200, r.text
    j = r.json()
    assert {"id", "txHash", "status"} <= set(j) and j["id"] == id_ and j["status"] == "DISPUTED"
    assert HEX32.match(j["txHash"]) and chain.sent[0][0] == "disputeOfframpWithSig"
    assert ctx.store.get_payment(id_)["dispute_deadline"] == chain.now + 7 * 86400
    assert_error(client.post("/dispute", json=body), 409, "NOT_DISPUTABLE")  # now DISPUTED
    assert len(chain.sent) == 1


def test_dispute_bad_sig(env):
    client, _, chain, user = env
    id_ = _completed(chain, user)
    assert_error(client.post("/dispute", json=_dispute_body(id_, user, chain.now + 600, signer=Account.create())),
                 400, "BAD_DISPUTE_SIG")
    other = _dispute_body(id_, user, chain.now + 601)["userSig"]  # signed for another deadline
    assert_error(client.post("/dispute", json={**_dispute_body(id_, user, chain.now + 600), "userSig": other}),
                 400, "BAD_DISPUTE_SIG")
    assert_error(client.post("/dispute", json={**_dispute_body(id_, user, chain.now + 600), "userSig": "0xzz"}),
                 400, "BAD_REQUEST")
    assert chain.sent == []


def test_dispute_not_disputable_and_expired(env):
    client, _, chain, user = env
    id_ = _completed(chain, user)
    assert_error(client.post("/dispute", json=_dispute_body(id_, user, chain.now - 1)), 400, "INTENT_EXPIRED")
    chain.dls[id_].releasable_at = chain.now  # window closed
    assert_error(client.post("/dispute", json=_dispute_body(id_, user, chain.now + 600)), 409, "NOT_DISPUTABLE")
    assert_error(client.post("/dispute", json=_dispute_body("0x" + "99" * 32, user, chain.now + 600)), 409,
                 "NOT_DISPUTABLE")
    assert chain.sent == []


def test_dispute_simulation_failed(env):
    client, _, chain, user = env
    id_ = _completed(chain, user)
    chain.fail["disputeOfframpWithSig"] = ChainError("disputeOfframpWithSig", "DisputeWindowExpired", {"expiredAt": 1})
    assert_error(client.post("/dispute", json=_dispute_body(id_, user, chain.now + 600)), 400, "SIMULATION_FAILED")


# ---- 3.7 events --------------------------------------------------------------------------------


def test_events_shape(env):
    client, ctx, _, _ = env
    ctx.store.insert_event({"chain": "arc", "tx": "0x" + "aa" * 32, "log_index": 0, "block": 5,
                            "name": "OfframpLocked", "ref_id": "0x" + "01" * 32, "args": {"amount": "1"}})
    ctx.store.insert_event({"chain": "arc", "tx": "0x" + "bb" * 32, "log_index": 0, "block": 6,
                            "name": "OfframpCompleted", "ref_id": "0x" + "01" * 32, "args": {}})
    items = client.get("/events", params={"chain": "arc", "id": "0x" + "01" * 32}).json()
    assert isinstance(items, list) and [e["name"] for e in items] == ["OfframpCompleted", "OfframpLocked"]
    assert items[1] == {"chain": "arc", "name": "OfframpLocked", "id": "0x" + "01" * 32, "block": "5",
                        "txHash": "0x" + "aa" * 32, "explorer": "https://explorer.testnet.arc.io/tx/0x" + "aa" * 32,
                        "args": {"amount": "1"}}


async def test_sse_stream_names_events_and_pings(settings, store, fx, keys, chain):
    """Drives the SSE generator directly (TestClient buffers infinite streams)."""
    import asyncio

    from app.api import stream_events

    ctx = build_context(settings, chains={"arc": ARC}, clients={"arc": chain}, store=store, fx=fx, keys=keys)

    class Req:
        class app:  # noqa: N801
            class state:  # noqa: N801
                pass

        async def is_disconnected(self):
            return False

    Req.app.state.ctx = ctx
    resp = await stream_events(Req(), ping=0.05)
    it = resp.body_iterator
    assert (await it.__anext__()).startswith("retry:")
    ctx.hub.publish({"type": "payout", "id": "x"})  # internal: not streamed
    ev = {"type": "chain_event", "chain": "arc", "name": "OfframpLocked", "id": "0x" + "01" * 32, "block": "5",
          "txHash": "0x" + "aa" * 32, "explorer": "e", "args": {}}
    ctx.hub.publish(ev)
    chunk = await asyncio.wait_for(it.__anext__(), 1)
    head, data = chunk.split("\n")[0], json.loads(chunk.split("\n")[1][len("data: "):])
    assert head == "event: OfframpLocked" and data == {k: v for k, v in ev.items() if k != "type"}
    assert (await it.__anext__()).startswith("event: ping\n")
    await it.aclose()
    assert ctx.hub.subscribers == 0


# ---- 3.8 onramp + admin ------------------------------------------------------------------------


def test_onramp_shapes(env):
    client, _, chain, user = env
    r = client.post("/onramp", json={"chain": "arc", "wallet": user.address, "inr": "10"})
    assert r.status_code == 200, r.text
    o = r.json()
    assert {"orderId", "amount", "symbol", "rate", "payTo", "status"} <= set(o)
    assert (o["amount"], o["symbol"], o["rate"], o["status"]) == ("0.098224", "USDC", "101.81", "AWAITING_FIAT")
    assert o["payTo"] == {"upiId": "rupeerail@okaxis", "note": f"RR-{o['orderId'][2:12]}"}
    path = f"/admin/onramp/{o['orderId']}/fiat-received"
    assert_error(client.post(path, json={"bankRef": "UTR1"}), 401, "UNAUTHORIZED")
    r = client.post(path, json={"bankRef": "UTR1"}, headers={"x-admin-token": "test-admin"})
    assert r.status_code == 200 and r.json()["status"] == "PAID" and HEX32.match(r.json()["txHash"])
    assert chain.sent[0][0] == "onramp" and chain.sent[0][1][2] == 98224
    g = client.get(f"/onramp/{o['orderId']}").json()
    assert g["status"] == "PAID" and g["paidTx"] == r.json()["txHash"]
    assert client.post(path, headers={"x-admin-token": "test-admin"}).json()["status"] == "PAID"  # idempotent
    assert len(chain.sent) == 1
    assert_error(client.get("/onramp/0x" + "00" * 32), 404, "UNKNOWN_ORDER")
    assert_error(client.post("/onramp", json={"chain": "arc", "wallet": "nope", "inr": "10"}), 400, "BAD_REQUEST")


def test_admin_reverse(env):
    client, ctx, chain, user = env
    id_ = _completed(chain, user)
    ctx.store.ensure_payment(id_, "arc", status="COMPLETED")
    assert_error(client.post(f"/admin/offramp/{id_}/reverse"), 401, "UNAUTHORIZED")
    r = client.post(f"/admin/offramp/{id_}/reverse", headers={"x-admin-token": "test-admin"})
    assert r.status_code == 200 and r.json()["result"] == "reversed" and chain.sent[0][0] == "reverseOfframp"


def test_view_token_is_derived_from_the_id_when_a_secret_is_set(env):
    from app.quote_service import derive_view_token
    client, ctx, chain, user = env

    # The same vector is in apps/upi-payer/tests/test_agent.py, so changing either side fails a test.
    assert derive_view_token("test-secret", bytes.fromhex("ab" * 32)) == "d570cbe8bc6adfc240d6e7da47f5856e83789a36600820ee92eb1167c7aa1356"
    assert derive_view_token("", bytes.fromhex("ab" * 32)) != derive_view_token("", bytes.fromhex("ab" * 32))  # random

    ctx.settings.view_token_secret = "test-secret"
    q = quote(client, user).json()
    token = derive_view_token("test-secret", bytes.fromhex(q["id"][2:]))
    assert q["viewToken"] == token
    assert client.get(f"/offramp/{q['id']}", headers={"x-view-token": token}).json()["payee"]["upiId"] == "chaipoint@okaxis"
    assert client.get(f"/offramp/{q['id']}", headers={"x-view-token": "0" * 64}).json()["payee"] is None
    assert client.get(f"/offramp/{q['id']}").json()["payee"] is None


def test_raw_qr_goes_only_to_a_view_token_holder(env):
    client, ctx, chain, user = env
    q = quote(client, user).json()
    assert client.get(f"/offramp/{q['id']}", headers={"x-view-token": q["viewToken"]}).json()["qrText"] == QR
    assert client.get(f"/offramp/{q['id']}").json()["qrText"] is None
    assert client.get(f"/offramp/{q['id']}", headers={"x-view-token": "0" * 32}).json()["qrText"] is None
    ctx.settings.expose_payee_by_id = True  # this setting exposes the payee, and must not expose the raw QR
    assert client.get(f"/offramp/{q['id']}").json()["qrText"] is None

