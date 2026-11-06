"""EIP-712 typed data for ScanPayVault (OfframpIntent, DisputeIntent) and the token Permit.

The digests here must equal the vault's `hashIntent` / `hashDispute` and the token's
`DOMAIN_SEPARATOR()`; tests/test_live_readonly.py checks that against both live chains.
Signature checks mirror OpenZeppelin 5.1 ECDSA.tryRecover: 65 bytes, low s, v in {27, 28}.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from eth_account import Account
from eth_account.messages import _hash_eip191_message, encode_typed_data
from eth_keys import keys as eth_keys
from eth_utils import keccak, to_checksum_address

from .commitment import hex32, to_bytes32

SECP256K1_N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
SECP256K1_HALF_N = 0x7FFFFFFFFFFFFFFFFFFFFFFFFFFFFFFF5D576E7357A4501DDFE92F46681B20A0
ERC1271_MAGIC = bytes.fromhex("1626ba7e")

EIP712_DOMAIN = [
    {"name": "name", "type": "string"},
    {"name": "version", "type": "string"},
    {"name": "chainId", "type": "uint256"},
    {"name": "verifyingContract", "type": "address"},
]
OFFRAMP_INTENT_TYPE = [
    {"name": "id", "type": "bytes32"},
    {"name": "user", "type": "address"},
    {"name": "amount", "type": "uint256"},
    {"name": "inrPaise", "type": "uint64"},
    {"name": "rateE6", "type": "uint64"},
    {"name": "qrCommitment", "type": "bytes32"},
    {"name": "deadline", "type": "uint64"},
]
DISPUTE_INTENT_TYPE = [
    {"name": "id", "type": "bytes32"},
    {"name": "user", "type": "address"},
    {"name": "deadline", "type": "uint64"},
]
PERMIT_TYPE = [
    {"name": "owner", "type": "address"},
    {"name": "spender", "type": "address"},
    {"name": "value", "type": "uint256"},
    {"name": "nonce", "type": "uint256"},
    {"name": "deadline", "type": "uint256"},
]
TYPES_FOR_CLIENTS = {
    "OfframpIntent": OFFRAMP_INTENT_TYPE,
    "DisputeIntent": DISPUTE_INTENT_TYPE,
    "Permit": PERMIT_TYPE,
}
OFFRAMP_INTENT_TYPESTRING = (
    "OfframpIntent(bytes32 id,address user,uint256 amount,uint64 inrPaise,uint64 rateE6,bytes32 qrCommitment,uint64 deadline)"
)
DISPUTE_INTENT_TYPESTRING = "DisputeIntent(bytes32 id,address user,uint64 deadline)"

UINT64_MAX = 2**64 - 1
UINT256_MAX = 2**256 - 1


def vault_domain(chain_id: int, vault: str) -> dict[str, Any]:
    return {"name": "ScanPayVault", "version": "1", "chainId": chain_id, "verifyingContract": to_checksum_address(vault)}


def permit_domain(name: str, version: str, chain_id: int, token: str) -> dict[str, Any]:
    return {"name": name, "version": version, "chainId": chain_id, "verifyingContract": to_checksum_address(token)}


@dataclass(frozen=True)
class OfframpIntent:
    id: str  # 0x bytes32
    user: str  # checksum address
    amount: int
    inr_paise: int
    rate_e6: int
    qr_commitment: str  # 0x bytes32
    deadline: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", hex32(to_bytes32(self.id)))
        object.__setattr__(self, "qr_commitment", hex32(to_bytes32(self.qr_commitment)))
        object.__setattr__(self, "user", to_checksum_address(self.user))
        for name in ("inr_paise", "rate_e6", "deadline"):
            v = getattr(self, name)
            if not isinstance(v, int) or not 0 <= v <= UINT64_MAX:
                raise ValueError(f"{name} out of uint64 range")
        if not isinstance(self.amount, int) or not 0 <= self.amount <= UINT256_MAX:
            raise ValueError("amount out of uint256 range")

    def message(self) -> dict[str, Any]:
        return {
            "id": to_bytes32(self.id),
            "user": self.user,
            "amount": self.amount,
            "inrPaise": self.inr_paise,
            "rateE6": self.rate_e6,
            "qrCommitment": to_bytes32(self.qr_commitment),
            "deadline": self.deadline,
        }

    def as_tuple(self) -> tuple:
        """Argument order of the Solidity struct, for contract calls."""
        return (
            to_bytes32(self.id),
            self.user,
            self.amount,
            self.inr_paise,
            self.rate_e6,
            to_bytes32(self.qr_commitment),
            self.deadline,
        )

    def to_json(self) -> dict[str, str]:
        """All fields as strings (what /quote returns and /offramp accepts)."""
        return {
            "id": self.id,
            "user": self.user,
            "amount": str(self.amount),
            "inrPaise": str(self.inr_paise),
            "rateE6": str(self.rate_e6),
            "qrCommitment": self.qr_commitment,
            "deadline": str(self.deadline),
        }

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> "OfframpIntent":
        return cls(
            id=str(d["id"]),
            user=str(d["user"]),
            amount=int(str(d["amount"])),
            inr_paise=int(str(d["inrPaise"])),
            rate_e6=int(str(d["rateE6"])),
            qr_commitment=str(d["qrCommitment"]),
            deadline=int(str(d["deadline"])),
        )


def intent_typed_data(domain: dict[str, Any], intent: OfframpIntent) -> dict[str, Any]:
    return {
        "types": {"EIP712Domain": EIP712_DOMAIN, "OfframpIntent": OFFRAMP_INTENT_TYPE},
        "primaryType": "OfframpIntent",
        "domain": domain,
        "message": intent.message(),
    }


def dispute_typed_data(domain: dict[str, Any], id_: str, user: str, deadline: int) -> dict[str, Any]:
    return {
        "types": {"EIP712Domain": EIP712_DOMAIN, "DisputeIntent": DISPUTE_INTENT_TYPE},
        "primaryType": "DisputeIntent",
        "domain": domain,
        "message": {"id": to_bytes32(id_), "user": to_checksum_address(user), "deadline": int(deadline)},
    }


def permit_typed_data(
    domain: dict[str, Any], owner: str, spender: str, value: int, nonce: int, deadline: int
) -> dict[str, Any]:
    return {
        "types": {"EIP712Domain": EIP712_DOMAIN, "Permit": PERMIT_TYPE},
        "primaryType": "Permit",
        "domain": domain,
        "message": {
            "owner": to_checksum_address(owner),
            "spender": to_checksum_address(spender),
            "value": int(value),
            "nonce": int(nonce),
            "deadline": int(deadline),
        },
    }


def typed_digest(full_message: dict[str, Any]) -> bytes:
    return _hash_eip191_message(encode_typed_data(full_message=full_message))


def domain_separator(domain: dict[str, Any]) -> bytes:
    """keccak256(abi.encode(EIP712Domain typehash, name, version, chainId, verifyingContract))."""
    signable = encode_typed_data(
        full_message={
            "types": {"EIP712Domain": EIP712_DOMAIN, "X": [{"name": "a", "type": "uint256"}]},
            "primaryType": "X",
            "domain": domain,
            "message": {"a": 0},
        }
    )
    return bytes(signable.header)


def intent_digest(domain: dict[str, Any], intent: OfframpIntent) -> bytes:
    return typed_digest(intent_typed_data(domain, intent))


def dispute_digest(domain: dict[str, Any], id_: str, user: str, deadline: int) -> bytes:
    return typed_digest(dispute_typed_data(domain, id_, user, deadline))


def permit_digest(domain: dict[str, Any], owner: str, spender: str, value: int, nonce: int, deadline: int) -> bytes:
    return typed_digest(permit_typed_data(domain, owner, spender, value, nonce, deadline))


def sign_digest_typed(account, full_message: dict[str, Any]) -> bytes:
    """Signs typed data with an eth_account LocalAccount; returns 65 bytes r||s||v (v = 27/28)."""
    signed = account.sign_message(encode_typed_data(full_message=full_message))
    return bytes(signed.signature)


def parse_sig_hex(sig: str | bytes) -> bytes:
    if isinstance(sig, bytes):
        return sig
    s = sig[2:] if sig.startswith("0x") else sig
    try:
        return bytes.fromhex(s)
    except ValueError:
        raise ValueError("signature is not hex") from None


def normalize_v(sig: bytes) -> bytes:
    """Wallets that return v in {0, 1} get v + 27, which ecrecover requires."""
    if len(sig) == 65 and sig[64] in (0, 1):
        return sig[:64] + bytes([sig[64] + 27])
    return sig


def split_sig(sig: bytes) -> tuple[int, bytes, bytes]:
    """Returns (v, r, s) of a 65-byte signature."""
    if len(sig) != 65:
        raise ValueError("signature must be 65 bytes")
    return sig[64], sig[:32], sig[32:64]


def ecdsa_try_recover(digest: bytes, sig: bytes) -> str | None:
    """Mirror of OZ ECDSA.tryRecover(bytes32, bytes): None on any error, else checksum address."""
    if len(sig) != 65:
        return None
    r = int.from_bytes(sig[:32], "big")
    s = int.from_bytes(sig[32:64], "big")
    v = sig[64]
    if s > SECP256K1_HALF_N or v not in (27, 28) or r == 0 or s == 0 or r >= SECP256K1_N:
        return None
    try:
        pub = eth_keys.Signature(vrs=(v - 27, r, s)).recover_public_key_from_msg_hash(digest)
    except Exception:
        return None
    return pub.to_checksum_address()


def ecdsa_recover_vrs(digest: bytes, v: int, r: bytes, s: bytes) -> str | None:
    return ecdsa_try_recover(digest, bytes(r).rjust(32, b"\0") + bytes(s).rjust(32, b"\0") + bytes([v & 0xFF]))


def verify_eoa(digest: bytes, sig: bytes, expected: str) -> bool:
    rec = ecdsa_try_recover(digest, sig)
    return rec is not None and rec.lower() == expected.lower()


def typehash(type_string: str) -> bytes:
    return keccak(text=type_string)


def sign_hash_unsafe_for_tests(private_key: str, digest: bytes) -> bytes:
    """Raw hash signing; only used by tests to craft signatures over arbitrary digests."""
    signed = Account.unsafe_sign_hash(digest, private_key)
    return bytes(signed.signature)
