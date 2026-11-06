"""Onramp (API v1 §3.8): an order priced on the ask side; an operator confirms the INR arrived;
the settler pays from the pool with vault.onramp(orderId, wallet, amount, inrPaise), within
the vault's daily cap. Order status: AWAITING_FIAT -> PAYING -> PAID (or FAILED).
"""

from __future__ import annotations

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
from .store import Store

log = logging.getLogger("rupeerail.onramp")


def order_view(o: dict, cfg: ChainConfig | None, collect_upi_id: str) -> dict:
    return {
        "orderId": o["id"],
        "chain": o["chain"],
        "wallet": o["wallet"],
        "inrPaise": str(o["inr_paise"]),
        "amount": format_units(int(o["amount"]), cfg.token.decimals) if cfg else o["amount"],
        "amountBaseUnits": str(o["amount"]),
        "symbol": cfg.token.symbol if cfg else None,
        "rate": two_dp(Decimal(int(o["rate_e6"])) / Decimal(10**6)),
        "payTo": {"upiId": collect_upi_id, "note": f"RR-{o['id'][2:12]}"},
        "status": o["status"],
        "paidTx": o.get("tx"),
        "explorer": cfg.tx_url(o["tx"]) if (cfg and o.get("tx")) else None,
        "lastError": o.get("last_error"),
    }


class OnrampService:
    def __init__(self, settings: Settings, chains: dict[str, ChainConfig], store: Store, fx: FxFeed,
                 settler: Any, clock=time.time) -> None:
        self.settings, self.chains, self.store, self.fx, self.settler = settings, chains, store, fx, settler
        self.clock = clock

    def view(self, o: dict) -> dict:
        return order_view(o, self.chains.get(o["chain"]), self.settings.collect_upi_id)

    def create(self, chain: str, wallet: str, inr: str | int | float) -> dict:
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
        self.store.insert_onramp({
            "id": order_id, "chain": chain, "wallet": to_checksum_address(wallet), "inr_paise": inr_paise,
            "amount": str(amount), "rate_e6": str(rate_e6), "market": cfg.fx_market, "ask": rate.ask,
            "status": "AWAITING_FIAT",
        })
        return self.view(self.store.get_onramp(order_id))

    async def fiat_received(self, order_id: str, bank_ref: str | None = None) -> dict:
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
