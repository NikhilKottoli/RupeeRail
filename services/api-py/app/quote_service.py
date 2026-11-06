"""POST /quote (API v1 §3.3): validate the QR, price it, sign hashIntent as the quote signer,
store everything, and return ready-to-sign EIP-712 payloads for the wallet.

This is where per-payment limits and the per-user daily cap are enforced (KYC/residency would
go here too). The only RPC call is a read of token.nonces(user) for the pre-filled permit; no
transaction is sent.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from eth_utils import is_address, to_checksum_address

from .commitment import hex32, qr_commitment, random_bytes32
from .common import ApiError
from .config import ChainConfig, Settings
from .eip712 import (
    EIP712_DOMAIN, OFFRAMP_INTENT_TYPE, PERMIT_TYPE, OfframpIntent, intent_typed_data, permit_domain,
    sign_digest_typed, vault_domain,
)
from .fx import FxFeed
from .qr import REFUND_REASONS, parse_upi_qr
from .quote_math import format_paise, format_units, offramp_amount, offramp_rate_e6, price_to_e6, rupees_to_paise
from .store import Store

DAY = 86_400


def derive_view_token(secret: str, id_b: bytes) -> str:
    """The quote's viewToken. With a shared secret, the payout agent can compute it from the event's id.

    Without one, it stays random and only the /quote caller can read the payee.
    """
    if not secret:
        return secrets.token_hex(16)
    return hmac.new(secret.encode(), id_b, hashlib.sha256).hexdigest()


def domains(cfg: ChainConfig) -> tuple[dict, dict]:
    vd = vault_domain(cfg.chain_id, cfg.vault)
    pd = permit_domain(cfg.token.permit_name, cfg.token.permit_version, cfg.chain_id, cfg.token.address)
    return vd, pd


def two_dp(v: Any) -> str | None:
    if v is None:
        return None
    return str(Decimal(str(v)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def signing_payloads(cfg: ChainConfig, intent: OfframpIntent) -> dict:
    """The two eth_signTypedData_v4 objects, with every integer as a decimal string."""
    vd, pd = domains(cfg)
    return {
        "permit": {
            "domain": pd,
            "types": {"EIP712Domain": EIP712_DOMAIN, "Permit": PERMIT_TYPE},
            "primaryType": "Permit",
            "message": None,  # filled by the caller (needs the token nonce)
        },
        "intent": {
            "domain": vd,
            "types": {"EIP712Domain": EIP712_DOMAIN, "OfframpIntent": OFFRAMP_INTENT_TYPE},
            "primaryType": "OfframpIntent",
            "message": intent.to_json(),
        },
    }


class QuoteService:
    def __init__(self, settings: Settings, chains: dict[str, ChainConfig], store: Store, fx: FxFeed,
                 quote_signer: Any, clients: dict[str, Any] | None = None, clock=time.time) -> None:
        self.settings, self.chains, self.store, self.fx = settings, chains, store, fx
        self.signer = quote_signer
        self.clients = clients or {}
        self.clock = clock

    async def quote(self, chain: str, user: str, inr: str | int | float, qr_text: str) -> dict:
        cfg = self.chains.get(chain)
        if cfg is None or chain not in self.clients:
            raise ApiError(400, "BAD_CHAIN", f"chain must be one of {sorted(self.clients)}")
        if not isinstance(user, str) or not is_address(user):
            raise ApiError(400, "BAD_REQUEST", "user must be a 0x address")
        user = to_checksum_address(user)
        try:
            inr_paise = rupees_to_paise(inr)
        except ValueError as e:
            raise ApiError(400, "BAD_REQUEST", str(e)) from None

        # 1. QR first: refuse an invalid QR before anything is signed.
        parsed = parse_upi_qr(qr_text or "", inr_paise)
        if not parsed.ok:
            code = REFUND_REASONS[int(parsed.reason)]  # INVALID_QR | AMOUNT_MISMATCH
            raise ApiError(400, code, parsed.detail)

        s = self.settings
        if inr_paise < s.min_inr_paise or inr_paise > s.max_inr_paise:
            raise ApiError(400, "AMOUNT_OUT_OF_RANGE",
                           f"amount must be between Rs {format_paise(s.min_inr_paise)} and Rs {format_paise(s.max_inr_paise)}")

        # 3. price
        rate = self.fx.get(cfg.fx_market)
        if rate is None:
            raise ApiError(503, "FX_STALE", f"no fresh {cfg.fx_market} rate (older than {s.fx_max_age_sec:g}s); try again")
        rate_e6 = offramp_rate_e6(price_to_e6(rate.bid), s.spread_bps)
        if rate_e6 <= 0:
            raise ApiError(503, "FX_STALE", "market rate is not usable")
        amount = offramp_amount(inr_paise, cfg.token.decimals, rate_e6)

        now = int(self.clock())
        used = self.store.user_quoted_paise(user, now - DAY, now)
        if used + inr_paise > s.daily_user_inr_paise:
            raise ApiError(429, "DAILY_LIMIT", f"daily limit Rs {format_paise(s.daily_user_inr_paise)} reached "
                           f"(Rs {format_paise(used)} used in the last 24 h)")

        try:
            nonce = await self.clients[chain].token_nonce(user)
        except Exception as e:
            raise ApiError(503, "RPC_UNAVAILABLE", f"could not read token.nonces(user): {type(e).__name__}") from None

        # 2. id, salt, commitment, deadline
        id_b, salt = random_bytes32(), random_bytes32()
        commitment = qr_commitment(salt, qr_text)
        intent = OfframpIntent(id=hex32(id_b), user=user, amount=amount, inr_paise=inr_paise, rate_e6=rate_e6,
                               qr_commitment=hex32(commitment), deadline=now + s.quote_ttl_sec)

        # 4. sign hashIntent as the quote signer
        vd, pd = domains(cfg)
        quote_sig = "0x" + sign_digest_typed(self.signer, intent_typed_data(vd, intent)).hex()
        view_token = derive_view_token(s.view_token_secret, id_b)

        self.store.insert_quote({
            "id": intent.id, "chain": chain, "user": user, "amount": str(amount), "inr_paise": inr_paise,
            "rate_e6": str(rate_e6), "qr_text": qr_text, "salt": hex32(salt), "qr_commitment": intent.qr_commitment,
            "deadline": intent.deadline, "quote_sig": quote_sig, "upi_id": parsed.upi_id,
            "payee_name": parsed.payee_name, "market": cfg.fx_market, "bid": rate.bid, "status": "QUOTED",
            "view_token": view_token, "created_at": now,
        })

        signing = signing_payloads(cfg, intent)
        signing["permit"]["message"] = {
            "owner": user, "spender": vd["verifyingContract"], "value": str(amount),
            "nonce": str(nonce), "deadline": str(intent.deadline),
        }
        return {
            "id": intent.id,
            "intent": intent.to_json(),
            "quoteSig": quote_sig,
            "payee": {
                "upiId": parsed.upi_id,
                "name": parsed.payee_name,
                "amountInQr": format_paise(parsed.amount_paise) if parsed.amount_paise is not None else None,
            },
            "display": {
                "amount": format_units(amount, cfg.token.decimals),
                "symbol": cfg.token.symbol,
                "rate": two_dp(Decimal(rate_e6) / Decimal(10**6)),
                "marketRate": two_dp(rate.bid),
                "globalRef": two_dp(self.fx.global_ref(cfg.fx_market)),
                "spreadBps": s.spread_bps,
                "networkFee": "₹0",
                "expiresAt": str(intent.deadline),
            },
            "signing": signing,
            "viewToken": view_token,
        }
