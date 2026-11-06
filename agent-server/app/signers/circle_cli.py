"""Circle agent wallet, through the Circle CLI.

It's a smart account, so signatures verify through ERC-1271 and not `ecrecover`. The backend checks permits with
`ecrecover` only, and a valid permit would also reset the vault's allowance to one payment. So this wallet pays from
a capped allowance set with `approve`, and signs only the intent. See docs/verify-log.md.
"""

from __future__ import annotations

import json
import re
import subprocess
from typing import Callable

SIGNATURE_RE = re.compile(r"^0x[0-9a-fA-F]{130}$")


class CircleSignerError(Exception):
    pass


class CircleCliSigner:
    uses_allowance = True

    def __init__(self, wallet: str, chain: str = "ARC-TESTNET", binary: str = "circle",
                 run: Callable[..., subprocess.CompletedProcess] = subprocess.run, timeout: float = 45.0) -> None:
        self.wallet, self.chain, self.binary, self.run, self.timeout = wallet, chain, binary, run, timeout

    def address(self) -> str:
        return self.wallet

    def sign_typed_data(self, typed_data: dict) -> str:
        result = self.run(
            [self.binary, "wallet", "sign", "typed-data", json.dumps(typed_data), "--address", self.wallet,
             "--chain", self.chain, "--output", "json"],
            capture_output=True, text=True, timeout=self.timeout,
        )
        output = result.stdout or ""
        start = output.find("{")  # the CLI can print a warning line before the JSON
        try:
            body = json.loads(output[start:]) if start >= 0 else {}
        except ValueError:
            body = {}
        data = body.get("data") or body
        signature = data.get("signature") if isinstance(data, dict) else None
        if result.returncode != 0 or not isinstance(signature, str) or not SIGNATURE_RE.match(signature):
            error = data.get("error") if isinstance(data, dict) else None
            message = error.get("message") if isinstance(error, dict) else "no valid signature returned"
            raise CircleSignerError(f"Circle wallet didn't sign: {message}")
        return signature
