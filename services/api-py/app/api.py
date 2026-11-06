"""HTTP routes: API contract v1. Read and quote endpoints: /health, /config, /account, /quote,
GET /offramp/{id} and /offramps.

All integers in responses are decimal strings (except chainId inside EIP-712 domains, spreadBps
and diagnostic ages, which the contract shows as numbers). Errors are {"error": CODE, "message"}.

Privacy: offramp ids are public onchain, so GET /offramp/{id} returns `payee: null` unless the
caller presents the `viewToken` that /quote returned (or EXPOSE_PAYEE_BY_ID=1 is set). qrText and
salt are never returned by any route.
"""

from __future__ import annotations

import asyncio
import json
import time
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Header, Query, Request
from pydantic import BaseModel, Field

from .common import ApiError
from .eip712 import EIP712_DOMAIN, TYPES_FOR_CLIENTS
from .qr import REFUND_REASONS
from .quote_service import domains

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
    if q and (ctx.settings.expose_payee_by_id or (view_token and view_token == q.get("view_token"))):
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

