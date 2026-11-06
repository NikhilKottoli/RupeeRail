"""POST /dispute: verify a user's DisputeIntent signature, then relay disputeOfframpWithSig
from the paymaster so a zero-gas user can still dispute a dishonest completion.
"""

from __future__ import annotations

import logging
from typing import Any

from .chain import ChainError, DryRunError
from .commitment import to_bytes32
from .common import ApiError, EventHub, verify_user_signature
from .config import ChainConfig
from .eip712 import UINT64_MAX, dispute_digest, parse_sig_hex, vault_domain
from .store import Store

log = logging.getLogger("rupeerail.dispute")

COMPLETED = 2


class DisputeRelay:
    def __init__(self, chains: dict[str, ChainConfig], clients: dict[str, Any], store: Store, account: Any,
                 hub: EventHub | None = None) -> None:
        self.chains, self.clients, self.store, self.account, self.hub = chains, clients, store, account, hub

    async def relay(self, chain: str, id_: str, deadline: int | str, user_sig_hex: str) -> dict:
        cfg = self.chains.get(chain)
        if cfg is None or chain not in self.clients:
            raise ApiError(400, "BAD_CHAIN", f"chain must be one of {sorted(self.clients)}")
        c = self.clients[chain]
        try:
            id_ = "0x" + to_bytes32(id_).hex()
            deadline = int(str(deadline))
            if not 0 <= deadline <= UINT64_MAX:
                raise ValueError("deadline out of uint64 range")
            sig = parse_sig_hex(user_sig_hex)
        except (ValueError, TypeError) as e:
            raise ApiError(400, "BAD_REQUEST", str(e)) from None

        st = await c.offramp_state(id_)
        if st.status != COMPLETED:
            raise ApiError(409, "NOT_DISPUTABLE", f"offramp is {st.status_name}, only COMPLETED can be disputed",
                           onchainStatus=st.status_name)
        now = await c.chain_time()
        if now > deadline:
            raise ApiError(400, "INTENT_EXPIRED", "dispute signature deadline has passed")
        dl = await c.deadlines(id_)
        if now >= dl.releasable_at:
            raise ApiError(409, "NOT_DISPUTABLE", "the dispute window has closed", releasableAt=str(dl.releasable_at))

        digest = dispute_digest(vault_domain(cfg.chain_id, cfg.vault), id_, st.user, deadline)
        ok, sig, how = await verify_user_signature(c, st.user, digest, sig)
        if not ok:
            raise ApiError(400, "BAD_DISPUTE_SIG", f"signature does not verify for the offramp's user ({how})")

        if not self.store.begin_dispute({"id": id_, "chain": chain, "user": st.user, "deadline": deadline,
                                         "user_sig": "0x" + sig.hex()}):
            raise ApiError(409, "DISPUTE_IN_PROGRESS", "a dispute for this offramp is already being relayed")
        try:
            res = await c.send(self.account, "disputeOfframpWithSig", [to_bytes32(id_), deadline, sig])
        except DryRunError as e:
            self.store.finish_dispute(id_, "FAILED", error=str(e))
            raise ApiError(503, "DRY_RUN", str(e)) from None
        except ChainError as e:
            self.store.finish_dispute(id_, "FAILED", error=str(e))
            raise ApiError(400, "SIMULATION_FAILED", str(e), revert=e.to_json()) from None
        except Exception as e:
            self.store.finish_dispute(id_, "FAILED", error=f"{type(e).__name__}: {e}")
            raise ApiError(502, "SEND_FAILED", f"{type(e).__name__}: {e}") from None

        self.store.finish_dispute(id_, "RELAYED", tx=res.hash)
        self.store.ensure_payment(id_, chain)
        try:
            self.store.update_payment(id_, status="DISPUTED", dispute_deadline=(await c.deadlines(id_)).releasable_at)
        except Exception:
            self.store.update_payment(id_, status="DISPUTED")  # the listener fills the deadline in
        self.store.add_tx(id_, "dispute", res.hash)
        if self.hub:
            self.hub.publish({"type": "dispute_relay", "chain": chain, "id": id_, "tx": res.hash,
                              "explorer": c.tx_url(res.hash)})
        log.info("[dispute:%s] relayed %s %s", chain, id_[:10], c.tx_url(res.hash))
        return {"id": id_, "txHash": res.hash, "status": "DISPUTED", "explorer": c.tx_url(res.hash)}
