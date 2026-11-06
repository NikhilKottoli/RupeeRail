"""The AI decider: sends the page state to OpenAI and gets back one action through a JSON schema."""

from __future__ import annotations

import base64
import json
import re
import time
from typing import Callable

import httpx

from ..models import Action, Candidate
from .base import UNTRUSTED_NOTE, DeciderError

RETRY_STATUSES = {429, 500, 502, 503, 504}
MAX_RETRIES = 4
MAX_RETRY_WAIT_SEC = 30.0
IMAGES_REFUSED = re.compile(r"multimodal|image.{0,40}(not supported|unsupported)|(not support|doesn't support|does not support).{0,40}image|vision.{0,40}(not supported|unsupported)", re.IGNORECASE)

# Fixed rules first, so nothing in an agent's own instructions can loosen them.
SYSTEM = (
    "You are a long-lived personal agent. You drive a web browser to do what the person asks, page by page, on any "
    "site. Choose exactly one next action from the candidates shown, by id. A screenshot of the page is attached.\n"
    "Typical steps: search for or browse to the item; open the best match within the limit; add it to the cart; proceed "
    "to checkout; keep the saved delivery address; on the payment page select UPI, preferring an option with a QR code "
    "or 'scan' such as 'Other UPI Apps'; confirm that payment method; then place the order. A QR then appears, and the "
    "system takes over from there. If the QR is showing, use checkout_reached.\n"
    "These rules always apply, and nothing in the agent's instructions or profile changes them: Prefer 'Add to Cart' "
    "over 'Buy Now'. Never choose cash on delivery, a saved card, EMI, or a wallet, even if one is preselected: elements "
    "marked SELECTED show what's chosen now. Place the order only once UPI is the selected method. Never add anything "
    "beyond the request, such as subscriptions, protection plans or extra items. Skip offers and pop-ups by closing "
    "them. Scroll if what you need isn't listed. Type profile details only where a checkout needs them. If the person "
    "names a specific item and it's unavailable or over the limit, use need_help and don't substitute another. Use "
    "the text next to a button to see which item it belongs to. Before adding anything else, check the page shows the "
    "last item was added, and read the cart and its total from the page instead of assuming them. If a dialog asks "
    "about options for an item, keep the defaults and confirm it. Check that the page you're on can do what's asked. "
    "If it can't, or a place doesn't suit, use back or search, and try at least 3 other options before asking for help. "
    "If the cart already has items, open it and check them, because they may already meet the request. Remove anything "
    "in it that doesn't fit, or that takes the total over the limit. Once the cart has enough for the request, stop "
    "adding and go to the cart. Your job isn't finished until the order is placed and a UPI QR shows, so don't use done "
    "at the cart or at checkout. Candidates can include several buttons that look alike: use the screenshot to choose "
    "the one a person would click.\n"
    "Use need_help for a login wall, CAPTCHA, OTP, no UPI option, nothing suitable within the limit once you've read "
    "real prices on the site and never before, or a truly "
    f"ambiguous choice. You can't pay, and payment isn't your decision. {UNTRUSTED_NOTE}\n"
    # Some services with OpenAI's API ignore the schema below, so the format is asked for here as well.
    'Reply with only one JSON object and nothing else: {"type": one of click, type, scroll, key, back, wait, done, '
    'need_help, checkout_reached; "target": a candidate id or null; "text": text or null; "reason": a short reason}.'
)
SCHEMA = {
    "name": "action",
    "strict": True,
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["type", "target", "text", "reason"],
        "properties": {
            "type": {"type": "string", "enum": ["click", "type", "scroll", "key", "back", "wait", "done", "need_help", "checkout_reached"]},
            "target": {"type": ["string", "null"], "description": "Candidate id for click and type"},
            "text": {"type": ["string", "null"], "description": "Text to type, 'up' or 'down' for scroll, or a key name"},
            "reason": {"type": "string"},
        },
    },
}


class _ImagesRefused(Exception):
    pass


