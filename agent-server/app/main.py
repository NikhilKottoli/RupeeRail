"""The agent server. Bind it to localhost:

    uv run uvicorn app.main:create_app --factory --host 127.0.0.1 --port 8790

A browser sends an Origin header that a page can't forge, so only the extension is let in. Without this, any website
the user visits could connect to the WebSocket and drive a server that can spend money.

That only stops web pages, and any script can send any Origin. So a server reached through a proxy refuses everything
unless it has invite codes (AGENT_ACCESS_CODES) or was deliberately opened to everyone (AGENT_OPEN_ACCESS=1).
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import re
import threading
import urllib.error
import urllib.request
from decimal import Decimal
from typing import Any, Callable, Mapping

from fastapi import FastAPI, Header, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from .arkiv_outbox import ArkivOutbox
from .deciders.base import Decider
from .deciders.openai_vision import OpenAIDecider
from .deciders.rule_based import RuleDecider
from .machine import Machine
from .models import AgentCreate, AgentFields, AgentPatch, BudgetRequest, PayerResult, QueuedRehearsal, RunRequest
from .payment.flow import PaymentFlow
from .payment.offramp_client import OfframpClient
from .payment.results import LedgerResults, PayoutResults, ReportedResults
from .payment.verify import Constraints
from .run import AgentRun, profile_terms
from .settings import Settings
from .signers.base import Signer
from .signers.circle_admin import WalletAdmin, WalletAdminError
from .signers.circle_cli import CircleCliSigner
from .signers.local_key import LocalKeySigner
from .store import BUSY_WALLET_STATES, AgentStore
from .templates import AVATARS, CATEGORIES, COLOURS, TEMPLATES
from .wallets import BudgetTooHigh, WalletManager

log = logging.getLogger("rupeerail.agent")
EXCHANGE_TIMEOUT_SEC = 30.0
EXPLORER = "https://explorer.testnet.arc.io/address/"
# Only needed to edit one agent, so listing agents leaves them out.
PRIVATE_FIELDS = ("instructions", "profile")
PUBLIC_CARD_FIELDS = ("name", "avatar", "colour", "category")
PUBLIC_RECORD_TIMEOUT_SEC = 8.0
SOCKET_CODE_PREFIX = "rupeerail.code."  # a browser's WebSocket can't set headers, and a URL would end up in logs
PAYER_RESULTS_PATH = "/payer-results"


def origin_ok(origin: str | None, settings: Settings) -> bool:
    if origin is None:  # not a browser, so a local tool, which is already on this machine
        return True
    if settings.extension_id:
        return origin == f"chrome-extension://{settings.extension_id}"
    return origin.startswith("chrome-extension://")


def same_secret(given: str | None, expected: str) -> bool:
    return bool(given) and hmac.compare_digest(given.encode(), expected.encode())


def bearer(authorization: str | None) -> str | None:
    scheme, _, value = (authorization or "").partition(" ")
    return value.strip() if scheme.lower() == "bearer" else None


def admitted(code: str | None, headers: Mapping[str, str], settings: Settings) -> bool:
    if settings.access_codes:
        return any(same_secret(code, known) for known in settings.access_codes)
    return settings.open_access or "x-forwarded-for" not in headers  # otherwise, forgetting the codes would open it to the internet


def socket_code(socket: WebSocket) -> str | None:
    for offered in socket.scope.get("subprotocols", []):
        if offered.startswith(SOCKET_CODE_PREFIX):
            return offered.removeprefix(SOCKET_CODE_PREFIX)
    return None


def make_signer(settings: Settings, wallet: str) -> Signer:
    if settings.signer == "local":
        return LocalKeySigner(settings.local_signer_key)
    return CircleCliSigner(wallet)


def make_decider(settings: Settings, agent: dict, prompt: str) -> Decider:
    if settings.decider == "rule" or (settings.decider == "auto" and not settings.openai_api_key):
        return RuleDecider(prompt)
    if not settings.openai_api_key:
        raise HTTPException(503, "OPENAI_API_KEY is not set")
    return OpenAIDecider(settings.openai_api_key, settings.openai_model, instructions=agent["instructions"], profile=agent["profile"], base_url=settings.openai_base)


def read_public_record(backend_url: str, wallet: str) -> dict | None:
    """The agent's record as the public reader serves it, read from Arkiv. None if it has no card there."""
    try:
        with urllib.request.urlopen(f"{backend_url}/arkiv/agents/{wallet}", timeout=PUBLIC_RECORD_TIMEOUT_SEC) as response:
            return json.load(response)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise


