"""Chains (read from deployments/*.json at startup) and settings (from env)."""

from __future__ import annotations

import json
import os
import shlex
import sys
from dataclasses import dataclass, field
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
SERVICE_DIR = APP_DIR.parent
REPO_ROOT = SERVICE_DIR.parent.parent
ABI_PATH = APP_DIR / "abi" / "ScanPayVault.json"

GWEI = 10**9
# The deployed host (Caddy serves the PWA and proxies the API on it). Razorpay's callback and webhook
# point here unless PUBLIC_BASE_URL overrides it, e.g. with an ngrok URL for local testing.
AZURE_BASE_URL = "https://rupee-rail.indiasouthcentral.cloudapp.azure.com"


@dataclass(frozen=True)
class TokenConfig:
    symbol: str
    address: str
    decimals: int
    permit_name: str
    permit_version: str


@dataclass(frozen=True)
class ChainConfig:
    key: str
    name: str
    chain_id: int
    rpc: str
    explorer: str
    vault: str
    deploy_tx: str
    token: TokenConfig
    fx_market: str
    min_max_fee_wei: int
    start_block: int | None = None

    def tx_url(self, tx_hash: str) -> str:
        return f"{self.explorer}/tx/{tx_hash}"

    def address_url(self, address: str) -> str:
        return f"{self.explorer}/address/{address}"


# key -> (deployment file, display name, CoinDCX market, min maxFeePerGas in gwei)
_CHAIN_META = {
    "arc": ("arc-testnet.json", "Arc Testnet", "USDCINR", 20),
    "whitechain": ("whitechain-sepolia.json", "Whitechain Sepolia", "USDTINR", 0),
}


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    return int(raw) if raw not in (None, "") else default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw in (None, ""):
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def load_chain(key: str, deployments_dir: Path | None = None) -> ChainConfig:
    file, name, market, min_gwei = _CHAIN_META[key]
    d = json.loads(((deployments_dir or REPO_ROOT / "deployments") / file).read_text())
    t = d["token"]
    prefix = key.upper()
    start = os.environ.get(f"{prefix}_START_BLOCK")
    return ChainConfig(
        key=key,
        name=name,
        chain_id=int(d["chainId"]),
        rpc=os.environ.get(f"{prefix}_RPC_URL") or d["rpc"],
        explorer=d["explorer"],
        vault=d["vault"],
        deploy_tx=d["deployTx"],
        token=TokenConfig(
            symbol=t["symbol"],
            address=t["address"],
            decimals=int(t["decimals"]),
            permit_name=t["permitDomain"]["name"],
            permit_version=t["permitDomain"]["version"],
        ),
        fx_market=market,
        min_max_fee_wei=_env_int(f"{prefix}_MIN_MAX_FEE_GWEI", min_gwei) * GWEI,
        start_block=int(start) if start else None,
    )


def load_chains(enabled: list[str] | None = None) -> dict[str, ChainConfig]:
    keys = enabled or list(_CHAIN_META)
    return {k: load_chain(k) for k in keys}


def _default_payout_command() -> str:
    # Dev/prod: APP_ENV=prod runs the RazorpayX rail; dev (the default) keeps the stub below. See app/environment.py.
    from .environment import prod_payout_command

    if (prod := prod_payout_command()) is not None:
        return prod
    return f"{shlex.quote(sys.executable)} {shlex.quote(str(SERVICE_DIR / 'scripts' / 'dev_payout_stub.py'))}"


