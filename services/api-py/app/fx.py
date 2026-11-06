"""CoinDCX FX poller (server-side: CoinDCX sends no CORS headers) plus an optional CoinGecko
reference rate (its free tier often returns 429; failures are tolerated).

Quotes are refused when the market's data is older than FX_MAX_AGE_SEC (default 2 minutes).
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

import httpx

log = logging.getLogger("rupeerail.fx")

COINDCX_TICKER = "https://api.coindcx.com/exchange/ticker"
COINGECKO = "https://api.coingecko.com/api/v3/simple/price?ids=tether,usd-coin&vs_currencies=inr"


@dataclass
class MarketRate:
    market: str
    bid: str
    ask: str
    fetched_at: float


class FxFeed:
    def __init__(self, markets: list[str], max_age_sec: float = 120.0, poll_sec: float = 15.0,
                 global_poll_sec: float = 60.0, client: httpx.AsyncClient | None = None) -> None:
        self.markets = sorted(set(markets))
        self.max_age_sec = max_age_sec
        self.poll_sec = poll_sec
        self.global_poll_sec = global_poll_sec
        self._client = client
        self._rates: dict[str, MarketRate] = {}
        self._global: dict[str, float | None] = {"USDTINR": None, "USDCINR": None}
        self.global_fetched_at: float | None = None
        self.last_error: str | None = None
        self.last_poll_at: float | None = None

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=8.0, headers={"user-agent": "rupeerail-api/0.1"})
        return self._client

    async def poll_markets(self) -> None:
        try:
            r = await self._http().get(COINDCX_TICKER)
            r.raise_for_status()
            rows = r.json()
            t = time.time()
            for row in rows:
                m = row.get("market")
                if m in self.markets and row.get("bid") not in (None, "") and row.get("ask") not in (None, ""):
                    self._rates[m] = MarketRate(m, str(row["bid"]), str(row["ask"]), t)
            missing = [m for m in self.markets if m not in self._rates]
            self.last_error = f"markets missing from ticker: {missing}" if missing else None
        except Exception as e:
            self.last_error = f"CoinDCX: {type(e).__name__}: {e}"
            log.warning("[fx] %s", self.last_error)
        finally:
            self.last_poll_at = time.time()

    async def poll_global(self) -> None:
        try:
            r = await self._http().get(COINGECKO)
            if r.status_code != 200:
                return  # 429 on the free tier is normal; reference only
            j = r.json()
            self._global = {"USDTINR": (j.get("tether") or {}).get("inr"), "USDCINR": (j.get("usd-coin") or {}).get("inr")}
            self.global_fetched_at = time.time()
        except Exception:
            pass

    def get(self, market: str) -> MarketRate | None:
        """Fresh rate, or None if missing or older than max_age_sec (quotes must be refused then)."""
        r = self._rates.get(market)
        if r is None or time.time() - r.fetched_at > self.max_age_sec:
            return None
        return r

    def global_ref(self, market: str) -> float | None:
        return self._global.get(market)

    def set(self, rate: MarketRate) -> None:
        """Test hook."""
        self._rates[rate.market] = rate

    def status(self) -> dict:
        t = time.time()
        return {
            "markets": {
                m: ({"bid": r.bid, "ask": r.ask, "ageSec": round(t - r.fetched_at, 1), "fresh": self.get(m) is not None}
                    if (r := self._rates.get(m)) else None)
                for m in self.markets
            },
            "globalRef": self._global,
            "lastError": self.last_error,
        }

    async def run(self) -> None:
        async def markets_loop() -> None:
            while True:
                await self.poll_markets()
                await asyncio.sleep(self.poll_sec)

        async def global_loop() -> None:
            while True:
                await self.poll_global()
                await asyncio.sleep(self.global_poll_sec)

        await asyncio.gather(markets_loop(), global_loop())

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
