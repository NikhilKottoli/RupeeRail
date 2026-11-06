"""Onramp (API v1 §3.8): an order priced on the ask side; once the INR has arrived, the settler pays
from the pool with vault.onramp(orderId, wallet, amount, inrPaise), within the vault's daily cap.

How "the INR arrived" is known depends on COLLECT_RAIL (app/environment.py):
  manual    an operator calls POST /admin/onramp/{id}/fiat-received
  razorpay  each order gets a Razorpay Payment Link; the browser callback and the webhook only *nudge*
            confirm_razorpay(), which fetches the link from Razorpay and pays only if it is paid in
            full, in INR, for this order. Nudges are recorded (notified_at) so reconcile() retries
            after a crash, a Razorpay API error or a failed transaction.

Order status: AWAITING_FIAT -> PAYING -> PAID, or AWAITING_FIAT -> EXPIRED (link expired/cancelled).
Paying twice is impossible: a per-order lock + DB compare-and-set here, and onrampDone(orderId) onchain.
"""

from __future__ import annotations

import asyncio
import logging
import time
from decimal import Decimal
from typing import Any

from eth_utils import is_address, to_checksum_address

from .chain import ChainError, DryRunError
from .commitment import hex32, random_bytes32
from .common import ApiError
from .config import ChainConfig, Settings
from .fx import FxFeed
from .quote_math import format_paise, format_units, onramp_amount, onramp_rate_e6, price_to_e6, rupees_to_paise
from .quote_service import two_dp
from .razorpay import RazorpayError
from .store import Store

log = logging.getLogger("rupeerail.onramp")

RAZORPAY_MIN_TTL_SEC = 15 * 60 + 60  # Razorpay wants expire_by >= 15 min ahead; keep a minute of slack
REFERENCE_LEN = 40  # Razorpay's reference_id limit; 40 hex chars of the 32-byte order id
FORGET_PENDING_AFTER_SEC = 3600  # stop reconciling a nudged-but-unpaid link this long after it expires


def order_view(o: dict, cfg: ChainConfig | None, collect_upi_id: str, link: dict | None = None) -> dict:
    return {
        "orderId": o["id"],
        "chain": o["chain"],
        "wallet": o["wallet"],
        "inrPaise": str(o["inr_paise"]),
        "amount": format_units(int(o["amount"]), cfg.token.decimals) if cfg else o["amount"],
        "amountBaseUnits": str(o["amount"]),
        "symbol": cfg.token.symbol if cfg else None,
        "rate": two_dp(Decimal(int(o["rate_e6"])) / Decimal(10**6)),
        "payTo": None if link else {"upiId": collect_upi_id, "note": f"RR-{o['id'][2:12]}"},
        "checkout": {
            "provider": "razorpay", "url": link["short_url"], "linkId": link["link_id"],
            "expiresAt": str(link["expires_at"]), "paymentId": link.get("payment_id"),
        } if link else None,
        "status": o["status"],
        "paidTx": o.get("tx"),
        "explorer": cfg.tx_url(o["tx"]) if (cfg and o.get("tx")) else None,
        "lastError": o.get("last_error"),
    }


def check_paid_link(remote: dict, link: dict, order: dict) -> tuple[str, str | None]:
    """Razorpay's view of a link -> ("paid", payment_id) | ("pending"|"closed"|"mismatch: ...", None)."""
    if remote.get("id") != link["link_id"] or remote.get("reference_id") != link["reference_id"]:
        return "mismatch: link id or reference does not match this order", None
    status = remote.get("status")
    if status in ("expired", "cancelled"):
        return "closed", None
    if status != "paid":
        return "pending", None
    want = int(order["inr_paise"])
    if remote.get("currency") != "INR" or int(remote.get("amount") or 0) != want \
            or int(remote.get("amount_paid") or 0) != want:
        return (f"mismatch: paid {remote.get('amount_paid')} {remote.get('currency')} "
                f"for an order of {want} INR paise"), None
    captured = [p for p in (remote.get("payments") or []) if p.get("status") == "captured"]
    return "paid", (captured[0].get("payment_id") if captured else None)