def start_thread(work: Callable[[], None]) -> None:
    threading.Thread(target=work, daemon=True).start()


def create_app(settings: Settings | None = None, client: Any | None = None, signer_for: Callable[[str], Signer] | None = None,
               results: PayoutResults | None = None, decider_factory: Callable[[dict, str], Decider] | None = None,
               machine_options: dict | None = None, store: AgentStore | None = None, admin: WalletAdmin | None = None,
               background: Callable[[Callable[[], None]], None] = start_thread, wallet_options: dict | None = None,
               outbox: ArkivOutbox | None = None, public_record: Callable[[str], dict | None] | None = None) -> FastAPI:
    settings = settings or Settings.load()
    client = client or OfframpClient(settings.backend_url)
    signer_for = signer_for or (lambda wallet: make_signer(settings, wallet))
    decide = decider_factory or (lambda agent, prompt: make_decider(settings, agent, prompt))
    store = store or AgentStore(settings.db_path)
    results = results or (ReportedResults(store.payer_result) if settings.payer_results_token else LedgerResults(settings.payer_ledger))
    wallets = WalletManager(store, admin or WalletAdmin(), client, settings.chain, settings.max_budget_usdc, **(wallet_options or {}))
    if outbox is None:
        outbox = ArkivOutbox(store, settings.arkiv_publisher_url, settings.arkiv_publisher_token, settings.chain)
        outbox.start()
    read_record = public_record or (lambda wallet: read_public_record(settings.backend_url, wallet))
    active: dict[str, AgentRun] = {}
    deciders: dict[str, Decider] = {}
    tasks: set[asyncio.Task] = set()
    webhooks: list[dict] = []
    queued: list[dict] = []
    reload_extension = {"wanted": False}

    app = FastAPI(title="RupeeRail agent")
    app.state.results = results
    pattern = f"chrome-extension://{re.escape(settings.extension_id)}" if settings.extension_id else r"chrome-extension://.*"
    app.add_middleware(CORSMiddleware, allow_origin_regex=pattern, allow_methods=["*"], allow_headers=["*"])

    @app.middleware("http")
    async def block_websites(request: Request, call_next):
        if not origin_ok(request.headers.get("origin"), settings):
            return JSONResponse({"detail": "only the RupeeRail extension may use this server"}, status_code=403)
        # A browser sends a preflight without credentials, and it reaches no route. The payer has its own token.
        checked = request.method != "OPTIONS" and request.url.path != PAYER_RESULTS_PATH
        if checked and not admitted(bearer(request.headers.get("authorization")), request.headers, settings):
            return JSONResponse({"detail": "This agent server isn't open to you."}, status_code=401)
        return await call_next(request)

    def running() -> AgentRun | None:
        return next((r for r in active.values() if not r.result and r.state != "created"), None)

    def get_agent(agent_id: str) -> dict:
        agent = store.agent(agent_id)
        if not agent:
            raise HTTPException(404, "unknown agent")
        return agent

    def live_agent(agent_id: str) -> dict:
        agent = get_agent(agent_id)
        if agent["archived"]:
            raise HTTPException(409, "this agent is archived")
        return agent

    def get_run(run_id: str) -> AgentRun:
        run = active.get(run_id)
        if not run:
            raise HTTPException(404, "unknown run")
        return run

    def check_limit(per_order_max_inr: int) -> None:
        if per_order_max_inr > settings.global_max_inr:
            raise HTTPException(400, f"the most an agent's limit can be is Rs {settings.global_max_inr}")

    def wallet_view(address: str | None) -> dict | None:
        if not address:
            return None
        view: dict = {"address": address, "explorer": f"{EXPLORER}{address}", "chain": settings.chain}
        try:
            token = client.config()["chains"][settings.chain]["token"]
            account = client.account(settings.chain, address)
            view.update(symbol=account.get("symbol"), balance=account.get("balanceFormatted"),
                        budget_left=str(Decimal(account.get("allowance", "0")) / 10 ** int(token.get("decimals", 6))))
        except Exception as e:  # a card still shows the agent if the backend can't be reached
            log.warning("couldn't read a wallet: %s", type(e).__name__)
            view["unavailable"] = True
        return view

    async def agent_view(agent: dict, private: bool = False) -> dict:
        view = {k: v for k, v in agent.items() if private or k not in PRIVATE_FIELDS}
        view["wallet"] = await asyncio.to_thread(wallet_view, agent["wallet"])
        return view

    @app.post(PAYER_RESULTS_PATH, status_code=202)
    async def payer_result(body: PayerResult, authorization: str | None = Header(default=None)) -> dict:
        """How a payer on another machine says how a payment ended. An invite code can't do this, or a guest could claim a payment was made."""
        if not settings.payer_results_token:
            raise HTTPException(404, "this server reads the payer's ledger")
        if not same_secret(bearer(authorization), settings.payer_results_token):
            raise HTTPException(401, "wrong token")
        store.save_payer_result(body.key, body.status)
        return {"saved": True}

    @app.get("/templates")
    async def templates() -> dict:
        return {"templates": TEMPLATES, "avatars": AVATARS, "colours": COLOURS, "categories": CATEGORIES, "global_max_inr": settings.global_max_inr, "max_budget_usdc": str(settings.max_budget_usdc)}

    @app.get("/agents")
    async def list_agents() -> list[dict]:
        return list(await asyncio.gather(*(agent_view(a) for a in store.agents())))

    @app.post("/agents", status_code=201)
    async def create_agent(body: AgentCreate) -> dict:
        check_limit(body.per_order_max_inr)
        try:
            wallets.check_budget(body.budget_usdc)
        except BudgetTooHigh as e:
            raise HTTPException(400, str(e)) from None
        agent = store.create_agent(body.model_dump(exclude={"budget_usdc"}), str(body.budget_usdc))
        background(lambda: wallets.setup(agent["id"]))
        return await agent_view(store.agent(agent["id"]), private=True)

    @app.get("/agents/{agent_id}")
    async def read_agent(agent_id: str) -> dict:
        return await agent_view(get_agent(agent_id), private=True)

    @app.patch("/agents/{agent_id}")
    async def edit_agent(agent_id: str, body: AgentPatch) -> dict:
        agent = live_agent(agent_id)
        changes = body.model_dump(exclude_unset=True, exclude_none=True)
        try:
            merged = AgentFields(**{**{k: agent[k] for k in AgentFields.model_fields}, **changes})
        except ValidationError as e:
            raise HTTPException(422, "; ".join(error["msg"] for error in e.errors())) from None
        check_limit(merged.per_order_max_inr)
        saved = store.update_agent(agent_id, merged.model_dump())
        if agent["published"] and not saved["published"]:
            outbox.withdraw(saved, receipts=True)
        elif saved["published"] and not agent["published"]:
            outbox.publish_history(saved)
        elif saved["published"] and any(agent[k] != saved[k] for k in PUBLIC_CARD_FIELDS):
            outbox.card_changed(saved)
        return await agent_view(saved, private=True)

    @app.delete("/agents/{agent_id}")
    async def archive_agent(agent_id: str) -> dict:
        agent = live_agent(agent_id)
        if agent["wallet_status"] in BUSY_WALLET_STATES:  # setup would finish afterwards and put the budget back
            raise HTTPException(409, "the wallet is still being set up")
        current = running()
        if current and current.agent_id == agent_id:
            raise HTTPException(409, "this agent is running")
        try:
            await asyncio.to_thread(wallets.revoke, agent_id)
        except Exception as e:
            detail = str(e) if isinstance(e, WalletAdminError) else type(e).__name__
            raise HTTPException(502, f"the budget couldn't be revoked, so the agent wasn't archived: {detail}") from None
        store.archive_agent(agent_id)
        if agent["published"]:
            outbox.withdraw(agent, receipts=False)  # its receipts stay until they expire, as a record of what it did
        return {"archived": True}

    @app.post("/agents/{agent_id}/budget")
    async def set_budget(agent_id: str, body: BudgetRequest) -> dict:
        agent = live_agent(agent_id)
        if agent["wallet_status"] in BUSY_WALLET_STATES:
            raise HTTPException(409, "the wallet is still being set up")
        try:
            wallets.check_budget(body.usdc)
        except BudgetTooHigh as e:
            raise HTTPException(400, str(e)) from None
        store.set_budget(agent_id, str(body.usdc))
        store.set_wallet_state(agent_id, "approving", "setting the on-chain budget")
        background(lambda: wallets.setup(agent_id))
        return await agent_view(store.agent(agent_id))

    @app.post("/agents/{agent_id}/fund")
    async def retry_setup(agent_id: str) -> dict:
        """Asks the faucet again if the wallet is short, then carries on with setup."""
        agent = live_agent(agent_id)
        if agent["wallet_status"] in BUSY_WALLET_STATES:
            raise HTTPException(409, "the wallet is still being set up")
        store.set_wallet_state(agent_id, "pending")
        background(lambda: wallets.setup(agent_id))
        return await agent_view(store.agent(agent_id))

    @app.get("/agents/{agent_id}/public-record")
    async def public_record_view(agent_id: str) -> dict:
        """What anyone can see about this agent, read back from Arkiv and not from this server's own database."""
        agent = get_agent(agent_id)
        if not agent["published"] or not agent["wallet"]:
            return {"published": False}
        page = f"{settings.backend_url}/agents/{agent['wallet']}"
        try:
            record = await asyncio.to_thread(read_record, agent["wallet"])
        except Exception as e:
            log.warning("couldn't read the public record: %s", type(e).__name__)
            return {"published": True, "unavailable": True, "page": page}
        if record is None:
            return {"published": True, "listed": False, "page": page}
        return {"published": True, "listed": True, "page": page, "stats": record.get("stats"), "expires_at": record.get("expiresAt"), "working": record.get("working")}

    @app.get("/agents/{agent_id}/runs")
    async def agent_runs(agent_id: str) -> list[dict]:
        get_agent(agent_id)
        return store.runs(agent_id)

    @app.post("/agents/{agent_id}/runs", status_code=201)
    async def start_run(agent_id: str, body: RunRequest) -> dict:
        agent = live_agent(agent_id)
        if running():
            raise HTTPException(409, "an agent is already running")
        if not body.rehearse and agent["wallet_status"] != "ready":
            raise HTTPException(409, f"{agent['name']}'s wallet isn't ready. A rehearsal doesn't need one")
        limits = (agent["per_order_max_inr"], settings.global_max_inr, *([body.max_inr] if body.max_inr else []))
        max_inr = min(limits)
        run = AgentRun(body.prompt, max_inr * 100, agent["sites"], agent["id"], agent["name"], agent["wallet"] or "", body.rehearse,
                       store.past_runs_text(agent["id"]), profile_terms(agent["profile"]))
        deciders[run.id] = decide(agent, body.prompt)  # here, so a missing key is reported before anything starts
        active[run.id] = run
        return {"run_id": run.id, "allowed_domains": agent["sites"], "start_url": agent["start_url"], "max_inr": max_inr, "rehearse": body.rehearse}

    if settings.dev_loop:
        # A local tool's way to start runs, for debugging on real sites from a person's own browser. There's deliberately
        # no way to ask for anything but a rehearsal here, so it can't order or pay.
        @app.post("/dev/rehearsals", status_code=202)
        async def queue_rehearsal(body: QueuedRehearsal) -> dict:
            agent = live_agent(body.agent_id)
            queued.append({"agent_id": agent["id"], "prompt": body.prompt, "max_inr": body.max_inr or agent["per_order_max_inr"],
                           "agent": {"name": agent["name"], "avatar": agent["avatar"], "colour": agent["colour"]}})
            return {"queued": len(queued)}

        @app.post("/dev/reload", status_code=202)
        async def queue_reload() -> dict:
            reload_extension["wanted"] = True
            return {"queued": True}

        @app.get("/dev/next")
        async def next_rehearsal() -> dict:
            if reload_extension["wanted"]:
                reload_extension["wanted"] = False
                return {"reload": True}
            return queued.pop(0) if queued and not running() else {}

    @app.get("/runs/{run_id}")
    async def run_state(run_id: str) -> dict:
        r = get_run(run_id)
        return {"run_id": r.id, "agent_id": r.agent_id, "prompt": r.prompt, "state": r.state, "result": r.result, "events": r.events}

    @app.post("/runs/{run_id}/stop")
    async def stop_run(run_id: str) -> dict:
        r = get_run(run_id)
        r.stop()
        return {"run_id": r.id, "stopped": True}

    @app.post("/webhooks/test")
    async def webhook_test(request: Request) -> dict:
        webhooks.append(await request.json())
        return {"received": True}

    @app.get("/webhooks/test")
    async def webhook_seen() -> list[dict]:
        return webhooks[-20:]

    @app.websocket("/runs/{run_id}")
    async def run_socket(socket: WebSocket, run_id: str) -> None:
        code = socket_code(socket)
        if not origin_ok(socket.headers.get("origin"), settings) or not admitted(code, socket.headers, settings):
            await socket.close(code=4403)
            return
        run = active.get(run_id)
        if not run or run.state != "created":
            await socket.close(code=4404)
            return
        if running():  # one phone and one tab, so one agent at a time
            await socket.close(code=4409)
            return
        await socket.accept(subprotocol=f"{SOCKET_CODE_PREFIX}{code}" if code else None)  # a browser fails the socket unless it's echoed
        loop = asyncio.get_running_loop()
        replies: asyncio.Queue[dict] = asyncio.Queue()
        outgoing: asyncio.Queue[dict] = asyncio.Queue()  # one writer, so messages keep their order

        async def writer() -> None:
            while True:
                message = await outgoing.get()
                try:
                    await socket.send_text(json.dumps(message))
                except (RuntimeError, WebSocketDisconnect):
                    return

        async def exchange(message: dict) -> dict:
            run.post(message)
            try:
                return await asyncio.wait_for(replies.get(), EXCHANGE_TIMEOUT_SEC)
            except asyncio.TimeoutError:
                return {"type": "error", "ok": False, "error": "the extension didn't respond"}

        run.attach(outgoing.put_nowait, loop)
        writing = asyncio.create_task(writer())
        run.state = "starting"
        store.start_run(run.id, run.agent_id, run.prompt, run.rehearse, run.max_paise)
        if not run.rehearse:
            outbox.run_started(store.agent(run.agent_id))

        def on_submit(offramp_id: str) -> None:
            try:
                store.note_submitted(run.id, offramp_id)
            except Exception as e:  # a history write must never affect a payment
                log.warning("couldn't record a submission: %s", type(e).__name__)

        def make_flow(r: AgentRun) -> PaymentFlow:
            return PaymentFlow(client, signer_for(r.wallet), settings.chain, Constraints(r.max_paise, r.allowed_domains),
                               emit=lambda m: r.emit("pay", m), should_stop=r.stopped.is_set, on_submit=on_submit)

        options = {"webhook_url": settings.webhook_url, "max_steps": settings.max_steps, "time_limit_sec": settings.time_limit_sec,
                   "result_wait_sec": settings.result_wait_sec, "debug_dir": settings.debug_dir, **(machine_options or {})}
        machine = Machine(run, exchange, deciders.pop(run.id), client, make_flow, results, settings.chain, **options)

        async def drive() -> None:
            try:
                await machine.run()
            except Exception as e:
                log.exception("the agent failed")
                run.result = {"status": "FAILED", "detail": f"the agent failed: {type(e).__name__}"}
                run.emit("result", run.result["detail"], status="FAILED")
            r = run.result
            amount = r.get("amount_inr")
            store.finish_run(run.id, r.get("status", "FAILED"), r.get("detail", ""), run.site, round(amount * 100) if amount is not None else None,
                             r.get("offramp_id"), r.get("txHash"))
            if not run.rehearse:
                outbox.run_finished(store.agent(run.agent_id), store.run(run.id))
            run.post({"type": "finished", "result": run.result})

        task = asyncio.create_task(drive())
        tasks.add(task)
        task.add_done_callback(tasks.discard)
        try:
            while True:
                message = json.loads(await socket.receive_text())
                kind = message.get("type")
                if kind == "stop":
                    run.stop()
                elif kind == "resume":
                    run.resume()
                elif kind in ("observation", "action_result", "error"):
                    replies.put_nowait(message)
        except (WebSocketDisconnect, json.JSONDecodeError):
            run.stop()  # with no extension, nobody can see or stop it. A submitted payment still finishes
            replies.put_nowait({"type": "error", "ok": False, "error": "the extension disconnected"})
        finally:
            run.detach()
            writing.cancel()

    return app
