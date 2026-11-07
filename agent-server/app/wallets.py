"""Sets up an agent's wallet and budget, and revokes it when the agent is archived.

Every step can be repeated, so a failed setup can simply be tried again. Nothing is treated as done until it can be
read back from the chain.
"""

from __future__ import annotations

import logging
import threading
import time
from decimal import Decimal
from typing import Any, Callable

from .signers.circle_admin import WalletAdmin, WalletAdminError
from .store import AgentStore

log = logging.getLogger("rupeerail.agent")


class BudgetTooHigh(Exception):
    pass


class WalletManager:
    def __init__(self, store: AgentStore, admin: WalletAdmin, client: Any, chain: str, max_budget_usdc: Decimal,
                 poll_sec: float = 3.0, wait_sec: float = 90.0, sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.monotonic, reserved: tuple[str, ...] = ()) -> None:
        self.store, self.admin, self.client, self.chain, self.max_budget_usdc = store, admin, client, chain, max_budget_usdc
        self.poll_sec, self.wait_sec, self.sleep, self.clock = poll_sec, wait_sec, sleep, clock
        self.assigning = threading.Lock()
        self.reserved = {address.lower() for address in reserved}  # wallets another agent server on this Circle account is using

    def check_budget(self, usdc: Decimal) -> None:
        if usdc > self.max_budget_usdc:
            raise BudgetTooHigh(f"the most an agent's budget can be is {self.max_budget_usdc} USDC")

    def _chain_info(self) -> tuple[str, str, int]:
        chain = self.client.config()["chains"][self.chain]
        return chain["token"]["address"], chain["vault"], int(chain["token"]["decimals"])

    def _account(self, address: str) -> dict:
        return self.client.account(self.chain, address)

    def _wait_for(self, check: Callable[[], bool]) -> bool:
        deadline = self.clock() + self.wait_sec
        while True:
            try:
                if check():
                    return True
            except Exception as e:  # a network blip here doesn't change what's on-chain
                log.warning("wallet check failed: %s", type(e).__name__)
            if self.clock() >= deadline:
                return False
            self.sleep(self.poll_sec)

    def _assign(self, agent: dict) -> str:
        if agent["wallet"]:
            return agent["wallet"]
        self.store.set_wallet_state(agent["id"], "creating", "finding a wallet")
        with self.assigning:  # two new agents must not pick the same unassigned wallet
            taken = set(self.store.assigned_wallets()) | self.reserved
            spare = next((w for w in self.admin.list_wallets() if w.lower() not in taken), None)
            address = spare or self.admin.create_wallet(agent["id"])
            self.store.assign_wallet(agent["id"], address)
        return address

    def _ensure_funds(self, agent_id: str, address: str, needed: int) -> bool:
        if int(self._account(address)["balance"]) >= needed:
            return True
        self.store.set_wallet_state(agent_id, "funding", "asking the Circle faucet for test USDC")
        try:
            self.admin.fund(address)
        except WalletAdminError as e:
            self.store.set_wallet_state(agent_id, "needs_funds", f"the faucet refused ({e}). Send USDC on Arc testnet to this wallet, then try again")
            return False
        if self._wait_for(lambda: int(self._account(address)["balance"]) >= needed):
            return True
        self.store.set_wallet_state(agent_id, "needs_funds", "the faucet's USDC hasn't arrived. Send USDC on Arc testnet to this wallet, then try again")
        return False

    def _approve(self, address: str, token: str, vault: str, amount: int) -> bool:
        """Sends the approval and waits to read it back. Returns False if the chain never shows it."""
        try:
            self.admin.approve(address, token, vault, amount)
        except WalletAdminError as e:
            if "deployed" not in str(e).lower():
                raise
            self.admin.self_transfer(address, token)  # a new smart account can need one transaction to deploy
            self.admin.approve(address, token, vault, amount)
        return self._wait_for(lambda: int(self._account(address)["allowance"]) == amount)

    def setup(self, agent_id: str) -> None:
        """Assign or create, fund, then set the budget. The status is shown on the agent's card as it goes."""
        agent = self.store.agent(agent_id)
        try:
            address = self._assign(agent)
            token, vault, decimals = self._chain_info()
            amount = int(Decimal(agent["budget_usdc"]) * 10 ** decimals)
            if not self._ensure_funds(agent_id, address, amount):
                return
            self.store.set_wallet_state(agent_id, "approving", "setting the on-chain budget")
            if self._approve(address, token, vault, amount):
                self.store.set_wallet_state(agent_id, "ready")
            else:
                self.store.set_wallet_state(agent_id, "failed", "the budget hasn't shown up on-chain yet. Try again")
        except WalletAdminError as e:
            self.store.set_wallet_state(agent_id, "failed", str(e))
        except Exception as e:
            log.exception("wallet setup failed")
            self.store.set_wallet_state(agent_id, "failed", f"wallet setup failed: {type(e).__name__}")

    def revoke(self, agent_id: str) -> None:
        """Sets the allowance to 0 and confirms it. Raises if that can't be confirmed, so an agent is never archived
        while its wallet can still be spent from."""
        agent = self.store.agent(agent_id)
        if not agent["wallet"]:
            return
        token, vault, _ = self._chain_info()
        if int(self._account(agent["wallet"])["allowance"]) == 0:
            return
        self.admin.approve(agent["wallet"], token, vault, 0)
        if not self._wait_for(lambda: int(self._account(agent["wallet"])["allowance"]) == 0):
            raise WalletAdminError("couldn't confirm the budget was revoked")