class OnrampService:
    def __init__(self, settings: Settings, chains: dict[str, ChainConfig], store: Store, fx: FxFeed,
                 settler: Any, clock=time.time, razorpay: Any = None) -> None:
        self.settings, self.chains, self.store, self.fx, self.settler = settings, chains, store, fx, settler
        self.clock = clock
        self.razorpay = razorpay  # a RazorpayClient when COLLECT_RAIL=razorpay, else None (manual)
        self.last_error: str | None = None
        self._locks: dict[str, asyncio.Lock] = {}

    def _lock(self, order_id: str) -> asyncio.Lock:
        return self._locks.setdefault(order_id.lower(), asyncio.Lock())

    def view(self, o: dict) -> dict:
        return order_view(o, self.chains.get(o["chain"]), self.settings.collect_upi_id,
                          self.store.get_razorpay_link(o["id"]))

    # ---- create ---------------------------------------------------------------------------------
    async def create(self, chain: str, wallet: str, inr: str | int | float) -> dict:
        cfg = self.chains.get(chain)
        if cfg is None:
            raise ApiError(400, "BAD_CHAIN", f"chain must be one of {sorted(self.chains)}")
        if not isinstance(wallet, str) or not is_address(wallet):
            raise ApiError(400, "BAD_REQUEST", "wallet must be a 0x address")
        try:
            inr_paise = rupees_to_paise(inr)
        except ValueError as e:
            raise ApiError(400, "BAD_REQUEST", str(e)) from None
        s = self.settings
        if inr_paise < s.min_inr_paise or inr_paise > s.max_inr_paise:
            raise ApiError(400, "AMOUNT_OUT_OF_RANGE",
                           f"amount must be between Rs {format_paise(s.min_inr_paise)} and Rs {format_paise(s.max_inr_paise)}")
        rate = self.fx.get(cfg.fx_market)
        if rate is None:
            raise ApiError(503, "FX_STALE", f"no fresh {cfg.fx_market} rate; try again")
        rate_e6 = onramp_rate_e6(price_to_e6(rate.ask), s.spread_bps)
        amount = onramp_amount(inr_paise, cfg.token.decimals, rate_e6)
        if amount <= 0:
            raise ApiError(400, "AMOUNT_OUT_OF_RANGE", "amount rounds to zero tokens")
        order_id = hex32(random_bytes32())
        link = None
        if self.razorpay is not None:
            await self._check_capacity(chain, amount)  # never take INR the vault can't pay out
            link = await self._create_link(order_id, cfg, inr_paise, amount)
        self.store.insert_onramp({
            "id": order_id, "chain": chain, "wallet": to_checksum_address(wallet), "inr_paise": inr_paise,
            "amount": str(amount), "rate_e6": str(rate_e6), "market": cfg.fx_market, "ask": rate.ask,
            "status": "AWAITING_FIAT",
        })
        if link:
            self.store.insert_razorpay_link(link)
        return self.view(self.store.get_onramp(order_id))

    async def _check_capacity(self, chain: str, amount: int) -> None:
        c = self.settler.clients[chain]
        try:
            pool, cap = await asyncio.gather(c.call("poolBalance"), c.call("remainingDailyCap"))
        except Exception as e:
            log.warning("[onramp:%s] capacity check failed: %s", chain, e)
            raise ApiError(503, "CHAIN_UNAVAILABLE", "can't read the vault right now; try again") from None
        if int(pool) < amount:
            raise ApiError(409, "POOL_INSUFFICIENT", "the onramp pool can't cover this amount right now")
        if int(cap) < amount:
            raise ApiError(409, "DAILY_CAP", "today's onramp limit is used up; try a smaller amount or tomorrow")

    async def _create_link(self, order_id: str, cfg: ChainConfig, inr_paise: int, amount: int) -> dict:
        expire_by = int(self.clock()) + max(self.settings.onramp_link_ttl_sec, RAZORPAY_MIN_TTL_SEC)
        reference_id = order_id[2:2 + REFERENCE_LEN]
        try:
            r = await self.razorpay.create_payment_link(
                amount_paise=inr_paise, reference_id=reference_id, expire_by=expire_by,
                description=f"Buy {format_units(amount, cfg.token.decimals)} {cfg.token.symbol} on {cfg.name}",
                callback_url=f"{self.settings.public_base_url}/onramp/razorpay/callback",
                notes={"orderId": order_id, "chain": cfg.key},
            )
        except RazorpayError as e:
            self.last_error = str(e)
            log.warning("[onramp] payment link for %s failed: %s", order_id[:10], e)
            raise ApiError(502, "PAYMENT_PROVIDER", "couldn't create the Razorpay payment link; try again") from None
        return {"order_id": order_id, "link_id": r["id"], "reference_id": reference_id,
                "short_url": r["short_url"], "expires_at": int(r.get("expire_by") or expire_by),
                "status": r.get("status") or "created"}

    # ---- pay ------------------------------------------------------------------------------------
    async def fiat_received(self, order_id: str, bank_ref: str | None = None) -> dict:
        """Operator path (manual rail), and the last step of confirm_razorpay."""
        async with self._lock(order_id):
            return await self._pay(order_id, bank_ref)

    async def _pay(self, order_id: str, bank_ref: str | None) -> dict:
        o = self.store.get_onramp(order_id)
        if o is None:
            raise ApiError(404, "UNKNOWN_ORDER", "no onramp order with this id")
        if o["status"] == "PAID":
            return {"orderId": o["id"], "status": "PAID", "txHash": o["tx"]}
        # PAYING may be retried (e.g. after an RPC error); onrampDone(orderId) onchain keeps it once-only.
        if not (self.store.cas_onramp(order_id, "AWAITING_FIAT", "PAYING", bank_ref=bank_ref)
                or o["status"] == "PAYING"):
            raise ApiError(409, "BAD_STATE", f"order is {o['status']}")
        try:
            outcome, tx = await self.settler.onramp(o["chain"], o["id"], o["wallet"], int(o["amount"]), int(o["inr_paise"]))
        except DryRunError as e:
            self.store.cas_onramp(order_id, "PAYING", "PAYING", last_error=str(e))
            raise ApiError(503, "DRY_RUN", str(e)) from None
        except ChainError as e:
            self.store.cas_onramp(order_id, "PAYING", "PAYING", last_error=str(e))
            raise ApiError(409, "SIMULATION_FAILED", str(e), revert=e.to_json()) from None
        self.store.cas_onramp(order_id, "PAYING", "PAID", tx=tx, last_error=None)
        log.info("[onramp] %s %s", order_id[:10], outcome)
        o = self.store.get_onramp(order_id)
        return {"orderId": o["id"], "status": "PAID", "txHash": o["tx"]}

    # ---- razorpay -------------------------------------------------------------------------------
    def note(self, link_id: str) -> dict | None:
        """A callback or webhook mentioned this link: remember it, so reconcile() follows it up."""
        link = self.store.get_razorpay_link_by_link_id(link_id)
        if link is not None:
            self.store.update_razorpay_link(link["order_id"], notified_at=int(self.clock()))
        return link

    async def confirm_razorpay(self, link_id: str) -> str:
        """Pays the order behind `link_id` iff Razorpay itself says the link is paid in full."""
        link = self.store.get_razorpay_link_by_link_id(link_id)
        if link is None or self.razorpay is None:
            return "unknown"
        order_id = link["order_id"]
        async with self._lock(order_id):
            o = self.store.get_onramp(order_id)
            if o["status"] not in ("AWAITING_FIAT", "PAYING"):
                self.store.update_razorpay_link(order_id, notified_at=None)
                return o["status"].lower()
            payment_id = link.get("payment_id")
            if o["status"] == "AWAITING_FIAT":
                try:
                    remote = await self.razorpay.fetch_payment_link(link_id)
                except RazorpayError as e:
                    self.last_error = str(e)
                    log.warning("[onramp] fetch %s failed: %s", link_id, e)
                    return "provider_error"  # still notified: reconcile() retries
                verdict, payment_id = check_paid_link(remote, link, o)
                self.store.update_razorpay_link(order_id, status=str(remote.get("status")),
                                                payment_id=payment_id or link.get("payment_id"))
                if verdict == "closed":
                    self.store.cas_onramp(order_id, "AWAITING_FIAT", "EXPIRED")
                    self.store.update_razorpay_link(order_id, notified_at=None)
                    return "expired"
                if verdict == "pending":
                    if self.clock() > int(link["expires_at"]) + FORGET_PENDING_AFTER_SEC:
                        self.store.update_razorpay_link(order_id, notified_at=None)
                    return "pending"
                if verdict != "paid":
                    log.error("[onramp] %s NOT paid out: %s", order_id[:10], verdict)
                    self.store.cas_onramp(order_id, "AWAITING_FIAT", "AWAITING_FIAT", last_error=verdict)
                    self.store.update_razorpay_link(order_id, notified_at=None)  # needs an operator
                    return "mismatch"
            try:
                await self._pay(order_id, payment_id)
            except ApiError as e:
                self.last_error = e.message
                log.warning("[onramp] pay %s failed (will retry): %s", order_id[:10], e.message)
                return "pay_failed"  # PAYING + still notified: reconcile() retries
            self.store.update_razorpay_link(order_id, notified_at=None)
            return "paid"

    async def reconcile(self) -> dict[str, str]:
        out: dict[str, str] = {}
        for link in self.store.razorpay_links_to_reconcile():
            if self._lock(link["order_id"]).locked():
                continue  # a callback/webhook is handling it right now
            out[link["order_id"]] = await self.confirm_razorpay(link["link_id"])
        return out

    async def run(self, interval: float) -> None:
        while True:
            try:
                await self.reconcile()
            except Exception as e:
                self.last_error = f"{type(e).__name__}: {e}"
                log.warning("[onramp] reconcile failed: %s", self.last_error)
            await asyncio.sleep(interval)
