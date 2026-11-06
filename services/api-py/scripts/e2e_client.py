#!/usr/bin/env python3
"""Live end-to-end client that behaves like the frontend. IT SENDS REAL TESTNET TRANSACTIONS
(through the backend's paymaster), so only run it after the user approves.

  GET /config -> GET /account -> POST /quote -> sign Permit + OfframpIntent locally with the USER key
  (OTHER_WALLET_PRIVATE_KEY, read as text from the repo-root .env, never printed) -> POST /offramp
  -> poll GET /offramp/{id}?refresh=1 until COMPLETED/REFUNDED -> optionally sign a DisputeIntent
  and POST /dispute -> poll until DISPUTED.

Usage (backend running on :8788 with workers on):
  uv run python scripts/e2e_client.py --chain arc --inr 10
  uv run python scripts/e2e_client.py --chain whitechain --inr 10 --dispute
Only public addresses, ids and tx links are printed.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import httpx
from eth_account import Account
from eth_account.messages import encode_typed_data

SERVICE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVICE))

from app.config import Settings  # noqa: E402
from app.keys import load_user_key  # noqa: E402

DEFAULT_QR = "upi://pay?pa=chaipoint@okaxis&pn=Chai%20Point&cu=INR"


def b32(h: str) -> bytes:
    return bytes.fromhex(h[2:] if h.startswith("0x") else h)


def typed(types: dict, primary: str, domain: dict, message: dict) -> dict:
    return {"types": {"EIP712Domain": types["EIP712Domain"], primary: types[primary]}, "primaryType": primary,
            "domain": domain, "message": message}


def sign(user, full: dict) -> bytes:
    return bytes(user.sign_message(encode_typed_data(full_message=full)).signature)


def check(r: httpx.Response) -> dict:
    if r.status_code >= 400:
        print(f"HTTP {r.status_code}: {r.text}")
        sys.exit(1)
    return r.json()


def poll(api: httpx.Client, id_: str, want: set[str], timeout: float) -> dict:
    t0, last = time.time(), None
    while time.time() - t0 < timeout:
        o = check(api.get(f"/offramp/{id_}", params={"refresh": "true"}))
        state = (o.get("status"), o.get("payout"), (o.get("onchain") or {}).get("status"))
        if state != last:
            print(f"  t+{time.time() - t0:5.1f}s status={state[0]} payout={state[1]} onchain={state[2]}")
            last = state
        if state[2] in want:
            return o
        time.sleep(3)
    print("timed out waiting for", want)
    sys.exit(2)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--api", default="http://127.0.0.1:8788")
    ap.add_argument("--chain", default="arc", choices=["arc", "whitechain"])
    ap.add_argument("--inr", default="10")
    ap.add_argument("--qr", default=DEFAULT_QR)
    ap.add_argument("--dispute", action="store_true", help="after COMPLETED, sign and relay a dispute")
    ap.add_argument("--timeout", type=float, default=180)
    a = ap.parse_args()

    user = load_user_key(Settings().env_file)
    print(f"user {user.address}")
    api = httpx.Client(base_url=a.api, timeout=150)

    cfg = check(api.get("/config"))
    chain = cfg["chains"][a.chain]
    print(f"chain {a.chain} ({chain['chainId']}) vault {chain['vault']} paymaster {cfg['paymaster']}")
    acct = check(api.get(f"/account/{a.chain}/{user.address}"))
    print(f"balance {acct['balanceFormatted']} {acct['symbol']} permitNonce {acct['permitNonce']}")

    q = check(api.post("/quote", json={"chain": a.chain, "user": user.address, "inr": a.inr, "qrText": a.qr}))
    it, d = q["intent"], q["display"]
    print(f"quote {q['id']}: Pay Rs {a.inr} to {q['payee']['name']} ({q['payee']['upiId']}) = "
          f"{d['amount']} {d['symbol']} at {d['rate']} INR/token (market {d['marketRate']})")

    # Exactly what the frontend passes to eth_signTypedData_v4: the backend's pre-filled payloads.
    psig = sign(user, q["signing"]["permit"])
    usig = sign(user, q["signing"]["intent"])
    permit_deadline = int(q["signing"]["permit"]["message"]["deadline"])
    res = check(api.post("/offramp", json={
        "chain": a.chain, "intent": it, "userSig": "0x" + usig.hex(),
        "permit": {"deadline": str(permit_deadline), "v": psig[64], "r": "0x" + psig[:32].hex(),
                   "s": "0x" + psig[32:64].hex()},
    }))
    print(f"submitted ({res['status']}) by paymaster {res['paidBy']}: {res['explorer']}")

    o = poll(api, it["id"], {"COMPLETED", "REFUNDED"}, a.timeout)
    for k, v in (o.get("txs") or {}).items():
        if v:
            print(f"  tx {k}: {chain['explorer']}/tx/{v}")
    if o["onchain"]["status"] != "COMPLETED" or not a.dispute:
        return 0

    deadline = int(time.time()) + 600
    dmsg = {"id": b32(it["id"]), "user": user.address, "deadline": deadline}
    dsig = sign(user, typed(chain["types"], "DisputeIntent", chain["vaultDomain"], dmsg))
    d = check(api.post("/dispute", json={"chain": a.chain, "id": it["id"], "deadline": str(deadline),
                                         "userSig": "0x" + dsig.hex()}))
    print(f"dispute relayed: {d['explorer']}")
    o = poll(api, it["id"], {"DISPUTED"}, a.timeout)
    print(f"  arbiter deadline (timers.disputeDeadline) {o['timers']['disputeDeadline']}")
    return 0


if __name__ == "__main__":
    Account.enable_unaudited_hdwallet_features()
    raise SystemExit(main())
