"""Fallback signer: a plain testnet key from the environment. It signs permits, so it needs no allowance."""

from __future__ import annotations

from eth_account import Account
from eth_account.messages import encode_typed_data


class LocalKeySigner:
    uses_allowance = False

    def __init__(self, private_key: str) -> None:
        self.account = Account.from_key(private_key)

    def address(self) -> str:
        return self.account.address

    def sign_typed_data(self, typed_data: dict) -> str:
        signed = self.account.sign_message(encode_typed_data(full_message=typed_data))
        return "0x" + bytes(signed.signature).hex()
