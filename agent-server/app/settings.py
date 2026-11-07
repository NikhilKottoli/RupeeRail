"""Configuration, from the environment and then agent-server/.env. See .env.example."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

SERVER_DIR = Path(__file__).resolve().parent.parent
DEFAULT_BACKEND = "https://rupee-rail.indiasouthcentral.cloudapp.azure.com"
# Long enough that guessing isn't practical, and only characters a WebSocket subprotocol can carry.
ACCESS_CODE = re.compile(r"^[A-Za-z0-9_-]{24,}$")


def parse_access_codes(value: str) -> tuple[str, ...]:
    """Refuses a weak or malformed code, so a typo can't leave a server that spends money on an easy password."""
    codes = tuple(code.strip() for code in value.split(",") if code.strip())
    if not all(ACCESS_CODE.match(code) for code in codes):
        raise ValueError("each AGENT_ACCESS_CODES entry needs at least 24 letters, digits, - or _")
    return codes


def load_env_file(path: Path = SERVER_DIR / ".env") -> dict[str, str]:
    values: dict[str, str] = {}
    if path.exists():
        for line in path.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                key, _, value = line.partition("=")
                if value.strip():
                    values[key.strip()] = value.strip().strip("\"'")
    return values


@dataclass
class Settings:
    backend_url: str = DEFAULT_BACKEND
    chain: str = "arc"
    signer: str = "circle"  # circle or local
    local_signer_key: str = ""
    decider: str = "auto"  # auto, openai or rule. auto uses OpenAI when there's a key
    openai_api_key: str = ""
    openai_model: str = "gpt-5.4-mini"
    openai_base: str = "https://api.openai.com/v1"  # any service with the same API
    db_path: str = str(SERVER_DIR / "data" / "agents.sqlite")
    global_max_inr: int = 1000  # no agent can go above this, whatever it's set to. Matches the backend and UPI Lite
    max_budget_usdc: Decimal = Decimal("10")  # no agent's on-chain budget can go above this
    payer_ledger: str = str(SERVER_DIR.parent / "apps" / "upi-payer" / "data" / "payer.sqlite")
    webhook_url: str = ""
    extension_id: str = ""  # when set, only this extension may connect
    max_steps: int = 60
    time_limit_sec: float = 600.0
    result_wait_sec: float = 120.0
    dev_loop: bool = False  # lets a local tool queue rehearsals. They can't order or pay
    debug_dir: str = ""  # saves each step's page text and screenshot here, so it can hold personal details
    arkiv_publisher_url: str = ""  # unset, nothing is ever published to Arkiv
    arkiv_publisher_token: str = ""
    access_codes: tuple[str, ...] = ()  # when set, every request needs one of these
    open_access: bool = False  # lets a proxied server run with no codes, so anyone on the internet can use its agents
    payer_results_token: str = ""  # when set, the payer reports results over the network, not through its ledger file

    @classmethod
    def load(cls) -> "Settings":
        e = {**load_env_file(), **os.environ}
        base = cls()
        return cls(
            backend_url=e.get("RUPEERAIL_API_URL") or base.backend_url,
            chain=e.get("AGENT_CHAIN") or base.chain,
            signer=e.get("AGENT_SIGNER") or base.signer,
            local_signer_key=e.get("LOCAL_SIGNER_KEY") or "",
            decider=e.get("AGENT_DECIDER") or base.decider,
            openai_api_key=e.get("OPENAI_API_KEY") or e.get("OPEN_AI_KEY") or "",
            openai_model=e.get("OPENAI_MODEL") or base.openai_model,
            openai_base=e.get("openai_base") or e.get("OPENAI_BASE") or base.openai_base,
            db_path=e.get("AGENT_DB") or base.db_path,
            global_max_inr=int(e.get("AGENT_GLOBAL_MAX_INR") or base.global_max_inr),
            max_budget_usdc=Decimal(e.get("AGENT_MAX_BUDGET_USDC") or base.max_budget_usdc),
            payer_ledger=e.get("PAYER_LEDGER") or base.payer_ledger,
            webhook_url=e.get("AGENT_WEBHOOK_URL") or "",
            extension_id=e.get("AGENT_EXTENSION_ID") or "",
            dev_loop=e.get("AGENT_DEV_LOOP") == "1",
            debug_dir=e.get("AGENT_DEBUG_DIR") or "",
            arkiv_publisher_url=e.get("ARKIV_PUBLISHER_URL") or "",
            arkiv_publisher_token=e.get("ARKIV_PUBLISHER_TOKEN") or "",
            access_codes=parse_access_codes(e.get("AGENT_ACCESS_CODES") or ""),
            open_access=e.get("AGENT_OPEN_ACCESS") == "1",
            payer_results_token=e.get("PAYER_RESULTS_TOKEN") or "",
        )
