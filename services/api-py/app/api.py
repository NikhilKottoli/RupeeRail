"""HTTP routes: API contract v1 (HANDOFF-payment-cycle-api-metamask.md, sections 3.1-3.8).

All integers in responses are decimal strings (except chainId inside EIP-712 domains, spreadBps
and diagnostic ages, which the contract shows as numbers). Errors are {"error": CODE, "message"}.

Privacy: offramp ids are public onchain, so GET /offramp/{id} returns `payee: null` unless the
caller presents the `viewToken` that /quote returned (or EXPOSE_PAYEE_BY_ID=1 is set). qrText is returned only to
a viewToken holder, because a merchant's QR carries fields such as `tr` that the phone needs to match the order.
The salt is never returned by any route.
"""

from __future__ import annotations

import asyncio
import json
import time
from decimal import Decimal
from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, BackgroundTasks, Header, Query, Request
from fastapi.responses import RedirectResponse, StreamingResponse
from pydantic import BaseModel, Field

from .common import ApiError
from .eip712 import EIP712_DOMAIN, TYPES_FOR_CLIENTS
from .qr import REFUND_REASONS
from .quote_service import domains
from .razorpay import LINK_EVENTS, verify_callback_signature, verify_webhook_signature

router = APIRouter()

Num = str | int

FINAL_TX_LABELS = ("refund", "reverse", "release", "resolve", "disputeTimeout", "reclaim", "claim_timeout")
PUBLIC_STATUSES = ("QUOTED", "SUBMITTED", "LOCKED", "COMPLETED", "DISPUTED", "RELEASED", "REFUNDED", "REVERSED")


class QuoteReq(BaseModel):
    chain: str
    user: str
    inr: str | int | float
    qrText: str = Field(max_length=4096)


class PermitModel(BaseModel):
    deadline: Num
    v: Num
    r: str
    s: str


class OfframpReq(BaseModel):
    chain: str
    intent: dict[str, Num]
    userSig: str = Field(max_length=4096)
    permit: PermitModel


class DisputeReq(BaseModel):
    chain: str
    id: str
    deadline: Num
    userSig: str = Field(max_length=4096)


class OnrampReq(BaseModel):
    chain: str
    wallet: str
    inr: str | int | float


class FiatReceivedReq(BaseModel):
    bankRef: str | None = Field(default=None, max_length=256)


class RefundReq(BaseModel):
    reason: int = 4


def ctx_of(request: Request):
    return request.app.state.ctx


def _s(v: Any) -> str | None:
    return None if v is None else str(v)


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _trim(price: str) -> str:
    return format(Decimal(price).normalize(), "f")


# ---- views ----------------------------------------------------------------------------------

def offramp_view(ctx, id_: str, p: dict | None, q: dict | None, *, view_token: str | None = None) -> dict:
    chain = (p or q)["chain"]
    cfg = ctx.chains.get(chain)
    if p and p["status"] not in ("NONE", None):
        status = p["status"]
    elif q:
        status = "QUOTED" if q["status"] == "QUOTED" else "SUBMITTED"
    else:
        status = "SUBMITTED"

    txs_raw = json.loads((p or {}).get("txs") or "{}")
    final = next((txs_raw[k] for k in FINAL_TX_LABELS if txs_raw.get(k)), None)
    txs = {"lock": txs_raw.get("lock") or (q or {}).get("offramp_tx"), "complete": txs_raw.get("complete"),
           "dispute": txs_raw.get("dispute"), "final": final}

    payee = None
    token_ok = bool(q and view_token and view_token == q.get("view_token"))
    if q and (ctx.settings.expose_payee_by_id or token_ok):
        payee = {"upiId": q["upi_id"], "name": q["payee_name"]}

    releasable_at = (p or {}).get("releasable_at")
    reason = (p or {}).get("refund_reason")
    src = p if (p and p.get("amount") is not None) else (q or {})
    return {
        "id": id_,
        "chain": chain,
        "user": (p or {}).get("user") or (q or {}).get("user"),
        "status": status,
        "payout": (p or {}).get("dispatch_state") or "NONE",
        "payee": payee,
        "qrText": q["qr_text"] if token_ok else None,
        "amount": _s(src.get("amount")),
        "inrPaise": _s(src.get("inr_paise")),
        "refundReason": _s(reason),
        "refundReasonName": REFUND_REASONS.get(reason) if reason else None,
        "timers": {
            "completableAt": _s((p or {}).get("completable_at")),
            "reclaimableAt": _s((p or {}).get("reclaimable_at")),
            "releasableAt": _s(releasable_at),  # dispute-window end only; never the arbiter deadline
            "disputeDeadline": _s((p or {}).get("dispute_deadline")) if status == "DISPUTED" else None,
        },
        "canDispute": bool(status == "COMPLETED" and releasable_at and time.time() < int(releasable_at)),
        "txs": txs,
        "explorer": cfg.tx_url(txs["lock"]) if (cfg and txs["lock"]) else None,
    }


