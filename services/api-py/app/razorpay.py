"""Razorpay collection rail for onramps: Payment Links, the browser callback and the webhook.

  create  POST /v1/payment_links         one link per onramp order (amount = inrPaise, INR)
  pay     the user pays on Razorpay's hosted page
  signal  GET  callback_url?...&razorpay_signature   (browser redirect; may never arrive)
          POST webhook payment_link.paid             (server to server; retried by Razorpay)
  verify  GET  /v1/payment_links/{id}    the only thing trusted before tokens move

Signatures: callback = HMAC-SHA256(link_id|reference_id|status|payment_id, API secret);
webhook = HMAC-SHA256(raw body, webhook secret). Both are hex, compared in constant time, and an
empty secret never verifies anything (HMAC with "" is forgeable by anyone).

Keys come from the env or the repo .env read as text (app/keys.py). They are never logged, and no
error message here includes them.
"""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from .keys import _lookup

API_BASE = "https://api.razorpay.com/v1"
KEY_VAR, SECRET_VAR, WEBHOOK_SECRET_VAR = "RAZORPAY_API_KEY", "RAZORPAY_API_SECRET", "RAZORPAY_WEBHOOK_SECRET"
LINK_EVENTS = ("payment_link.paid", "payment_link.expired", "payment_link.cancelled")


class RazorpayError(RuntimeError):
    """A Razorpay API failure. The message never contains key material."""


@dataclass(frozen=True)
class RazorpayCreds:
    key_id: str
    key_secret: str
    webhook_secret: str

    @property
    def test_mode(self) -> bool:
        return self.key_id.startswith("rzp_test_")

    def __repr__(self) -> str:  # never show secrets
        return f"RazorpayCreds(mode={'test' if self.test_mode else 'live'})"


def missing_creds(env_file: str | Path | None) -> list[str]:
    return [n for n in (KEY_VAR, SECRET_VAR, WEBHOOK_SECRET_VAR) if not _lookup(n, env_file)]


def load_creds(env_file: str | Path | None) -> RazorpayCreds:
    missing = missing_creds(env_file)
    if missing:
        raise RazorpayError(f"{', '.join(missing)} not set (env or repo-root .env)")
    return RazorpayCreds(_lookup(KEY_VAR, env_file), _lookup(SECRET_VAR, env_file),
                         _lookup(WEBHOOK_SECRET_VAR, env_file))


# ---- signatures -----------------------------------------------------------------------------

def hmac_hex(secret: str, message: bytes) -> str:
    return hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


def _same(expected: str, given: str | None) -> bool:
    return bool(given) and hmac.compare_digest(expected, given.strip().lower())


def verify_webhook_signature(raw_body: bytes, signature: str | None, webhook_secret: str) -> bool:
    if not webhook_secret:
        return False
    return _same(hmac_hex(webhook_secret, raw_body), signature)


CALLBACK_FIELDS = ("razorpay_payment_link_id", "razorpay_payment_link_reference_id",
                   "razorpay_payment_link_status", "razorpay_payment_id")


def callback_message(params: Mapping[str, str]) -> bytes:
    return "|".join(params.get(k, "") for k in CALLBACK_FIELDS).encode()


def verify_callback_signature(params: Mapping[str, str], key_secret: str) -> bool:
    if not key_secret or not all(params.get(k) for k in CALLBACK_FIELDS):
        return False
    return _same(hmac_hex(key_secret, callback_message(params)), params.get("razorpay_signature"))


# ---- API client -----------------------------------------------------------------------------

class RazorpayClient:
    def __init__(self, creds: RazorpayCreds, *, base_url: str = API_BASE, timeout: float = 15.0,
                 transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.creds = creds
        self._http = httpx.AsyncClient(base_url=base_url, auth=(creds.key_id, creds.key_secret),
                                       timeout=timeout, transport=transport)

    async def _request(self, method: str, path: str, **kw: Any) -> dict:
        try:
            r = await self._http.request(method, path, **kw)
        except httpx.HTTPError as e:
            raise RazorpayError(f"{method} {path}: {type(e).__name__}") from None
        if r.status_code >= 400:
            try:
                err = r.json().get("error") or {}
            except ValueError:
                err = {}
            raise RazorpayError(f"{method} {path}: HTTP {r.status_code} {err.get('code', '')} "
                                f"{err.get('description', '')}".strip())
        return r.json()

    async def create_payment_link(self, *, amount_paise: int, reference_id: str, description: str,
                                  expire_by: int, callback_url: str, notes: dict[str, str]) -> dict:
        return await self._request("POST", "/payment_links", json={
            "amount": int(amount_paise), "currency": "INR", "accept_partial": False,
            "reference_id": reference_id, "description": description, "expire_by": int(expire_by),
            "notify": {"sms": False, "email": False}, "reminder_enable": False, "notes": notes,
            "callback_url": callback_url, "callback_method": "get",
        })

    async def fetch_payment_link(self, link_id: str) -> dict:
        if not link_id.startswith("plink_") or not link_id[6:].isalnum():
            raise RazorpayError("not a payment link id")
        return await self._request("GET", f"/payment_links/{link_id}")

    async def close(self) -> None:
        await self._http.aclose()
