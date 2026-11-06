"""Configuration, from the environment and then agent-server/.env. See .env.example."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
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
    circle_wallet: str = "0x179dfbc07b23541849a2cb6d38d991d9ec7cda88"
    local_signer_key: str = ""
    decider: str = "auto"  # auto, openai or rule. auto uses OpenAI when there's a key
    openai_api_key: str = ""
    openai_model: str = "gpt-5.4-mini"
    global_max_inr: int = 30
    allowed_domains: list[str] = field(default_factory=lambda: ["127.0.0.1"])
    payer_ledger: str = str(SERVER_DIR.parent / "apps" / "upi-payer" / "data" / "payer.sqlite")
    webhook_url: str = ""
    extension_id: str = ""  # when set, only this extension may connect
    max_steps: int = 60
    time_limit_sec: float = 600.0
    result_wait_sec: float = 120.0

    @classmethod
    def load(cls) -> "Settings":
        e = {**load_env_file(), **os.environ}
        domains = e.get("AGENT_ALLOWED_DOMAINS")
        base = cls()
        return cls(
            backend_url=e.get("RUPEERAIL_API_URL") or base.backend_url,
            chain=e.get("AGENT_CHAIN") or base.chain,
            signer=e.get("AGENT_SIGNER") or base.signer,
            circle_wallet=e.get("CIRCLE_WALLET") or base.circle_wallet,
            local_signer_key=e.get("LOCAL_SIGNER_KEY") or "",
            decider=e.get("AGENT_DECIDER") or base.decider,
            openai_api_key=e.get("OPENAI_API_KEY") or e.get("OPEN_AI_KEY") or "",
            openai_model=e.get("OPENAI_MODEL") or base.openai_model,
            global_max_inr=int(e.get("AGENT_GLOBAL_MAX_INR") or base.global_max_inr),
            allowed_domains=[d.strip() for d in domains.split(",") if d.strip()] if domains else base.allowed_domains,
            payer_ledger=e.get("PAYER_LEDGER") or base.payer_ledger,
            webhook_url=e.get("AGENT_WEBHOOK_URL") or "",
            extension_id=e.get("AGENT_EXTENSION_ID") or "",
        )