def event_view(ctx, e: dict) -> dict:
    cfg = ctx.chains.get(e["chain"])
    return {"chain": e["chain"], "name": e["name"], "id": e.get("ref_id"), "block": str(e["block"]),
            "txHash": e["tx"], "explorer": cfg.tx_url(e["tx"]) if cfg else None, "args": e["args"]}


def chain_view(cfg) -> dict:
    vd, pd = domains(cfg)
    return {
        "chainId": cfg.chain_id, "name": cfg.name, "rpc": cfg.rpc, "explorer": cfg.explorer,
        "vault": vd["verifyingContract"],
        "token": {"symbol": cfg.token.symbol, "address": pd["verifyingContract"], "decimals": cfg.token.decimals},
        "vaultDomain": vd, "permitDomain": pd,
        "types": {"EIP712Domain": EIP712_DOMAIN, **TYPES_FOR_CLIENTS},
    }


# ---- meta -----------------------------------------------------------------------------------

@router.get("/health")
async def health(request: Request) -> dict:
    ctx = ctx_of(request)
    fx = {}
    for m, st in ctx.fx.status()["markets"].items():
        if st:
            fx[m] = {"bid": _trim(st["bid"]), "ask": _trim(st["ask"]), "ageSec": int(st["ageSec"])}
    return {
        "ok": True,
        "chains": list(ctx.chains),
        "paymaster": ctx.keys.paymaster.address,
        "settler": ctx.keys.settler.address,
        "quoteSigner": ctx.keys.quote_signer.address,
        "fx": fx,
        "diagnostics": {
            "uptimeSec": round(time.time() - ctx.started_at, 1) if ctx.started_at else None,
            "workers": ctx.settings.workers,
            "dryRun": ctx.settings.dry_run,
            "fx": ctx.fx.status(),
            "listeners": {k: lst.status() for k, lst in ctx.listeners.items()},
            "settlerLastError": ctx.settler.last_error,
            "keeperLastError": ctx.keeper.last_error,
            "collectRail": ctx.settings.collect_rail,
            "onrampLastError": ctx.onramp.last_error,
            "sseSubscribers": ctx.hub.subscribers,
            "onchainRoles": ctx.onchain_roles,
        },
    }


@router.get("/config")
async def config(request: Request) -> dict:
    ctx = ctx_of(request)
    return {
        "paymaster": ctx.keys.paymaster.address,
        "chains": {k: chain_view(cfg) for k, cfg in ctx.chains.items()},
        "quoteSigner": ctx.keys.quote_signer.address,
        "spreadBps": ctx.settings.spread_bps,
        "quoteTtlSec": ctx.settings.quote_ttl_sec,
        "limits": {k: str(v) for k, v in ctx.settings.limits().items()},
        "refundReasons": {str(k): v for k, v in REFUND_REASONS.items()},
        "statuses": list(PUBLIC_STATUSES),
        # The web app offers Buy only on the razorpay rail: the manual rail's UPI ID is a placeholder until set.
        "onramp": {"rail": ctx.settings.collect_rail,
                   "testMode": bool(ctx.onramp.razorpay and ctx.onramp.razorpay.creds.test_mode)},
    }


@router.get("/account/{chain}/{address}")
async def account(chain: str, address: str, request: Request) -> dict:
    """Token balance, permit nonce and vault allowance (saves the frontend an RPC round trip)."""
    from .quote_math import format_units

    ctx = ctx_of(request)
    if chain not in ctx.clients:
        raise ApiError(400, "BAD_CHAIN", f"chain must be one of {sorted(ctx.clients)}")
    c, cfg = ctx.clients[chain], ctx.chains[chain]
    try:
        bal, nonce, allowance = await asyncio.gather(c.token_balance(address), c.token_nonce(address),
                                                     c.token_allowance(address, cfg.vault))
    except ValueError as e:
        raise ApiError(400, "BAD_REQUEST", str(e)) from None
    return {"chain": chain, "address": address, "balance": str(bal), "permitNonce": str(nonce),
            "allowance": str(allowance), "balanceFormatted": format_units(bal, cfg.token.decimals),
            "symbol": cfg.token.symbol}


# ---- offramp --------------------------------------------------------------------------------

