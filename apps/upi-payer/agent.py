"""Watches the backend's public OfframpLocked events and pays each one once through the phone.

Everything goes out from this machine to the backend, so nothing here listens on a port:

    GET /events                         which offramps were locked (public)
    GET /offramp/{id}?refresh=true      who to pay and how much (x-view-token, derived from the id)

The raw QR comes with the payee, so a merchant's `tr`, `mc` and `sign` fields reach the phone intact.
Results stay local: the backend is not told whether the phone paid.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable
from urllib.parse import urlparse

import payer


class BackendRefused(Exception):
    """The backend answered with an API error."""

    def __init__(self, status: int, code: str, message: str = "") -> None:
        super().__init__(f"{status} {code}: {message}")
        self.status, self.code = status, code


def derive_view_token(secret: str, id_: str) -> str:
    """Must match derive_view_token in the backend's app/quote_service.py."""
    return hmac.new(secret.encode(), bytes.fromhex(id_.removeprefix("0x")), hashlib.sha256).hexdigest()


def payment_from_view(view: dict) -> dict:
    """What to pay, from a GET /offramp/{id}?refresh=true response, or BackendRefused if it shouldn't be paid now.

    The backend only starts dispatching after the lock matched its quote, so `payout` also confirms the match.
    """
    onchain = view.get("onchain") or {}
    status, payout = onchain.get("status"), view.get("payout")
    if status in (None, "NONE"):
        raise BackendRefused(409, "NOT_READY", "not locked onchain yet")
    if status not in ("LOCKED", "COMPLETED") or payout in ("FAILED", "SKIPPED", "NEEDS_OPERATOR"):
        raise BackendRefused(409, "NOT_PAYABLE", f"offramp is {status}, payout {payout}")
    if payout not in ("DISPATCHING", "DISPATCHED"):
        raise BackendRefused(409, "NOT_READY", "the backend has not matched this lock yet")
    payee = view.get("payee") or {}
    if not payee.get("upiId"):
        raise BackendRefused(403, "NO_PAYEE", "no payee returned: is VIEW_TOKEN_SECRET the same as the backend's?")
    return {"lockedAt": onchain["lockedAt"], "inrPaise": onchain["inrPaise"], "upiId": payee["upiId"],
            "payeeName": payee.get("name"), "qrText": view.get("qrText")}


def check_api_url(url: str) -> str:
    """The token and payee details must not cross the internet unencrypted."""
    parsed = urlparse(url)
    if parsed.scheme == "https" and parsed.hostname:
        return url.rstrip("/")
    if parsed.scheme == "http" and parsed.hostname:
        try:
            private = parsed.hostname == "localhost" or ipaddress.ip_address(parsed.hostname).is_private
        except ValueError:
            private = False
        if private:
            return url.rstrip("/")
        raise ValueError("RUPEERAIL_API_URL must use https unless it is a private address")
    raise ValueError("RUPEERAIL_API_URL must be an http(s) URL")


class Backend:
    def __init__(self, base_url: str, secret: str, timeout: float = 15.0) -> None:
        self.base_url, self.secret, self.timeout = check_api_url(base_url), secret, timeout

    def _get(self, path: str, headers: dict | None = None) -> Any:
        req = urllib.request.Request(self.base_url + path, headers={"accept": "application/json", **(headers or {})})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            try:
                body = json.load(e)
            except ValueError:
                body = {}
            raise BackendRefused(e.code, str(body.get("error") or "HTTP_ERROR"), str(body.get("message") or "")) from None

    def events(self, limit: int = 100) -> list[dict]:
        return self._get(f"/events?limit={limit}")

    def payment(self, id_: str) -> dict:
        view = self._get(f"/offramp/{id_}?refresh=true", {"x-view-token": derive_view_token(self.secret, id_)})
        return payment_from_view(view)


@dataclass
class AgentConfig:
    poll_sec: float = 2.0
    max_lock_age_sec: int = 600  # never pay an old lock, such as history seen on the first start
    resolve_wait_sec: float = 60.0  # how long to wait for the backend to process a lock it reported


class Agent:
    def __init__(self, backend: Any, cfg: AgentConfig, payer_cfg: payer.Config, adb: Any | None = None,
                 clock: Callable[[], float] = time.time, sleep: Callable[[float], None] = time.sleep,
                 log: Callable[[str], None] = payer.log) -> None:
        self.backend, self.cfg, self.payer_cfg, self.adb = backend, cfg, payer_cfg, adb
        self.clock, self.sleep, self.log = clock, sleep, log
        self.settled: set[str] = set()  # this run's finished ids; the ledger covers restarts
        self.first_seen: dict[str, float] = {}
        self.last_problem: str | None = None

    def _problem(self, message: str) -> None:
        if message != self.last_problem:  # once per problem, not once per poll
            self.log(f"WARNING {message}")
            self.last_problem = message

    def tick(self) -> None:
        try:
            events = self.backend.events()
        except Exception as e:
            self._problem(f"can't read events: {e}")
            return
        for ev in reversed(events):  # the API returns newest first; pay in the order they were locked
            id_ = str(ev.get("id") or "").lower()
            if ev.get("name") == "OfframpLocked" and id_ and id_ not in self.settled:
                self._handle(id_)

    def _handle(self, id_: str) -> None:
        now = self.clock()
        try:
            p = self.backend.payment(id_)
        except BackendRefused as e:
            if e.code == "NOT_PAYABLE":
                self.settled.add(id_)
            elif e.code in ("NOT_READY", "NOT_FOUND", "NO_PAYEE"):
                if now - self.first_seen.setdefault(id_, now) >= self.cfg.resolve_wait_sec:
                    self.settled.add(id_)  # nothing was paid
                    self.log(f"ALERT gave up on {id_}, nothing was paid: {e}")
            else:  # for example the backend or the RPC is down: it can still be paid once that's fixed
                self._problem(f"backend refused the payee lookup: {e}")
            return
        except Exception as e:
            self._problem(f"can't look up {id_[:10]}: {e}")
            return

        self.last_problem = None
        self.settled.add(id_)
        age = now - int(p["lockedAt"])
        if age > self.cfg.max_lock_age_sec:
            return

        job = {"idempotencyKey": id_, "upiId": p.get("upiId"), "payeeName": p.get("payeeName"),
               "inrPaise": int(p["inrPaise"]), "qrText": p.get("qrText")}
        _, result = payer.run_job(job, self.payer_cfg, self.adb, clock=self.clock, sleep=self.sleep)
        self._report(id_, p, result)

    def _report(self, id_: str, p: dict, result: dict) -> None:
        if result.get("replay"):
            return
        status, detail = result.get("status"), result.get("detail") or ""
        paise = int(p["inrPaise"])
        if status == "paid":
            self.log(f"PAID Rs {payer.format_paise(paise)} for {id_}")
        elif status == "dry_run":
            self.log(f"dry run for {id_}: nothing was paid")
        elif status == "unknown":
            self.log(f"ALERT outcome UNKNOWN for {id_}: {detail}. Check the phone before doing anything else.")
        else:
            self.log(f"ALERT NOT PAID Rs {payer.format_paise(paise)} for {id_}: {detail}. "
                     "The backend isn't told: check the offramp and reverse it if it completed.")

    def run(self) -> None:
        while True:
            self.tick()
            self.sleep(self.cfg.poll_sec)