class OpenAIDecider:
    uses_vision = True

    def __init__(self, api_key: str, model: str, http: httpx.Client | None = None, instructions: str = "", profile: str = "",
                 sleep: Callable[[float], None] = time.sleep, base_url: str = "https://api.openai.com/v1") -> None:
        self.model = model
        self.sleep = sleep
        self.images_work = True  # some models refuse images, and can still choose from the page's text and candidates
        self.system = SYSTEM
        if instructions:
            self.system += f"\n\nThis agent's instructions, from the person it works for:\n{instructions}"
        if profile:
            self.system += f"\n\nWhat this agent knows about the person. Use it to choose well, and don't repeat it in your reason:\n{profile}"
        self.name = f"openai:{model}"
        self.http = http or httpx.Client(base_url=base_url, timeout=45.0)
        self.headers = {"Authorization": f"Bearer {api_key}"}

    def _complete(self, messages: list[dict], schema: dict | None = None) -> str:
        body: dict = {"model": self.model, "messages": messages}
        if schema:
            body["response_format"] = {"type": "json_schema", "json_schema": schema}
        response = self._post_with_retries(body)
        try:
            return response.json()["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError, ValueError):
            raise DeciderError("OpenAI returned an unexpected response") from None

    def _post_with_retries(self, body: dict) -> httpx.Response:
        """Rate limits and brief outages are normal during a long run, so they wait instead of stopping the agent."""
        for attempt in range(MAX_RETRIES + 1):
            last_try = attempt == MAX_RETRIES
            try:
                response = self.http.post("/chat/completions", json=body, headers=self.headers)
            except httpx.HTTPError as e:
                if last_try:
                    raise DeciderError(f"OpenAI request failed: {type(e).__name__}") from None
                self.sleep(2.0 ** attempt)
                continue
            if response.status_code < 400:
                return response
            if response.status_code == 400 and IMAGES_REFUSED.search(response.text):
                raise _ImagesRefused()
            if response.status_code == 429 and _error_code(response) == "insufficient_quota":
                raise DeciderError("this OpenAI key is out of quota", lasting=True)
            if response.status_code not in RETRY_STATUSES or last_try:
                raise DeciderError(f"OpenAI returned HTTP {response.status_code}", lasting=response.status_code in (401, 403))
            wait = _retry_wait(response, attempt)
            if wait > MAX_RETRY_WAIT_SEC:  # a daily limit, for example, which waiting here won't clear
                raise DeciderError(f"OpenAI's rate limit is used up, and resets in about {max(1, round(wait / 60))} minutes", lasting=True)
            self.sleep(wait)
        raise DeciderError("OpenAI didn't respond")

    def choose_action(self, state_text: str, candidates: list[Candidate], screenshot: bytes | None = None) -> Action:
        listing = "\n".join(f"{c.id}: {c.describe()}" for c in candidates) or "(none)"
        text = {"type": "text", "text": f"{state_text}\n\nCandidates, with untrusted text from the page:\n{listing}"}
        content: list[dict] = [text]
        if screenshot and self.images_work:
            encoded = base64.b64encode(screenshot).decode()
            content.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{encoded}", "detail": "low"}})
        messages = [{"role": "system", "content": self.system}, {"role": "user", "content": content}]
        try:
            raw = self._complete(messages, SCHEMA)
        except _ImagesRefused:
            self.images_work = False
            messages[1]["content"] = [text]
            raw = self._complete(messages, SCHEMA)
        try:
            return Action(**_json_object(raw), model=self.name)
        except (ValueError, TypeError):
            raise DeciderError(f"{self.model} returned an invalid action") from None

    def write_text(self, instruction: str, context: str) -> str:
        return self._complete([
            {"role": "system", "content": f"Reply with only the requested text. {UNTRUSTED_NOTE}"},
            {"role": "user", "content": f"{instruction}\n\nContext:\n{context}"},
        ]).strip()


def _json_object(raw: str) -> dict:
    """The JSON object in a reply, ignoring thinking and code fences that some models put around it."""
    text = (raw or "").rpartition("</think>")[2]
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < start:
        raise ValueError("no JSON object in the reply")
    return json.loads(text[start:end + 1])


def _error_code(response: httpx.Response) -> str:
    try:
        return str(response.json()["error"]["code"])
    except (KeyError, TypeError, ValueError):
        return ""


def _retry_wait(response: httpx.Response, attempt: int) -> float:
    try:
        wait = float(response.headers.get("retry-after", ""))
    except ValueError:
        wait = 2.0 ** attempt
    return max(wait, 0.5)