@router.post("/quote")
async def post_quote(body: QuoteReq, request: Request) -> dict:
    ctx = ctx_of(request)
    ctx.limiter.check(f"quote:{body.user.lower()}")
    ctx.limiter.check(f"ip:{_client_ip(request)}")
    return await ctx.quotes.quote(body.chain, body.user, body.inr, body.qrText)


@router.post("/offramp")
async def post_offramp(body: OfframpReq, request: Request) -> dict:
    ctx = ctx_of(request)
    ctx.limiter.check(f"offramp:{str(body.intent.get('user', '')).lower()}")
    return await ctx.paymaster.submit(body.chain, body.intent, body.userSig, body.permit.model_dump())


@router.get("/offramp/{id_}")
async def get_offramp(id_: str, request: Request, refresh: bool = False, viewToken: str | None = None,
                      x_view_token: str | None = Header(default=None)) -> dict:
    ctx = ctx_of(request)
    id_ = id_.lower()
    p, q = ctx.store.get_payment(id_), ctx.store.get_quote(id_)
    if p is None and q is None:
        raise ApiError(404, "NOT_FOUND", "unknown offramp id")
    out = offramp_view(ctx, id_, p, q, view_token=viewToken or x_view_token)
    if refresh and out["chain"] in ctx.clients:
        c = ctx.clients[out["chain"]]
        st, dl, now = await asyncio.gather(c.offramp_state(id_), c.deadlines(id_), c.chain_time())
        disputed = st.status_name == "DISPUTED"
        out["onchain"] = {
            "status": st.status_name, "user": st.user, "amount": str(st.amount), "inrPaise": str(st.inr_paise),
            "lockedAt": str(st.locked_at), "completedAt": str(st.completed_at), "qrCommitment": st.qr_commitment,
            "completableAt": str(dl.completable_at), "reclaimableAt": str(dl.reclaimable_at),
            "releasableAt": None if disputed else str(dl.releasable_at),
            "disputeDeadline": str(dl.releasable_at) if disputed else None,
            "chainTime": str(now),
        }
    return out


@router.get("/offramps")
async def list_offramps(request: Request, user: str | None = None, chain: str | None = None,
                        limit: int = Query(50, ge=1, le=500)) -> dict:
    ctx = ctx_of(request)
    items = []
    for p in ctx.store.list_payments(user=user, chain=chain, limit=limit):
        items.append(offramp_view(ctx, p["id"], p, ctx.store.get_quote(p["id"])))
    return {"items": items}


@router.post("/dispute")
async def post_dispute(body: DisputeReq, request: Request) -> dict:
    ctx = ctx_of(request)
    ctx.limiter.check(f"dispute:{_client_ip(request)}")
    return await ctx.disputes.relay(body.chain, body.id, body.deadline, body.userSig)


# ---- events ---------------------------------------------------------------------------------

@router.get("/events")
async def list_events(request: Request, chain: str | None = None, id: str | None = None,
                      limit: int = Query(100, ge=1, le=1000)) -> list[dict]:
    ctx = ctx_of(request)
    return [event_view(ctx, e) for e in ctx.store.list_events(chain=chain, ref_id=id, limit=limit)]


@router.get("/events/stream")
async def stream_events(request: Request, ping: float = 15.0) -> StreamingResponse:
    """SSE: `event:` is the contract event name, `data:` the /events item; `ping` every 15 s."""
    ctx = ctx_of(request)
    q = ctx.hub.subscribe()

    async def gen():
        try:
            yield "retry: 3000\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    ev = await asyncio.wait_for(q.get(), timeout=ping)
                except asyncio.TimeoutError:
                    yield f"event: ping\ndata: {json.dumps({'t': str(int(time.time()))})}\n\n"
                    continue
                if ev.get("type") != "chain_event":
                    continue  # internal notifications stay internal
                data = {k: v for k, v in ev.items() if k != "type"}
                yield f"event: {ev['name']}\ndata: {json.dumps(data, default=str)}\n\n"
        finally:
            ctx.hub.unsubscribe(q)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ---- onramp + admin -------------------------------------------------------------------------

def _require_admin(ctx, token: str | None) -> None:
    if not ctx.settings.admin_token:
        raise ApiError(503, "ADMIN_DISABLED", "ADMIN_TOKEN is not configured")
    if token != ctx.settings.admin_token:
        raise ApiError(401, "UNAUTHORIZED", "bad or missing x-admin-token")


@router.post("/onramp")
async def post_onramp(body: OnrampReq, request: Request) -> dict:
    ctx = ctx_of(request)
    ctx.limiter.check(f"onramp:{_client_ip(request)}")
    return await ctx.onramp.create(body.chain, body.wallet, body.inr)


