#!/usr/bin/env python3
"""Everything the extension's browser tests talk to, with nothing real behind it.

Serves the mock store on :5181, a fake off-ramp on :8791, and the real agent server on :8790, reading a throwaway payer
ledger. Each agent gets a throwaway local key in place of a Circle wallet, so no testnet, wallet or phone is involved.

    uv run python -m tests.e2e_stack
"""

from __future__ import annotations

import os
import re
import socket
import sqlite3
import tempfile
import threading
import time
from pathlib import Path

import asyncio
import json

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from eth_account import Account

from app.deciders.openai_vision import OpenAIDecider
from app.deciders.rule_based import RuleDecider
from app.main import create_app
from app.models import Action
from app.payment.offramp_client import OfframpClient
from app.payment.results import LedgerResults
from app.settings import Settings
from app.signers.local_key import LocalKeySigner
from app.store import AgentStore

from .test_payment import CHAIN, TOKEN, make_quote

ROOT = Path(__file__).resolve().parents[2]
FAKE_URL = "http://127.0.0.1:8791"
LEDGER = Path(tempfile.mkdtemp(prefix="agent-e2e-")) / "payer.sqlite"
state = {"quotes": [], "submits": [], "payer": "paid"}
accounts: dict[str, dict] = {}
keys: dict[str, str] = {}


def account_of(address: str) -> dict:
    return accounts.setdefault(address.lower(), {"balance": "20000000", "allowance": "0", "balanceFormatted": "20.000000", "symbol": "USDC"})


class FakeAdmin:
    """Stands in for Circle. Wallets are throwaway local keys, and an approval takes effect at once."""

    def list_wallets(self) -> list[str]:
        return []

    def create_wallet(self, idempotency_key: str) -> str:
        account = Account.create()
        keys[account.address] = account.key.hex()
        return account.address

    def fund(self, address: str) -> None:
        account_of(address)["balance"] = "20000000"

    def approve(self, address: str, token: str, spender: str, amount: int) -> None:
        account_of(address)["allowance"] = str(amount)


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
        return account_of(address)

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
        account = account_of(body["intent"]["user"])
        account["allowance"] = str(int(account["allowance"]) - int(body["intent"]["amount"]))
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


class LabelScript:
    """Follows a prompt like SCRIPT:["click:Foodie","back","need_help"], so browser tests can check an action without a
    model. Finds a click's target by its label, because candidate ids depend on the page's layout."""
    name = "script"

    def __init__(self, prompt: str) -> None:
        self.steps = json.loads(prompt.removeprefix("SCRIPT:"))

    def choose_action(self, state_text, candidates, screenshot):
        step = self.steps.pop(0) if self.steps else "need_help"
        kind, _, label = step.partition(":")
        if kind != "click":
            return Action(type=kind, reason="script", model=self.name)
        target = next(c for c in candidates if label in c.text)
        return Action(type="click", target=target.id, reason="script", model=self.name)

    def write_text(self, instruction, context):
        return ""


HOVER_STYLE = """<style>
.left { position: fixed; left: 0; top: 0; width: 50vw; height: 100vh; display: block; background: #eee; }
#add { position: fixed; left: 60vw; top: 40vh; width: 30vw; height: 80px; }
#zoom { position: fixed; left: 50vw; top: 0; width: 50vw; height: 100vh; background: #ccc; z-index: 5; }
</style>"""


def serve(app, port: int) -> threading.Thread:
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    return thread


def refuse_if_taken(*ports: int) -> None:
    """Browser tests delete every agent they find. If a real agent server held :8790, they would archive real agents
    and create real Circle wallets, so stop here instead of letting them reach it."""
    for port in ports:
        with socket.socket() as probe:
            if probe.connect_ex(("127.0.0.1", port)) == 0:
                raise SystemExit(f"port {port} is in use. Stop whatever is on it, such as a real agent server, before running the browser tests.")


def main() -> None:
    refuse_if_taken(5181, 8791, 8790)
    store = FastAPI()

    @store.get("/slow/")
    async def slow_page():  # a start page that takes a while, so a new tab has no address yet
        await asyncio.sleep(3)
        return RedirectResponse("/")

    @store.get("/hover/", response_class=HTMLResponse)
    async def hover_start():
        return f"<!doctype html><title>Shop</title>{HOVER_STYLE}<a id=photo class=left href='/hover/product'>Open product</a>"

    @store.get("/hover/product", response_class=HTMLResponse)
    async def hover_product():  # the pointer is still over this photo from the last click, so its zoom starts open, until the mouse leaves
        return (f"<!doctype html><title>Product</title>{HOVER_STYLE}<div id=photo class=left>Photo</div><div id=zoom></div>"
                "<button id=add onclick=\"this.textContent='Added'\">Add to cart</button>"
                "<script>addEventListener('mousemove', (e) => { zoom.style.display = e.target.id === 'photo' ? 'block' : 'none'; });</script>")

    store.mount("/", StaticFiles(directory=ROOT / "apps" / "mock-store", html=True, check_dir=False))  # kept locally, not in git
    def decider(agent: dict, prompt: str):
        real = Settings.load()  # E2E_OPENAI=1 tries the real model against the fake off-ramp
        if os.environ.get("E2E_OPENAI"):
            return OpenAIDecider(real.openai_api_key, real.openai_model, instructions=agent["instructions"], profile=agent["profile"], base_url=real.openai_base)
        return LabelScript(prompt) if prompt.startswith("SCRIPT:") else RuleDecider(prompt)

    settings = Settings(backend_url=FAKE_URL, signer="local", decider="rule", global_max_inr=1000, payer_ledger=str(LEDGER), dev_loop=True,
                        webhook_url="http://127.0.0.1:8790/webhooks/test")
    agent = create_app(settings, client=OfframpClient(FAKE_URL), signer_for=lambda wallet: LocalKeySigner(keys[wallet]), results=LedgerResults(str(LEDGER)),
                       decider_factory=decider, store=AgentStore(":memory:"), admin=FakeAdmin(), wallet_options={"poll_sec": 0.1, "wait_sec": 5},
                       machine_options={"poll_sec": 0.3, "wait_sec": 0.3, "result_wait_sec": 6, "time_limit_sec": 300})
    serve(store, 5181)
    serve(fake_offramp(), 8791)
    serve(agent, 8790)
    print("e2e stack ready", flush=True)
    threading.Event().wait()


if __name__ == "__main__":
    main()
