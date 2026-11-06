from __future__ import annotations

from typing import Protocol


class Signer(Protocol):
    #: True if the wallet pays from an existing allowance, instead of signing a permit for each payment.
    uses_allowance: bool

    def address(self) -> str: ...

    def sign_typed_data(self, typed_data: dict) -> str:
        """Signs a full EIP-712 payload and returns a 0x signature."""
        ...
