"""AsyncWeb3 client per chain: retried reads, a per-sender nonce queue, "simulate (eth_call)
then send" with EIP-1559 fees and a per-chain maxFeePerGas floor (Arc: 20 gwei), and decoding
of the vault's custom errors from its ABI.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
from dataclasses import dataclass
from typing import Any, Callable

from eth_abi import decode as abi_decode
from eth_utils import keccak, to_checksum_address
from web3 import AsyncHTTPProvider, AsyncWeb3
from web3.exceptions import ContractLogicError, TimeExhausted

from .commitment import hex32
from .config import ABI_PATH, ChainConfig
from .store import STATUS_NAMES

log = logging.getLogger("rupeerail.chain")

TOKEN_ABI = [
    {"type": "function", "name": "balanceOf", "stateMutability": "view",
     "inputs": [{"name": "a", "type": "address"}], "outputs": [{"name": "", "type": "uint256"}]},
    {"type": "function", "name": "allowance", "stateMutability": "view",
     "inputs": [{"name": "o", "type": "address"}, {"name": "s", "type": "address"}],
     "outputs": [{"name": "", "type": "uint256"}]},
    {"type": "function", "name": "nonces", "stateMutability": "view",
     "inputs": [{"name": "o", "type": "address"}], "outputs": [{"name": "", "type": "uint256"}]},
    {"type": "function", "name": "DOMAIN_SEPARATOR", "stateMutability": "view",
     "inputs": [], "outputs": [{"name": "", "type": "bytes32"}]},
    {"type": "function", "name": "decimals", "stateMutability": "view",
     "inputs": [], "outputs": [{"name": "", "type": "uint8"}]},
]
ERC1271_ABI = [
    {"type": "function", "name": "isValidSignature", "stateMutability": "view",
     "inputs": [{"name": "h", "type": "bytes32"}, {"name": "s", "type": "bytes"}],
     "outputs": [{"name": "", "type": "bytes4"}]},
]


def load_vault_abi() -> list[dict]:
    return json.loads(ABI_PATH.read_text())


# ---- revert decoding -------------------------------------------------------------------------

def _canonical_type(inp: dict) -> str:
    t = inp["type"]
    if t.startswith("tuple"):
        inner = ",".join(_canonical_type(c) for c in inp["components"])
        return f"({inner}){t[5:]}"
    return t


class ErrorDecoder:
    def __init__(self, abi: list[dict]) -> None:
        self.by_selector: dict[bytes, tuple[str, list[str], list[str], list[str]]] = {}
        for item in abi:
            if item.get("type") != "error":
                continue
            types = [_canonical_type(i) for i in item["inputs"]]
            sig = f"{item['name']}({','.join(types)})"
            self.by_selector[keccak(text=sig)[:4]] = (
                item["name"], types, [i["name"] for i in item["inputs"]],
                [i.get("internalType", "") for i in item["inputs"]],
            )

    def decode(self, data: bytes | str | None) -> tuple[str, dict[str, Any]]:
        if data is None:
            return "Unknown", {}
        if isinstance(data, str):
            h = data[2:] if data.startswith("0x") else data
            try:
                data = bytes.fromhex(h)
            except ValueError:
                return "Unknown", {"raw": data}
        if len(data) < 4:
            return "Unknown", {"raw": "0x" + data.hex()}
        sel, body = data[:4], data[4:]
        if sel == bytes.fromhex("08c379a0"):
            return "Error", {"message": abi_decode(["string"], body)[0]}
        if sel == bytes.fromhex("4e487b71"):
            return "Panic", {"code": abi_decode(["uint256"], body)[0]}
        if sel not in self.by_selector:
            return "Unknown", {"raw": "0x" + data.hex()}
        name, types, names, internal = self.by_selector[sel]
        try:
            values = abi_decode(types, body)
        except Exception:
            return name, {"raw": "0x" + data.hex()}
        out: dict[str, Any] = {}
        for n, it, v in zip(names, internal, values):
            if isinstance(v, bytes):
                v = "0x" + v.hex()
            elif "Status" in it and isinstance(v, int) and v < len(STATUS_NAMES):
                v = STATUS_NAMES[v]
            out[n] = v
        return name, out


def extract_revert_data(e: BaseException) -> str | None:
    data = getattr(e, "data", None)
    if isinstance(data, str) and data.startswith("0x"):
        return data
    if isinstance(data, dict) and isinstance(data.get("data"), str):
        return data["data"]
    resp = getattr(e, "rpc_response", None)
    if isinstance(resp, dict):
        d = (resp.get("error") or {}).get("data")
        if isinstance(d, str) and d.startswith("0x"):
            return d
    for a in getattr(e, "args", ()):
        if isinstance(a, str) and a.startswith("0x") and len(a) >= 10:
            return a
        if isinstance(a, dict) and isinstance(a.get("data"), str):
            return a["data"]
    return None


class ChainError(Exception):
    """A decoded revert or a failed transaction. `.name` is the custom error name."""

    def __init__(self, fn: str, name: str, args: dict[str, Any] | None = None, detail: str = "") -> None:
        self.fn, self.name, self.err_args, self.detail = fn, name, args or {}, detail
        arg_s = ", ".join(f"{k}={v}" for k, v in self.err_args.items())
        super().__init__(f"{fn} reverted: {name}({arg_s}){' ' + detail if detail else ''}")

    def to_json(self) -> dict[str, Any]:
        return {"function": self.fn, "error": self.name, "args": {k: str(v) for k, v in self.err_args.items()},
                "message": str(self)}


class DryRunError(Exception):
    """Raised instead of broadcasting when DRY_RUN is on (the simulation passed)."""


# ---- data types ------------------------------------------------------------------------------

@dataclass
class OnchainOfframp:
    user: str
    amount: int
    inr_paise: int
    locked_at: int
    completed_at: int
    status: int
    qr_commitment: str

    @property
    def status_name(self) -> str:
        return STATUS_NAMES[self.status] if self.status < len(STATUS_NAMES) else str(self.status)


@dataclass
class Deadlines:
    completable_at: int
    reclaimable_at: int
    dispute_window: int
    releasable_at: int  # while DISPUTED: the arbiter's resolve deadline


@dataclass
class TxResult:
    hash: str
    block: int
    gas_used: int


@dataclass
class DecodedEvent:
    name: str
    args: dict[str, Any]
    tx: str
    log_index: int
    block: int


def _jsonable(v: Any) -> Any:
    if isinstance(v, (bytes, bytearray)):
        return "0x" + bytes(v).hex()
    if isinstance(v, str) and len(v) == 42 and v.startswith("0x"):
        return to_checksum_address(v)
    return v


# ---- client ----------------------------------------------------------------------------------

class ChainClient:
    def __init__(self, cfg: ChainConfig, *, dry_run: bool = False, receipt_timeout: float = 120.0,
                 abi: list[dict] | None = None) -> None:
        self.cfg = cfg
        self.dry_run = dry_run
        self.receipt_timeout = receipt_timeout
        self.abi = abi or load_vault_abi()
        self.w3 = AsyncWeb3(AsyncHTTPProvider(cfg.rpc, request_kwargs={"timeout": 20},
                                              exception_retry_configuration=None))
        self.vault_address = to_checksum_address(cfg.vault)
        self.token_address = to_checksum_address(cfg.token.address)
        self.vault = self.w3.eth.contract(address=self.vault_address, abi=self.abi)
        self.token = self.w3.eth.contract(address=self.token_address, abi=TOKEN_ABI)
        self.errors = ErrorDecoder(self.abi)
        self._locks: dict[str, asyncio.Lock] = {}
        self._nonces: dict[str, int] = {}
        self._events: dict[bytes, Any] = {}
        for item in self.abi:
            if item.get("type") == "event":
                types = ",".join(_canonical_type(i) for i in item["inputs"])
                self._events[keccak(text=f"{item['name']}({types})")] = getattr(self.vault.events, item["name"])
        self._start_block: int | None = None
        self._ts_cache: dict[int, int] = {}

    # -- reads, with retries + backoff for the rate-limited public RPCs --
    async def _read(self, fn: Callable[..., Any], *args: Any, attempts: int = 6, **kwargs: Any) -> Any:
        delay = 0.5
        for i in range(attempts):
            try:
                return await fn(*args, **kwargs)
            except ContractLogicError:
                raise
            except Exception as e:  # network, 429, JSON-RPC rate-limit errors
                if i == attempts - 1:
                    raise
                log.debug("[%s] read retry %d after %s", self.cfg.key, i + 1, type(e).__name__)
                await asyncio.sleep(delay + random.random() * 0.25)
                delay = min(delay * 2, 8.0)

    async def call(self, fn: str, *args: Any) -> Any:
        return await self._read(getattr(self.vault.functions, fn)(*args).call)

    async def chain_id(self) -> int:
        return int(await self._read(lambda: self.w3.eth.chain_id))

    async def block_number(self) -> int:
        return int(await self._read(lambda: self.w3.eth.block_number))

    async def block_timestamp(self, block: int | str = "latest") -> int:
        if isinstance(block, int) and block in self._ts_cache:
            return self._ts_cache[block]
        b = await self._read(self.w3.eth.get_block, block)
        ts = int(b["timestamp"])
        if isinstance(block, int):
            if len(self._ts_cache) > 4096:
                self._ts_cache.clear()
            self._ts_cache[block] = ts
        return ts

    async def chain_time(self) -> int:
        return await self.block_timestamp("latest")

    async def start_block(self) -> int:
        """First block to scan: env override, else the vault's deploy block."""
        if self.cfg.start_block is not None:
            return self.cfg.start_block
        if self._start_block is None:
            r = await self._read(self.w3.eth.get_transaction_receipt, self.cfg.deploy_tx)
            self._start_block = int(r["blockNumber"])
        return self._start_block

    async def offramp_state(self, id_: str | bytes) -> OnchainOfframp:
        user, amount, inr, locked, completed, status, commit = await self.call("offramps", _b32(id_))
        return OnchainOfframp(to_checksum_address(user), int(amount), int(inr), int(locked), int(completed),
                              int(status), hex32(commit))

    async def deadlines(self, id_: str | bytes) -> Deadlines:
        c, r, w, rel = await self.call("deadlines", _b32(id_))
        return Deadlines(int(c), int(r), int(w), int(rel))

    async def hash_intent(self, intent_tuple: tuple) -> bytes:
        return bytes(await self.call("hashIntent", intent_tuple))

    async def hash_dispute(self, id_: str, user: str, deadline: int) -> bytes:
        return bytes(await self.call("hashDispute", _b32(id_), to_checksum_address(user), int(deadline)))

    async def token_call(self, fn: str, *args: Any) -> Any:
        return await self._read(getattr(self.token.functions, fn)(*args).call)

    async def token_nonce(self, owner: str) -> int:
        return int(await self.token_call("nonces", to_checksum_address(owner)))

    async def token_balance(self, owner: str) -> int:
        return int(await self.token_call("balanceOf", to_checksum_address(owner)))

    async def token_allowance(self, owner: str, spender: str) -> int:
        return int(await self.token_call("allowance", to_checksum_address(owner), to_checksum_address(spender)))

    async def token_domain_separator(self) -> bytes:
        return bytes(await self.token_call("DOMAIN_SEPARATOR"))

    async def get_code(self, address: str) -> bytes:
        return bytes(await self._read(self.w3.eth.get_code, to_checksum_address(address)))

    async def is_valid_1271(self, address: str, digest: bytes, sig: bytes) -> bool:
        c = self.w3.eth.contract(address=to_checksum_address(address), abi=ERC1271_ABI)
        try:
            res = await self._read(c.functions.isValidSignature(digest, sig).call, attempts=3)
        except Exception:
            return False
        return bytes(res) == bytes.fromhex("1626ba7e")

    async def get_logs(self, from_block: int, to_block: int) -> list[DecodedEvent]:
        logs = await self._read(self.w3.eth.get_logs, {
            "address": self.vault_address, "fromBlock": from_block, "toBlock": to_block,
        })
        out: list[DecodedEvent] = []
        for lg in logs:
            topics = lg.get("topics") or []
            if not topics:
                continue
            ev = self._events.get(bytes(topics[0]))
            if ev is None:
                continue
            try:
                d = ev().process_log(lg)
            except Exception as e:  # malformed / unexpected log: record and skip
                log.warning("[%s] cannot decode log %s: %s", self.cfg.key, lg.get("transactionHash"), e)
                continue
            out.append(DecodedEvent(
                name=d["event"], args={k: _jsonable(v) for k, v in dict(d["args"]).items()},
                tx="0x" + bytes(d["transactionHash"]).hex(), log_index=int(d["logIndex"]),
                block=int(d["blockNumber"]),
            ))
        out.sort(key=lambda e: (e.block, e.log_index))
        return out

    # -- writes --
    def _lock(self, address: str) -> asyncio.Lock:
        return self._locks.setdefault(address.lower(), asyncio.Lock())

    def _encode(self, fn: str, args: tuple | list) -> str:
        return self.vault.encode_abi(fn, args=list(args))

    async def simulate(self, sender: str, fn: str, args: tuple | list) -> None:
        """eth_call the vault function from `sender`; raises ChainError with the decoded revert."""
        tx = {"from": to_checksum_address(sender), "to": self.vault_address, "data": self._encode(fn, args)}
        try:
            await self._read(self.w3.eth.call, tx, "latest")
        except ContractLogicError as e:
            name, eargs = self.errors.decode(extract_revert_data(e))
            raise ChainError(fn, name, eargs, "" if name != "Unknown" else str(e)) from None

    async def fees(self) -> tuple[int, int]:
        block = await self._read(self.w3.eth.get_block, "latest")
        base = int(block.get("baseFeePerGas") or 0)
        try:
            prio = int(await self._read(lambda: self.w3.eth.max_priority_fee, attempts=2))
        except Exception:
            prio = 10**9
        max_fee = max(2 * base + prio, self.cfg.min_max_fee_wei)
        return max_fee, min(prio, max_fee)

    async def send(self, account: Any, fn: str, args: tuple | list) -> TxResult:
        """Simulate, then sign and broadcast from `account`, serialised per sender; waits for the receipt."""
        async with self._lock(account.address):
            await self.simulate(account.address, fn, args)
            if self.dry_run:
                raise DryRunError(f"DRY_RUN: simulation of {fn} on {self.cfg.key} passed; not broadcasting")
            data = self._encode(fn, args)
            base_tx = {"from": account.address, "to": self.vault_address, "data": data}
            try:
                gas = int(await self._read(self.w3.eth.estimate_gas, base_tx))
            except ContractLogicError as e:
                name, eargs = self.errors.decode(extract_revert_data(e))
                raise ChainError(fn, name, eargs) from None
            pending = int(await self._read(self.w3.eth.get_transaction_count, account.address, "pending"))
            key = account.address.lower()
            nonce = max(pending, self._nonces.get(key, 0))
            max_fee, prio = await self.fees()
            tx = {
                "chainId": self.cfg.chain_id, "to": self.vault_address, "data": data, "value": 0,
                "gas": gas * 12 // 10 + 10_000, "nonce": nonce, "type": 2,
                "maxFeePerGas": max_fee, "maxPriorityFeePerGas": prio,
            }
            signed = account.sign_transaction(tx)
            try:
                h = await self.w3.eth.send_raw_transaction(signed.raw_transaction)
            except Exception:
                self._nonces.pop(key, None)
                raise
            self._nonces[key] = nonce + 1
            tx_hash = "0x" + bytes(h).hex()
            log.info("[%s] sent %s %s", self.cfg.key, fn, self.cfg.tx_url(tx_hash))
            try:
                rcpt = await self.w3.eth.wait_for_transaction_receipt(h, timeout=self.receipt_timeout, poll_latency=1.0)
            except TimeExhausted:
                raise ChainError(fn, "ReceiptTimeout", {"tx": tx_hash}) from None
            if int(rcpt["status"]) != 1:
                raise ChainError(fn, "RevertedOnchain", {"tx": tx_hash})
            return TxResult(tx_hash, int(rcpt["blockNumber"]), int(rcpt["gasUsed"]))

    def tx_url(self, tx_hash: str) -> str:
        return self.cfg.tx_url(tx_hash)

    async def close(self) -> None:
        try:
            await self.w3.provider.disconnect()
        except Exception:
            pass


def _b32(v: str | bytes) -> bytes:
    if isinstance(v, bytes):
        return v
    return bytes.fromhex(v[2:] if v.startswith("0x") else v)

