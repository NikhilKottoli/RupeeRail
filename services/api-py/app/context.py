"""Wires settings, chains, keys, store, FX and the quote service into one object the API uses.

Relayer, listener, payout runner, settler, keepers, disputes and onramp are added on top of this.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .chain import ChainClient
from .common import EventHub, RateLimiter
from .config import ChainConfig, Settings, load_chains
from .fx import FxFeed
from .keys import Keys, load_keys
from .quote_service import QuoteService
from .store import Store


@dataclass
class Context:
    settings: Settings
    chains: dict[str, ChainConfig]
    clients: dict[str, Any]
    store: Store
    fx: FxFeed
    keys: Keys
    hub: EventHub
    quotes: QuoteService
    limiter: RateLimiter | None = None
    started_at: float = 0.0
    onchain_roles: dict[str, dict] = field(default_factory=dict)


def build_context(settings: Settings | None = None, *, chains: dict[str, ChainConfig] | None = None,
                  clients: dict[str, Any] | None = None, store: Store | None = None, fx: FxFeed | None = None,
                  keys: Keys | None = None, clock=None) -> Context:
    import time

    settings = settings or Settings()
    chains = chains or load_chains(settings.enabled_chains)
    clients = clients if clients is not None else {
        k: ChainClient(cfg, dry_run=settings.dry_run, receipt_timeout=settings.tx_receipt_timeout_sec)
        for k, cfg in chains.items()
    }
    store = store or Store(settings.db_path)
    fx = fx or FxFeed([c.fx_market for c in chains.values()], max_age_sec=settings.fx_max_age_sec,
                      poll_sec=settings.fx_poll_sec, global_poll_sec=settings.global_ref_poll_sec)
    keys = keys or load_keys(settings.env_file)
    clock = clock or time.time
    return Context(
        settings=settings, chains=chains, clients=clients, store=store, fx=fx, keys=keys, hub=EventHub(),
        quotes=QuoteService(settings, chains, store, fx, keys.quote_signer, clients=clients, clock=clock),
        limiter=RateLimiter(settings.rate_limit_per_min),
    )
