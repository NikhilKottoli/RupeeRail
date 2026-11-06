#!/usr/bin/env python3
"""Everything the extension's browser tests talk to, with nothing real behind it.

Serves the mock store on :5181, a fake off-ramp on :8791, and the real agent server on :8790, signing with a public
test key and reading a throwaway payer ledger. No testnet, wallet or phone is involved.

    uv run python -m tests.e2e_stack
"""

from __future__ import annotations

import os
import re
import sqlite3
import tempfile
import threading
import time
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles

from app.deciders.openai_vision import OpenAIDecider
from app.deciders.rule_based import RuleDecider
from app.main import create_app
from app.payment.flow import FINAL_OK  # noqa: F401
from app.payment.offramp_client import OfframpClient
from app.payment.results import LedgerResults
from app.settings import Settings
from app.signers.local_key import LocalKeySigner

from .test_payment import CHAIN, TOKEN, make_quote

ROOT = Path(__file__).resolve().parents[2]
TEST_KEY = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"  # Foundry's public test key 0
FAKE_URL = "http://127.0.0.1:8791"
LEDGER = Path(tempfile.mkdtemp(prefix="agent-e2e-")) / "payer.sqlite"
state = {"quotes": [], "submits": [], "payer": "paid"}


def ledger_write(id_: str, status: str) -> None:
    db = sqlite3.connect(LEDGER)
    db.execute("CREATE TABLE IF NOT EXISTS payments (key TEXT PRIMARY KEY, upi_id TEXT, paise INTEGER, status TEXT, detail TEXT, utr TEXT, created REAL, updated REAL)")
    db.execute("INSERT OR REPLACE INTO payments VALUES (?,?,?,?,?,?,?,?)", (id_.lower(), "x", 100, status, "", None, 1, 1))
    db.commit()
    db.close()


def fake_offramp() -> FastAPI:
    app = FastAPI()
    next_id = {"n": 0}

    @app.get("/config")
    def config():
        chain = {**CHAIN, "token": {"symbol": "USDC", "decimals": 6, "address": TOKEN}, "name": "Arc Testnet"}
        return {"chains": {"arc": chain}}

    @app.get("/health")
    def health():
        return {"fx": {"USDCINR": {"bid": "100"}}}

    @app.get("/account/{chain}/{address}")
    def account(chain: str, address: str):
        return {"balance": "20000000", "allowance": "0", "balanceFormatted": "20.000000", "symbol": "USDC"}

    @app.post("/quote")
    async def quote(request: Request):
        body = await request.json()
        state["quotes"].append(body)
        payee = re.search(r"pa=([^&]+)", body["qrText"]).group(1)
        q = make_quote(wallet=body["user"], payee=payee, inr=str(int(round(float(body["inr"]) * 100))))
        next_id["n"] += 1
        id_ = "0x" + f"{next_id['n']:064x}"
        deadline = str(int(time.time()) + 90)
        q["id"] = id_
        for m in (q["intent"], q["signing"]["intent"]["message"]):
            m["id"], m["deadline"] = id_, deadline
        q["signing"]["permit"]["message"]["deadline"] = deadline
        return q

    @app.post("/offramp")
    async def offramp(request: Request):
        body = await request.json()
        state["submits"].append(body)
        id_ = body["intent"]["id"]
        threading.Timer(0.5, ledger_write, (id_, state["payer"])).start()
        return {"id": id_, "txHash": "0x" + "11" * 32, "status": "SUBMITTED"}

    @app.get("/offramp/{id_}")
    def status(id_: str):
        return {"id": id_, "status": "COMPLETED"}

    @app.get("/__state")
    def get_state():
        return state

    @app.post("/__reset")
    async def reset(request: Request):
        body = await request.json()
        state.update(quotes=[], submits=[], payer=body.get("payer", "paid"))
        return state

    return app


def serve(app, port: int) -> threading.Thread:
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    return thread


def main() -> None:
    store = FastAPI()
    store.mount("/", StaticFiles(directory=ROOT / "apps" / "mock-store", html=True))
    def decider(goal: str):
        real = Settings.load()  # E2E_OPENAI=1 tries the real model against the fake off-ramp
        return OpenAIDecider(real.openai_api_key, real.openai_model) if os.environ.get("E2E_OPENAI") else RuleDecider(goal)

    settings = Settings(backend_url=FAKE_URL, signer="local", local_signer_key=TEST_KEY, decider="rule", global_max_inr=80,
                        allowed_domains=(os.environ.get("E2E_DOMAINS") or "127.0.0.1").split(","), payer_ledger=str(LEDGER), webhook_url="http://127.0.0.1:8790/webhooks/test")
    agent = create_app(settings, client=OfframpClient(FAKE_URL), signer=LocalKeySigner(TEST_KEY), results=LedgerResults(str(LEDGER)),
                       decider_factory=decider, machine_options={"poll_sec": 0.3, "wait_sec": 0.3, "result_wait_sec": 6, "time_limit_sec": 300})
    serve(store, 5181)
    serve(fake_offramp(), 8791)
    serve(agent, 8790)
    print("e2e stack ready", flush=True)
    threading.Event().wait()


if __name__ == "__main__":
    main()
