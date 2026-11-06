"""POST /offramp: the paymaster relays offramp(intent, userSig, quoteSig, permit) so the user pays no gas.

Checks, in order: quote exists / same chain / every field matches / unused / unexpired ->
user signature over hashIntent (EOA ecrecover, or ERC-1271 for contract wallets) -> permit
signature against token.nonces(user) (a bad permit is tolerated only if the allowance already
covers the amount, mirroring the vault's try/catch) -> balance -> atomic claim -> simulate -> send.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from .chain import ChainError, DryRunError
from .common import ApiError, EventHub, verify_user_signature
from .config import ChainConfig
from .eip712 import OfframpIntent, ecdsa_recover_vrs, intent_digest, parse_sig_hex, permit_digest
from .quote_service import domains
from .store import Store

log = logging.getLogger("rupeerail.paymaster")


def _b32(v: str, name: str) -> bytes:
    try:
        b = parse_sig_hex(v)
    except ValueError:
        raise ApiError(400, "BAD_PERMIT_SIG", f"permit.{name} is not hex") from None
    if len(b) != 32:
        raise ApiError(400, "BAD_PERMIT_SIG", f"permit.{name} must be 32 bytes")
    return b


class Paymaster:
    def __init__(self, chains: dict[str, ChainConfig], clients: dict[str, Any], store: Store, account: Any,
                 hub: EventHub | None = None, clock=time.time) -> None:
        self.chains, self.clients, self.store, self.account, self.hub = chains, clients, store, account, hub
        self.clock = clock

    async def submit(self, chain: str, intent_json: dict, user_sig_hex: str, permit: dict) -> dict:
        cfg = self.chains.get(chain)
        if cfg is None or chain not in self.clients:
            raise ApiError(400, "BAD_CHAIN", f"chain must be one of {sorted(self.clients)}")
        client = self.clients[chain]
        try:
            intent = OfframpIntent.from_json(intent_json)
        except (KeyError, ValueError, TypeError) as e:
            raise ApiError(400, "BAD_REQUEST", f"malformed intent: {e}") from None

        # 1. quote: exists, same chain, every field identical, unused, unexpired
        q = self.store.get_quote(intent.id)
        if q is None:
            raise ApiError(404, "UNKNOWN_QUOTE", "no quote with this id")
        if q["chain"] != chain:
            raise ApiError(400, "QUOTE_MISMATCH", "quote was issued for another chain", fields=["chain"])
        stored = OfframpIntent(id=q["id"], user=q["user"], amount=int(q["amount"]), inr_paise=int(q["inr_paise"]),
                               rate_e6=int(q["rate_e6"]), qr_commitment=q["qr_commitment"], deadline=int(q["deadline"]))
        diff = [f for f in ("user", "amount", "inr_paise", "rate_e6", "qr_commitment", "deadline")
                if getattr(stored, f) != getattr(intent, f)]
        if diff:
            raise ApiError(400, "QUOTE_MISMATCH", "intent does not match the issued quote", fields=diff)
        if q["status"] != "QUOTED":
            raise ApiError(409, "QUOTE_USED", f"quote already {q['status'].lower()}")
        if int(self.clock()) > intent.deadline:
            raise ApiError(410, "QUOTE_EXPIRED", "quote expired; request a new one")

        vd, pd = domains(cfg)
        digest = intent_digest(vd, intent)

        # 2. user signature (EOA or ERC-1271)
        try:
            user_sig = parse_sig_hex(user_sig_hex)
        except ValueError:
            raise ApiError(400, "BAD_INTENT_SIG", "userSig is not hex") from None
        ok, user_sig, how = await verify_user_signature(client, intent.user, digest, user_sig)
        if not ok:
            raise ApiError(400, "BAD_INTENT_SIG", f"userSig does not verify for user ({how})")

        # 3. permit
        try:
            p_deadline, p_v = int(str(permit["deadline"])), int(str(permit["v"]))
        except (KeyError, ValueError, TypeError):
            raise ApiError(400, "BAD_PERMIT_SIG", "permit needs deadline, v, r, s") from None
        if p_v in (0, 1):
            p_v += 27
        if not 0 <= p_v <= 255 or not 0 <= p_deadline < 2**256:
            raise ApiError(400, "BAD_PERMIT_SIG", "permit v/deadline out of range")
        p_r, p_s = _b32(str(permit.get("r", "")), "r"), _b32(str(permit.get("s", "")), "s")
        nonce = await client.token_nonce(intent.user)
        p_digest = permit_digest(pd, intent.user, cfg.vault, intent.amount, nonce, p_deadline)
        rec = ecdsa_recover_vrs(p_digest, p_v, p_r, p_s)
        permit_ok = rec is not None and rec.lower() == intent.user.lower() and p_deadline >= int(self.clock())
        if not permit_ok:
            allowance = await client.token_allowance(intent.user, cfg.vault)
            if allowance < intent.amount:
                raise ApiError(400, "BAD_PERMIT_SIG", "permit signature does not verify against token.nonces(user) "
                               "(or is expired) and the existing allowance is too small", nonce=str(nonce))

        # 4. balance
        bal = await client.token_balance(intent.user)
        if bal < intent.amount:
            raise ApiError(400, "INSUFFICIENT_BALANCE", f"balance {bal} < amount {intent.amount}")

        # 5. atomic claim, then simulate + send
        if not self.store.claim_quote(intent.id, "QUOTED", "SUBMITTED"):
            raise ApiError(409, "QUOTE_USED", "quote is already being submitted")
        quote_sig = parse_sig_hex(q["quote_sig"])
        args = [intent.as_tuple(), user_sig, quote_sig, (p_deadline, p_v, p_r, p_s)]
        try:
            res = await client.send(self.account, "offramp", args)
        except DryRunError as e:
            self.store.set_quote_status(intent.id, "QUOTED", last_error=str(e))
            raise ApiError(503, "DRY_RUN", str(e)) from None
        except ChainError as e:
            if e.name == "ReceiptTimeout":  # broadcast but unconfirmed: keep the claim, listener will see it
                self.store.set_quote_status(intent.id, "SUBMITTED", last_error=str(e))
                raise ApiError(504, "PENDING", str(e), revert=e.to_json()) from None
            self.store.set_quote_status(intent.id, "QUOTED", last_error=str(e))
            raise ApiError(409 if e.name == "OfframpIdUsed" else 400, "SIMULATION_FAILED", str(e),
                           revert=e.to_json()) from None
        except Exception as e:
            # Ambiguous (e.g. network error around send_raw_transaction): keep the claim.
            self.store.set_quote_status(intent.id, "SUBMITTED", last_error=f"{type(e).__name__}: {e}")
            raise ApiError(502, "SEND_FAILED", f"{type(e).__name__}: {e}") from None

        self.store.set_quote_status(intent.id, "USED", offramp_tx=res.hash)
        if self.hub:
            self.hub.publish({"type": "paymaster", "action": "offramp", "chain": chain, "id": intent.id,
                              "tx": res.hash, "explorer": client.tx_url(res.hash)})
        log.info("[paymaster:%s] offramp %s %s", chain, intent.id[:10], client.tx_url(res.hash))
        # The listener flips the status to LOCKED once it sees (and matches) OfframpLocked.
        return {"id": intent.id, "txHash": res.hash, "explorer": client.tx_url(res.hash),
                "paidBy": self.account.address, "status": "SUBMITTED"}
