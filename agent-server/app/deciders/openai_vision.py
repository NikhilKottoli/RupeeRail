"""The AI decider: sends the page state to OpenAI and gets back one action through a JSON schema."""

from __future__ import annotations

import base64
import json

import httpx

from ..models import Action, Candidate
from .base import UNTRUSTED_NOTE, DeciderError

SYSTEM = (
    "You drive a web browser to complete the user's shopping goal, page by page, on any store. Choose exactly one next "
    "action from the candidates shown, by id. A screenshot of the page is attached.\n"
    "Typical steps: search for the item; open the best match you can afford within the limit; add it to the cart; "
    "proceed to checkout; keep the saved delivery address; on the payment page select UPI, preferring an option with a "
    "QR code or 'scan' such as 'Other UPI Apps'; confirm that payment method; then place the order. A QR then appears, "
    "and the system takes over from there. If the QR is showing, use checkout_reached.\n"
    "Rules: Prefer 'Add to Cart' over 'Buy Now'. Never choose cash on delivery, a saved card, EMI, or a wallet, even if "
    "one is preselected: elements marked SELECTED show what's chosen now. Place the order only once UPI is the "
    "selected method. Never add anything beyond the goal, such as subscriptions, protection plans or extra items. "
    "Skip offers and pop-ups by closing them. Scroll if what you need isn't listed.\n"
    "Use need_help for a login wall, CAPTCHA, OTP, no UPI option, nothing affordable within the limit, or a truly "
    f"ambiguous item. You can't pay, and payment isn't your decision. {UNTRUSTED_NOTE}"
)
SCHEMA = {
    "name": "action",
    "strict": True,
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["type", "target", "text", "reason"],
        "properties": {
            "type": {"type": "string", "enum": ["click", "type", "scroll", "key", "wait", "done", "need_help", "checkout_reached"]},
            "target": {"type": ["string", "null"], "description": "Candidate id for click and type"},
            "text": {"type": ["string", "null"], "description": "Text to type, 'up' or 'down' for scroll, or a key name"},
            "reason": {"type": "string"},
        },
    },
}


class OpenAIDecider:
    uses_vision = True

    def __init__(self, api_key: str, model: str, http: httpx.Client | None = None) -> None:
        self.model = model
        self.name = f"openai:{model}"
        self.http = http or httpx.Client(base_url="https://api.openai.com/v1", timeout=45.0)
        self.headers = {"Authorization": f"Bearer {api_key}"}

    def _complete(self, messages: list[dict], schema: dict | None = None) -> str:
        body: dict = {"model": self.model, "messages": messages}
        if schema:
            body["response_format"] = {"type": "json_schema", "json_schema": schema}
        try:
            response = self.http.post("/chat/completions", json=body, headers=self.headers)
        except httpx.HTTPError as e:
            raise DeciderError(f"OpenAI request failed: {type(e).__name__}") from None
        if response.status_code >= 400:
            raise DeciderError(f"OpenAI returned HTTP {response.status_code}")
        try:
            return response.json()["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError, ValueError):
            raise DeciderError("OpenAI returned an unexpected response") from None

    def choose_action(self, state_text: str, candidates: list[Candidate], screenshot: bytes | None = None) -> Action:
        listing = "\n".join(f"{c.id}: {c.describe()}" for c in candidates) or "(none)"
        content: list[dict] = [{"type": "text", "text": f"{state_text}\n\nCandidates:\n{listing}"}]
        if screenshot:
            encoded = base64.b64encode(screenshot).decode()
            content.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{encoded}", "detail": "low"}})
        raw = self._complete([{"role": "system", "content": SYSTEM}, {"role": "user", "content": content}], SCHEMA)
        try:
            return Action(**json.loads(raw), model=self.name)
        except (ValueError, TypeError):
            raise DeciderError("OpenAI returned an invalid action") from None

    def write_text(self, instruction: str, context: str) -> str:
        return self._complete([
            {"role": "system", "content": f"Reply with only the requested text. {UNTRUSTED_NOTE}"},
            {"role": "user", "content": f"{instruction}\n\nContext:\n{context}"},
        ]).strip()
