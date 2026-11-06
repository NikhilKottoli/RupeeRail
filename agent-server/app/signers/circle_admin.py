"""Circle wallet administration: creating, funding and setting a budget.

These are the actions that change what an agent can spend, so they're kept apart from signing. Only the panel's
requests reach this class. The state machine and deciders are never given it, and a run can only sign.
"""

from __future__ import annotations

import json
import subprocess
from typing import Callable

MAX_WALLETS_PER_CHAIN = 5


class WalletAdminError(Exception):
    pass


class WalletLimitReached(WalletAdminError):
    pass


class WalletAdmin:
    def __init__(self, chain: str = "ARC-TESTNET", binary: str = "circle",
                 run: Callable[..., subprocess.CompletedProcess] = subprocess.run, timeout: float = 90.0) -> None:
        self.chain, self.binary, self.run, self.timeout = chain, binary, run, timeout

    def _call(self, *args: str) -> dict:
        try:
            result = self.run([self.binary, "wallet", *args, "--output", "json"], capture_output=True, text=True, timeout=self.timeout)
        except subprocess.TimeoutExpired:
            raise WalletAdminError(f"Circle didn't answer `wallet {args[0]}` in time") from None
        output = result.stdout or ""
        start = output.find("{")  # the CLI can print a warning line before the JSON
        try:
            body = json.loads(output[start:]) if start >= 0 else {}
        except ValueError:
            body = {}
        data = body.get("data", body) if isinstance(body, dict) else {}
        if result.returncode != 0:
            error = data.get("error") if isinstance(data, dict) else None
            message = error.get("message") if isinstance(error, dict) else None
            raise WalletAdminError(f"Circle `wallet {args[0]}` failed: {message or 'no details given'}")
        return data if isinstance(data, dict) else {}

    def list_wallets(self) -> list[str]:
        data = self._call("list", "--type", "agent", "--chain", self.chain)
        return [w["address"] for w in data.get("wallets", []) if isinstance(w, dict) and w.get("address")]

    def create_wallet(self, idempotency_key: str) -> str:
        """Uses one of Circle's limited slots, so callers try an unassigned wallet first. Also creates a wallet on every
        other testnet, which aren't used. Returns the new address by listing, so it works for a retried key as well."""
        before = self.list_wallets()
        if len(before) >= MAX_WALLETS_PER_CHAIN:
            raise WalletLimitReached(f"Circle allows {MAX_WALLETS_PER_CHAIN} agent wallets on this chain, and all are in use")
        self._call("create", "--testnet", "--idempotency-key", idempotency_key)
        created = [w for w in self.list_wallets() if w not in before]
        if len(created) != 1:
            raise WalletAdminError("Circle didn't return one new wallet")
        return created[0]

    def fund(self, address: str) -> None:
        self._call("fund", "--address", address, "--chain", self.chain)

    def approve(self, address: str, token: str, spender: str, amount: int) -> None:
        self._call("execute", "approve(address,uint256)", spender, str(amount), "--contract", token, "--address", address, "--chain", self.chain)

    def self_transfer(self, address: str, token: str, amount: str = "0.01") -> None:
        """Deploys a smart account that hasn't made a transaction yet."""
        self._call("transfer", address, "--amount", amount, "--token", token, "--address", address, "--chain", self.chain)
