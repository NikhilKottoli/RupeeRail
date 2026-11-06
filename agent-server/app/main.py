"""The agent server. Bind it to localhost:

    uv run uvicorn app.main:create_app --factory --host 127.0.0.1 --port 8790

A browser sends an Origin header that a page can't forge, so only the extension is let in. Without this, any website
the user visits could connect to the WebSocket and drive a server that can spend money.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any, Callable

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .deciders.base import Decider
from .deciders.openai_vision import OpenAIDecider
from .deciders.rule_based import RuleDecider
from .machine import Machine
from .models import SessionRequest
from .payment.flow import PaymentFlow
from .payment.offramp_client import OfframpClient
from .payment.results import LedgerResults, PayoutResults
from .payment.verify import Constraints
from .session import AgentSession
from .settings import Settings
from .signers.base import Signer
from .signers.circle_cli import CircleCliSigner
from .signers.local_key import LocalKeySigner

log = logging.getLogger("rupeerail.agent")
EXCHANGE_TIMEOUT_SEC = 30.0


def origin_ok(origin: str | None, settings: Settings) -> bool:
    if origin is None:  # not a browser, so a local tool, which is already on this machine
        return True
    if settings.extension_id:
        return origin == f"chrome-extension://{settings.extension_id}"
    return origin.startswith("chrome-extension://")


def make_signer(settings: Settings) -> Signer:
    if settings.signer == "local":
        return LocalKeySigner(settings.local_signer_key)
    return CircleCliSigner(settings.circle_wallet)


def make_decider(settings: Settings, goal: str) -> Decider:
    if settings.decider == "rule" or (settings.decider == "auto" and not settings.openai_api_key):
        return RuleDecider(goal)
    if not settings.openai_api_key:
        raise HTTPException(503, "OPENAI_API_KEY is not set")
    return OpenAIDecider(settings.openai_api_key, settings.openai_model)


def create_app(settings: Settings | None = None, client: Any | None = None, signer: Signer | None = None,
               results: PayoutResults | None = None, decider_factory: Callable[[str], Decider] | None = None,
               machine_options: dict | None = None) -> FastAPI:
    settings = settings or Settings.load()
    client = client or OfframpClient(settings.backend_url)
    results = results or LedgerResults(settings.payer_ledger)
    signer = signer or make_signer(settings)
    decide = decider_factory or (lambda goal: make_decider(settings, goal))
    sessions: dict[str, AgentSession] = {}
    tasks: set[asyncio.Task] = set()
    webhooks: list[dict] = []

    app = FastAPI(title="RupeeRail agent")
    pattern = f"chrome-extension://{re.escape(settings.extension_id)}" if settings.extension_id else r"chrome-extension://.*"
    app.add_middleware(CORSMiddleware, allow_origin_regex=pattern, allow_methods=["*"], allow_headers=["*"])

    @app.middleware("http")
    async def block_websites(request: Request, call_next):
        if not origin_ok(request.headers.get("origin"), settings):
            return JSONResponse({"detail": "only the RupeeRail extension may use this server"}, status_code=403)
        return await call_next(request)

    def running() -> AgentSession | None:
        return next((s for s in sessions.values() if not s.result and s.state != "created"), None)

    @app.post("/sessions")
    async def start_session(body: SessionRequest) -> dict:
        if running():
            raise HTTPException(409, "a session is already running")
        domains = settings.allowed_domains
        if body.allowed_domains is not None:  # a session can narrow the sites, but never widen them
            domains = [d for d in body.allowed_domains if d in settings.allowed_domains]
            if not domains:
                raise HTTPException(400, "none of those sites are allowed")
        max_inr = min(body.max_inr, settings.global_max_inr)
        session = AgentSession(body.goal, max_inr * 100, domains)
        sessions[session.id] = session
        return {"session_id": session.id, "allowed_domains": domains, "max_inr": max_inr}

    def get_session(session_id: str) -> AgentSession:
        session = sessions.get(session_id)
        if not session:
            raise HTTPException(404, "unknown session")
        return session

    @app.get("/sessions/{session_id}")
    async def session_state(session_id: str) -> dict:
        s = get_session(session_id)
        return {"session_id": s.id, "goal": s.goal, "state": s.state, "result": s.result, "events": s.events}

    @app.post("/sessions/{session_id}/stop")
    async def stop_session(session_id: str) -> dict:
        s = get_session(session_id)
        s.stop()
        return {"session_id": s.id, "stopped": True}

    @app.get("/wallet")
    async def wallet() -> dict:
        address = signer.address()
        account = await asyncio.to_thread(client.account, settings.chain, address)
        return {"address": address, "chain": settings.chain, "symbol": account.get("symbol"), "balance": account.get("balanceFormatted"),
                "allowance": account.get("allowance"), "uses_allowance": signer.uses_allowance}

    @app.post("/webhooks/test")
    async def webhook_test(request: Request) -> dict:
        webhooks.append(await request.json())
        return {"received": True}

    @app.get("/webhooks/test")
    async def webhook_seen() -> list[dict]:
        return webhooks[-20:]

    @app.websocket("/sessions/{session_id}")
    async def session_socket(socket: WebSocket, session_id: str) -> None:
        if not origin_ok(socket.headers.get("origin"), settings):
            await socket.close(code=4403)
            return
        session = sessions.get(session_id)
        if not session or session.state != "created":
            await socket.close(code=4404)
            return
        await socket.accept()
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
            session.post(message)
            try:
                return await asyncio.wait_for(replies.get(), EXCHANGE_TIMEOUT_SEC)
            except asyncio.TimeoutError:
                return {"type": "error", "ok": False, "error": "the extension didn't respond"}

        session.attach(outgoing.put_nowait, loop)
        writing = asyncio.create_task(writer())
        session.state = "starting"

        def make_flow(s: AgentSession) -> PaymentFlow:
            return PaymentFlow(client, signer, settings.chain, Constraints(s.max_paise, s.allowed_domains),
                               emit=lambda m: s.emit("pay", m), should_stop=s.stopped.is_set)

        options = {"webhook_url": settings.webhook_url, "max_steps": settings.max_steps, "time_limit_sec": settings.time_limit_sec,
                   "result_wait_sec": settings.result_wait_sec, **(machine_options or {})}
        machine = Machine(session, exchange, decide(session.goal), client, make_flow, results, settings.chain, **options)

        async def drive() -> None:
            try:
                await machine.run()
            except Exception as e:
                log.exception("the agent failed")
                session.result = {"status": "FAILED", "detail": f"the agent failed: {type(e).__name__}"}
                session.emit("result", session.result["detail"], status="FAILED")
            session.post({"type": "finished", "result": session.result})

        task = asyncio.create_task(drive())
        tasks.add(task)
        task.add_done_callback(tasks.discard)
        try:
            while True:
                message = json.loads(await socket.receive_text())
                kind = message.get("type")
                if kind == "stop":
                    session.stop()
                elif kind == "resume":
                    session.resume()
                elif kind in ("observation", "action_result", "error"):
                    replies.put_nowait(message)
        except (WebSocketDisconnect, json.JSONDecodeError):
            session.stop()  # with no extension, nobody can see or stop it. A submitted payment still finishes
            replies.put_nowait({"type": "error", "ok": False, "error": "the extension disconnected"})
        finally:
            session.detach()
            writing.cancel()

    return app