# Razorpay (COLLECT_RAIL=razorpay). Both routes only *nudge* OnrampService.confirm_razorpay, which pays
# only after fetching the link from Razorpay. Declared before /onramp/{order_id}.

def _razorpay(ctx):
    if ctx.onramp.razorpay is None:
        raise ApiError(404, "RAIL_DISABLED", "the razorpay collect rail is not enabled")
    return ctx.onramp.razorpay


@router.get("/onramp/razorpay/callback")
async def razorpay_callback(request: Request, background: BackgroundTasks):
    """Razorpay redirects the payer's browser here after paying; we send them on to the web app."""
    ctx = ctx_of(request)
    rz = _razorpay(ctx)
    params = dict(request.query_params)
    verified = verify_callback_signature(params, rz.creds.key_secret)
    link = ctx.onramp.note(params["razorpay_payment_link_id"]) if verified else None
    if link is not None:
        background.add_task(ctx.onramp.confirm_razorpay, link["link_id"])
    order_id = link["order_id"] if link else None
    if not ctx.settings.web_app_url:
        return {"orderId": order_id, "verified": verified}
    query = urlencode({"onramp": order_id or "", "payment": "received" if link else "unverified"})
    return RedirectResponse(f"{ctx.settings.web_app_url}/?{query}", status_code=303)


@router.post("/onramp/razorpay/webhook")
async def razorpay_webhook(request: Request, background: BackgroundTasks,
                           x_razorpay_signature: str | None = Header(default=None),
                           x_razorpay_event_id: str | None = Header(default=None)) -> dict:
    """Server-to-server. Must answer 2xx fast, or Razorpay retries; payment runs after the response."""
    ctx = ctx_of(request)
    rz = _razorpay(ctx)
    raw = await request.body()  # the signature covers these exact bytes
    if not verify_webhook_signature(raw, x_razorpay_signature, rz.creds.webhook_secret):
        raise ApiError(400, "BAD_SIGNATURE", "webhook signature does not verify")
    try:
        event = json.loads(raw)
    except ValueError:
        raise ApiError(400, "BAD_REQUEST", "webhook body is not JSON") from None
    name = str(event.get("event") or "")
    if x_razorpay_event_id and not ctx.store.record_razorpay_event(x_razorpay_event_id[:128], name):
        return {"ok": True, "duplicate": True}
    link_id = (((event.get("payload") or {}).get("payment_link") or {}).get("entity") or {}).get("id")
    if name not in LINK_EVENTS or not isinstance(link_id, str):
        return {"ok": True, "ignored": name}
    link = ctx.onramp.note(link_id)
    if link is None:
        return {"ok": True, "ignored": "unknown link"}  # e.g. another backend's order on the same account
    background.add_task(ctx.onramp.confirm_razorpay, link_id)
    return {"ok": True}


@router.get("/onramp/{order_id}")
async def get_onramp(order_id: str, request: Request) -> dict:
    ctx = ctx_of(request)
    o = ctx.store.get_onramp(order_id)
    if o is None:
        raise ApiError(404, "UNKNOWN_ORDER", "unknown onramp order")
    return ctx.onramp.view(o)


@router.post("/admin/onramp/{order_id}/fiat-received")
async def admin_fiat_received(order_id: str, request: Request, body: FiatReceivedReq | None = None,
                              x_admin_token: str | None = Header(default=None)) -> dict:
    ctx = ctx_of(request)
    _require_admin(ctx, x_admin_token)
    return await ctx.onramp.fiat_received(order_id, (body.bankRef if body else None))


@router.post("/admin/offramp/{id_}/refund")
async def admin_refund(id_: str, body: RefundReq, request: Request,
                       x_admin_token: str | None = Header(default=None)) -> dict:
    ctx = ctx_of(request)
    _require_admin(ctx, x_admin_token)
    p = ctx.store.get_payment(id_)
    if p is None:
        raise ApiError(404, "NOT_FOUND", "unknown offramp id")
    if not 1 <= body.reason <= 4:
        raise ApiError(400, "BAD_REQUEST", "reason must be 1-4")
    return {"id": id_, "result": await ctx.settler.refund(p["chain"], id_, body.reason)}


@router.post("/admin/offramp/{id_}/reverse")
async def admin_reverse(id_: str, request: Request, x_admin_token: str | None = Header(default=None)) -> dict:
    ctx = ctx_of(request)
    _require_admin(ctx, x_admin_token)
    p = ctx.store.get_payment(id_)
    if p is None:
        raise ApiError(404, "NOT_FOUND", "unknown offramp id")
    return {"id": id_, "result": await ctx.settler.reverse(p["chain"], id_)}