@dataclass
class Settings:
    port: int = field(default_factory=lambda: _env_int("PORT", 8788))
    db_path: str = field(default_factory=lambda: os.environ.get("DB_PATH") or str(SERVICE_DIR / "data" / "rupeerail.sqlite"))
    enabled_chains: list[str] = field(
        default_factory=lambda: [c for c in (os.environ.get("CHAINS") or "arc,whitechain").split(",") if c in _CHAIN_META]
    )
    spread_bps: int = field(default_factory=lambda: _env_int("SPREAD_BPS", 80))
    quote_ttl_sec: int = field(default_factory=lambda: _env_int("QUOTE_TTL_SEC", 90))
    min_inr_paise: int = field(default_factory=lambda: _env_int("MIN_INR_PAISE", 100))  # Rs 1
    max_inr_paise: int = field(default_factory=lambda: _env_int("MAX_INR_PAISE", 100_000))  # Rs 1,000
    daily_user_inr_paise: int = field(default_factory=lambda: _env_int("DAILY_USER_INR_PAISE", 500_000))  # Rs 5,000
    fx_poll_sec: float = field(default_factory=lambda: float(_env_int("FX_POLL_SEC", 15)))
    fx_max_age_sec: float = field(default_factory=lambda: float(_env_int("FX_MAX_AGE_SEC", 120)))
    global_ref_poll_sec: float = field(default_factory=lambda: float(_env_int("GLOBAL_REF_POLL_SEC", 60)))
    listener_poll_sec: float = field(default_factory=lambda: float(_env_int("LISTENER_POLL_SEC", 3)))
    listener_max_range: int = field(default_factory=lambda: min(_env_int("LISTENER_MAX_RANGE", 2000), 2000))
    listener_confirmations: int = field(default_factory=lambda: _env_int("LISTENER_CONFIRMATIONS", 1))
    settler_tick_sec: float = field(default_factory=lambda: float(_env_int("SETTLER_TICK_SEC", 3)))
    keeper_tick_sec: float = field(default_factory=lambda: float(_env_int("KEEPER_TICK_SEC", 30)))
    payout_command: str = field(default_factory=lambda: os.environ.get("PAYOUT_COMMAND") or _default_payout_command())
    payout_timeout_sec: float = field(default_factory=lambda: float(_env_int("PAYOUT_TIMEOUT_SEC", 30)))
    payout_max_lock_age_sec: int = field(default_factory=lambda: _env_int("PAYOUT_MAX_LOCK_AGE_SEC", 600))
    complete_alert_sec: int = field(default_factory=lambda: _env_int("COMPLETE_ALERT_SEC", 3600))
    rate_limit_per_min: int = field(default_factory=lambda: _env_int("RATE_LIMIT_PER_MIN", 30))
    expose_payee_by_id: bool = field(default_factory=lambda: _env_bool("EXPOSE_PAYEE_BY_ID", False))
    admin_token: str = field(default_factory=lambda: os.environ.get("ADMIN_TOKEN") or "")
    view_token_secret: str = field(default_factory=lambda: os.environ.get("VIEW_TOKEN_SECRET") or "")
    collect_upi_id: str = field(default_factory=lambda: os.environ.get("COLLECT_UPI_ID") or "rupeerail@okaxis")
    workers: bool = field(default_factory=lambda: _env_bool("WORKERS", True))
    dry_run: bool = field(default_factory=lambda: _env_bool("DRY_RUN", False))
    tx_receipt_timeout_sec: float = field(default_factory=lambda: float(_env_int("TX_RECEIPT_TIMEOUT_SEC", 120)))
    env_file: str = field(default_factory=lambda: os.environ.get("RUPEERAIL_ENV_FILE") or str(REPO_ROOT / ".env"))
    # Onramp collection (app/environment.py, app/razorpay.py). These three may also live in the .env
    # file (read as text); filled in __post_init__ because they depend on env_file.
    collect_rail: str = ""
    public_base_url: str = ""  # https URL Razorpay reaches us at; AZURE_BASE_URL unless overridden (ngrok locally)
    web_app_url: str = ""  # where the browser lands after paying; defaults to public_base_url
    onramp_link_ttl_sec: int = field(default_factory=lambda: _env_int("ONRAMP_LINK_TTL_SEC", 960))  # Razorpay min 15 min
    onramp_tick_sec: float = field(default_factory=lambda: float(_env_int("ONRAMP_TICK_SEC", 15)))

    def __post_init__(self) -> None:
        from .environment import collect_rail
        from .keys import _lookup

        def get(name: str) -> str:
            return _lookup(name, self.env_file) or ""

        if not self.collect_rail:
            self.collect_rail = collect_rail({"APP_ENV": os.environ.get("APP_ENV", ""),
                                              "COLLECT_RAIL": get("COLLECT_RAIL")}).name
        self.public_base_url = (self.public_base_url or get("PUBLIC_BASE_URL") or AZURE_BASE_URL).rstrip("/")
        self.web_app_url = (self.web_app_url or get("WEB_APP_URL") or self.public_base_url).rstrip("/")

    def limits(self) -> dict:
        return {
            "minInrPaise": self.min_inr_paise,
            "maxInrPaise": self.max_inr_paise,
            "dailyUserInrPaise": self.daily_user_inr_paise,
        }
