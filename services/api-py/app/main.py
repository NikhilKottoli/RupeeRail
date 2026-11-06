"""FastAPI app. `uv run uvicorn app.main:app --port 8788` (env WORKERS=0 to skip chain workers),
or `uv run python -m app.main --port 8788 [--no-workers] [--dry-run]`.

Background tasks (when workers are on): FX poller, one listener per chain, the settler loop and
the keepers. The payout runner's boot recovery (DISPATCHING -> NEEDS_OPERATOR) runs first.
The FX poller also runs with workers off (it is read-only and /quote needs it) unless FX=0.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .api import router
from .chain import ChainError, DryRunError
from .common import ApiError
from .config import Settings

log = logging.getLogger("rupeerail")


def _setup_logging() -> None:
    if not logging.getLogger().handlers:
        logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"),
                            format="%(asctime)s %(levelname)s %(name)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("web3").setLevel(logging.WARNING)


async def startup_checks(ctx) -> None:
    """Read-only: chain ids and onchain roles vs our keys. Mismatches are logged, not fatal."""
    for k, c in ctx.clients.items():
        try:
            cid = await c.chain_id()
            if cid != ctx.chains[k].chain_id:
                log.error("[startup:%s] RPC chainId %s != expected %s", k, cid, ctx.chains[k].chain_id)
            qs, st = await asyncio.gather(c.call("quoteSigner"), c.call("settler"))
            ctx.onchain_roles[k] = {"quoteSigner": qs, "settler": st}
            if qs.lower() != ctx.keys.quote_signer.address.lower():
                log.error("[startup:%s] vault quoteSigner %s != our quote signer %s", k, qs, ctx.keys.quote_signer.address)
            if st.lower() != ctx.keys.settler.address.lower():
                log.error("[startup:%s] vault settler %s != our settler %s", k, st, ctx.keys.settler.address)
            log.info("[startup:%s] chainId %s ok; vault roles quoteSigner=%s settler=%s", k, cid, qs, st)
        except Exception as e:
            log.warning("[startup:%s] checks failed: %s", k, e)


def create_app(ctx: Any = None, *, workers: bool | None = None, fx: bool | None = None) -> FastAPI:
    _setup_logging()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        nonlocal ctx
        if ctx is None:
            from .context import build_context

            ctx = build_context(Settings())
        from .environment import assert_ready

        assert_ready(ctx.settings)  # dev/prod: a no-op in dev; prod won't start on a dev rail (app/environment.py)
        app.state.ctx = ctx
        ctx.started_at = time.time()
        run_workers = ctx.settings.workers if workers is None else workers
        run_fx = (os.environ.get("FX", "1") not in ("0", "false", "no")) if fx is None else fx
        ctx.settings.workers = run_workers
        log.info("RupeeRail API: chains=%s workers=%s dryRun=%s db=%s collect=%s", list(ctx.chains), run_workers,
                 ctx.settings.dry_run, ctx.settings.db_path, ctx.settings.collect_rail)
        if ctx.onramp.razorpay is not None:
            log.info("razorpay: %r, callback %s/onramp/razorpay/callback, webhook %s/onramp/razorpay/webhook",
                     ctx.onramp.razorpay.creds, ctx.settings.public_base_url, ctx.settings.public_base_url)
        log.info("addresses: %s", ctx.keys.addresses())
        tasks: list[asyncio.Task] = []
        if run_fx:
            tasks.append(asyncio.create_task(ctx.fx.run(), name="fx"))
        if run_workers:
            await startup_checks(ctx)
            flagged = ctx.runner.recover()
            if flagged:
                log.error("payout recovery flagged %d payment(s) NEEDS_OPERATOR", len(flagged))
            for k, lst in ctx.listeners.items():
                tasks.append(asyncio.create_task(lst.run(), name=f"listener:{k}"))
            tasks.append(asyncio.create_task(ctx.settler.run(ctx.settings.settler_tick_sec), name="settler"))
            tasks.append(asyncio.create_task(ctx.keeper.run(ctx.settings.keeper_tick_sec), name="keepers"))
            if ctx.onramp.razorpay is not None:
                tasks.append(asyncio.create_task(ctx.onramp.run(ctx.settings.onramp_tick_sec), name="onramp"))
        try:
            yield
        finally:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            for lst in ctx.listeners.values():
                for t in list(lst.tasks):
                    t.cancel()
            await ctx.fx.close()
            if ctx.onramp.razorpay is not None:
                await ctx.onramp.razorpay.close()
            for c in ctx.clients.values():
                close = getattr(c, "close", None)
                if close:
                    await close()

    app = FastAPI(title="RupeeRail API", version="0.1.0", lifespan=lifespan)
    app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
    app.include_router(router)

    @app.exception_handler(ApiError)
    async def _api_error(_: Request, e: ApiError):
        return JSONResponse(status_code=e.status, content=e.to_json())

    @app.exception_handler(ChainError)
    async def _chain_error(_: Request, e: ChainError):
        return JSONResponse(status_code=400, content={"error": "SIMULATION_FAILED", "message": str(e), "revert": e.to_json()})

    @app.exception_handler(DryRunError)
    async def _dry_run(_: Request, e: DryRunError):
        return JSONResponse(status_code=503, content={"error": "DRY_RUN", "message": str(e)})

    @app.exception_handler(RequestValidationError)
    async def _validation(_: Request, e: RequestValidationError):
        return JSONResponse(status_code=400, content={"error": "BAD_REQUEST", "message": "invalid request body",
                                                      "details": [{"loc": d.get("loc"), "msg": d.get("msg")}
                                                                  for d in e.errors()]})

    return app


app = create_app()


def cli() -> None:
    import argparse

    import uvicorn

    ap = argparse.ArgumentParser(description="RupeeRail API")
    ap.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8788")))
    ap.add_argument("--no-workers", action="store_true", help="API only: no listener/settler/keepers")
    ap.add_argument("--dry-run", action="store_true", help="simulate transactions but never broadcast")
    a = ap.parse_args()
    if a.no_workers:
        os.environ["WORKERS"] = "0"
    if a.dry_run:
        os.environ["DRY_RUN"] = "1"
    uvicorn.run("app.main:app", host=a.host, port=a.port)


if __name__ == "__main__":
    cli()
