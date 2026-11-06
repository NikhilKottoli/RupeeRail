"""qrCommitment = keccak256(abi.encode(bytes32 salt, string qrText)) (abi.encode, NOT encodePacked).

The salt and qrText never go onchain; the backend stores them keyed by the offramp id.
"""

from __future__ import annotations

import secrets

from eth_abi import encode
from eth_utils import keccak


def to_bytes32(v: bytes | str) -> bytes:
    if isinstance(v, str):
        h = v[2:] if v.startswith("0x") else v
        b = bytes.fromhex(h)
    else:
        b = bytes(v)
    if len(b) != 32:
        raise ValueError("expected 32 bytes")
    return b


def qr_commitment(salt: bytes | str, qr_text: str) -> bytes:
    return keccak(encode(["bytes32", "string"], [to_bytes32(salt), qr_text]))


def random_bytes32() -> bytes:
    return secrets.token_bytes(32)


def hex32(b: bytes) -> str:
    return "0x" + bytes(b).hex()
