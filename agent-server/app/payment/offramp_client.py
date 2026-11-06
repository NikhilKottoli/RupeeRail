"""Client for the existing off-ramp backend. See docs/backend-notes.md."""

from __future__ import annotations

from typing import Any

import httpx


class OfframpError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.status, self.code = status, code


class OfframpClient:
    def __init__(self, base_url: str, timeout: float = 150.0) -> None:
        self.http = httpx.Client(base_url=base_url.rstrip("/"), timeout=timeout)

    def _check(self, response: httpx.Response) -> Any:
        if response.status_code >= 400:
            try:
                body = response.json()
            except ValueError:
                body = {}
            raise OfframpError(response.status_code, str(body.get("error", "HTTP_ERROR")), str(body.get("message", "")))
        return response.json()

    def config(self) -> dict:
        return self._check(self.http.get("/config"))

    def health(self) -> dict:
        return self._check(self.http.get("/health"))

    def account(self, chain: str, address: str) -> dict:
        return self._check(self.http.get(f"/account/{chain}/{address}"))

    def quote(self, chain: str, user: str, inr: str, qr_text: str) -> dict:
        return self._check(self.http.post("/quote", json={"chain": chain, "user": user, "inr": inr, "qrText": qr_text}))

    def submit(self, chain: str, intent: dict, user_sig: str, permit: dict) -> dict:
        return self._check(self.http.post("/offramp", json={"chain": chain, "intent": intent, "userSig": user_sig, "permit": permit}))

    def status(self, id_: str, view_token: str | None = None) -> dict:
        headers = {"x-view-token": view_token} if view_token else {}
        return self._check(self.http.get(f"/offramp/{id_}", headers=headers))
