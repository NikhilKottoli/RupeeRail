"""Fakes and fixtures. Unit tests use throwaway keys only, never the real ones."""

from __future__ import annotations

import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from eth_account import Account

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.chain import ChainError, DecodedEvent, Deadlines, OnchainOfframp, TxResult  # noqa: E402
from app.commitment import hex32, qr_commitment  # noqa: E402
from app.config import ChainConfig, Settings, TokenConfig  # noqa: E402
from app.fx import FxFeed, MarketRate  # noqa: E402
from app.keys import Keys  # noqa: E402
from app.store import Store  # noqa: E402

# Well-known throwaway keys (anvil/hardhat dev accounts #1-#3): NOT the project's keys.
QUOTE_SIGNER_PK = "0x59c6995e998f97a5a0044966f0945389dc9e86dae88c7a8412f4603b6b78690d"
OPERATOR_PK = "0x5de4111afa1a4b94908f83103eb1f1706367c2e68ca870fc3fb9a804cdab365a"
USER_PK = "0x7c852118294e51e653712a81e05800f419141751be58f605c371e15141b007a6"

ARC = ChainConfig(
    key="arc", name="Arc Testnet", chain_id=5042002, rpc="http://fake", explorer="https://explorer.testnet.arc.io",
    vault="0x878734374e6131fceeeb83fa8605ab49f4d21836", deploy_tx="0x" + "00" * 32,
    token=TokenConfig("USDC", "0x3600000000000000000000000000000000000000", 6, "USDC", "2"),
    fx_market="USDCINR", min_max_fee_wei=20 * 10**9,
)

QR = "upi://pay?pa=chaipoint@okaxis&pn=Chai%20Point&cu=INR"


@dataclass
class FakeChain:
    """In-memory stand-in for ChainClient implementing the vault's state machine for sends."""

    cfg: ChainConfig = ARC
    now: int = field(default_factory=lambda: int(time.time()))
    head: int = 1000
    offramps: dict[str, OnchainOfframp] = field(default_factory=dict)
    dls: dict[str, Deadlines] = field(default_factory=dict)
    code: dict[str, bytes] = field(default_factory=dict)
    balances: dict[str, int] = field(default_factory=dict)
    nonces: dict[str, int] = field(default_factory=dict)
    allowances: dict[str, int] = field(default_factory=dict)
    valid_1271: set[tuple[str, bytes]] = field(default_factory=set)
    logs: list[DecodedEvent] = field(default_factory=list)
    sent: list[tuple[str, list]] = field(default_factory=list)
    fail: dict[str, ChainError] = field(default_factory=dict)
    onramp_done: set[str] = field(default_factory=set)
    pool_balance: int = 10**12
    daily_cap_left: int = 10**12
    get_logs_calls: list[tuple[int, int]] = field(default_factory=list)
    _tx: int = 0

    # reads
    async def offramp_state(self, id_):
        return self.offramps.get(_k(id_), OnchainOfframp("0x" + "00" * 20, 0, 0, 0, 0, 0, "0x" + "00" * 32))

    async def deadlines(self, id_):
        return self.dls.get(_k(id_), Deadlines(0, 0, 0, 0))

    async def chain_time(self):
        return self.now

    async def block_timestamp(self, block="latest"):
        return self.now

    async def block_number(self):
        return self.head

    async def start_block(self):
        return 1

    async def chain_id(self):
        return self.cfg.chain_id

    async def get_code(self, address):
        return self.code.get(address.lower(), b"")

    async def is_valid_1271(self, address, digest, sig):
        return (address.lower(), sig) in self.valid_1271

    async def token_nonce(self, owner):
        return self.nonces.get(owner.lower(), 0)

    async def token_balance(self, owner):
        return self.balances.get(owner.lower(), 0)

    async def token_allowance(self, owner, spender):
        return self.allowances.get(owner.lower(), 0)

    async def call(self, fn, *args):
        if fn == "onrampDone":
            return hex32(args[0]) in self.onramp_done
        if fn == "poolBalance":
            return self.pool_balance
        if fn == "remainingDailyCap":
            return self.daily_cap_left
        raise NotImplementedError(fn)

    async def get_logs(self, frm, to):
        self.get_logs_calls.append((frm, to))
        return [e for e in self.logs if frm <= e.block <= to]

    def tx_url(self, h):
        return f"{self.cfg.explorer}/tx/{h}"

    # writes
    async def send(self, account, fn, args):
        if fn in self.fail:
            raise self.fail[fn]
        self.sent.append((fn, list(args)))
        self._tx += 1
        id_ = hex32(args[0]) if fn != "offramp" else hex32(args[0][0])
        o = self.offramps.get(id_)
        if fn == "offramp":
            it = args[0]
            self.offramps[id_] = OnchainOfframp(it[1], it[2], it[3], self.now, 0, 1, hex32(it[5]))
            self.dls[id_] = Deadlines(self.now + 15, self.now + 86400, 86400, 0)
        elif fn == "completeOfframp":
            o.status, o.completed_at = 2, self.now
            d = self.dls[id_]
            d.releasable_at = self.now + d.dispute_window
        elif fn in ("refundOfframp", "reclaimOfframp"):
            o.status = 3
        elif fn in ("reverseOfframp", "claimDisputeTimeout"):
            o.status = 4
        elif fn == "releaseOfframp":
            o.status = 5
        elif fn == "disputeOfframpWithSig":
            o.status = 6
            self.dls[id_].releasable_at = self.now + 7 * 86400
        elif fn == "onramp":
            self.onramp_done.add(id_)
        return TxResult("0x" + f"{self._tx:064x}", self.head, 21000)

    def lock(self, id_, user, amount, inr_paise, commitment, status=1, locked_at=None):
        t = self.now if locked_at is None else locked_at
        self.offramps[_k(id_)] = OnchainOfframp(user, amount, inr_paise, t, 0, status, commitment)
        self.dls[_k(id_)] = Deadlines(t + 15, t + 86400, 86400, 0)

    async def close(self):
        pass


