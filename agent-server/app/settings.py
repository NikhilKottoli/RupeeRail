"""Configuration, from the environment and then agent-server/.env. See .env.example."""

from __future__ import annotations

import os
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

SERVER_DIR = Path(__file__).resolve().parent.parent
DEFAULT_BACKEND = "https://rupee-rail.indiasouthcentral.cloudapp.azure.com"


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
        )