def _k(id_) -> str:
    return hex32(id_) if isinstance(id_, bytes) else id_.lower()


class FakeSettler:
    def __init__(self) -> None:
        self.refunds: list[tuple[str, str, int]] = []
        self.completes: list[tuple[str, str]] = []

    async def refund(self, chain, id_, reason):
        self.refunds.append((chain, id_, reason))
        return "refunded"

    async def complete(self, chain, id_):
        self.completes.append((chain, id_))
        return "completed"


class FakeRunner:
    def __init__(self) -> None:
        self.dispatched: list[tuple[dict, Any]] = []

    async def dispatch(self, payment, parsed):
        self.dispatched.append((payment, parsed))
        return "dispatched"


@pytest.fixture(autouse=True)
def hermetic_env(request, monkeypatch, tmp_path):
    """Unit tests never read the real repo .env (COLLECT_RAIL, Razorpay keys...). Live tests still do."""
    if request.node.get_closest_marker("live"):
        return
    monkeypatch.setenv("RUPEERAIL_ENV_FILE", str(tmp_path / "absent.env"))
    for name in ("APP_ENV", "COLLECT_RAIL", "PUBLIC_BASE_URL", "WEB_APP_URL", "RAZORPAY_API_KEY",
                 "RAZORPAY_API_SECRET", "RAZORPAY_WEBHOOK_SECRET"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def keys() -> Keys:
    return Keys(quote_signer=Account.from_key(QUOTE_SIGNER_PK), paymaster=Account.from_key(OPERATOR_PK),
                settler=Account.from_key(OPERATOR_PK))


@pytest.fixture
def user():
    return Account.from_key(USER_PK)


@pytest.fixture
def store():
    s = Store(":memory:")
    yield s
    s.close()


@pytest.fixture
def chain():
    return FakeChain()


@pytest.fixture
def fx():
    f = FxFeed(["USDCINR"])
    f.set(MarketRate("USDCINR", "100.14", "101.00", time.time()))
    return f


@pytest.fixture
def settings(tmp_path):
    s = Settings()
    s.db_path = ":memory:"
    s.workers = False
    s.admin_token = "test-admin"
    s.spread_bps = 80
    s.quote_ttl_sec = 90
    s.min_inr_paise, s.max_inr_paise, s.daily_user_inr_paise = 100, 100_000, 500_000
    return s


def store_quote(store: Store, *, id_: str, user: str, amount: int = 100666, inr_paise: int = 1000,
                rate_e6: int = 99338880, qr_text: str = QR, salt: str = "0x" + "ab" * 32, chain: str = "arc",
                deadline: int | None = None, status: str = "QUOTED") -> dict:
    q = {
        "id": id_, "chain": chain, "user": user, "amount": str(amount), "inr_paise": inr_paise,
        "rate_e6": str(rate_e6), "qr_text": qr_text, "salt": salt,
        "qr_commitment": hex32(qr_commitment(salt, qr_text)), "deadline": deadline or int(time.time()) + 90,
        "quote_sig": "0x" + "00" * 65, "status": status,
    }
    store.insert_quote(q)
    return q
