"""The agent's state machine.

    preflight -> observe -> decide -> act -> observe ... -> pay -> await_result -> done

Each state is a method that returns the next state. Models only choose page actions. Code decides when the checkout
has been reached, and the payment path has its own checks, so no model can cause a payment.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import re
import time
from typing import Any, Awaitable, Callable

from .callbacks import build_payload, send_webhook
from .deciders.base import Decider, DeciderError
from .models import Action, Candidate, PageObservation
from .payment.flow import Observation, PaymentFlow
from .payment.offramp_client import OfframpClient
from .payment.qr import QrError, find_payment, find_total
from .payment.results import PayoutResults, outcome
from .payment.verify import origin_allowed
from .session import AgentSession

TERMINAL = {"done", "failed", "halted"}
ALLOWED_KEYS = {"Enter", "Tab", "Escape"}
MAYBE_PAYMENT = re.compile(r"upi|scan|qr\b|upi://", re.IGNORECASE)
MAX_TEXT = 200
REPEATS_BEFORE_SCREENSHOT = 1
REPEATS_BEFORE_HELP = 4
BLOCKED_COMMITS_BEFORE_HELP = 2
# These can spend through a saved card or cash on delivery, outside every limit here. A person checks first.
ORDER_COMMIT = re.compile(
    r"\b(place (your )?order|buy now|pay now|confirm (your )?(order|payment|purchase)|complete (your )?(order|purchase)|submit order|make payment|pay ₹)",
    re.IGNORECASE,
)
# Whether code can see that UPI is what would be charged. Anything else stops the click.
OTHER_METHOD = re.compile(r"cash|card|ending in|emi\b|wallet|balance|net ?banking|\bcod\b", re.IGNORECASE)
PAYING_LINE = re.compile(r"(?:paying (?:with|by|via|using)|pay(?:ment)? (?:method|with|by|via|using))[^\n]{0,60}", re.IGNORECASE)
UPI = re.compile(r"\bupi\b", re.IGNORECASE)
PAUSE_LIMIT_SEC = 300.0

Exchange = Callable[[dict], Awaitable[dict]]


class Machine:
    def __init__(self, session: AgentSession, exchange: Exchange, decider: Decider, client: OfframpClient,
                 make_flow: Callable[[AgentSession], PaymentFlow], results: PayoutResults, chain: str,
                 webhook_url: str = "", max_steps: int = 40, time_limit_sec: float = 300.0, result_wait_sec: float = 120.0,
                 poll_sec: float = 3.0, wait_sec: float = 1.5, clock: Callable[[], float] = time.monotonic) -> None:
        self.session, self.exchange, self.decider, self.client = session, exchange, decider, client
        self.make_flow, self.results, self.chain, self.webhook_url = make_flow, results, chain, webhook_url
        self.max_steps, self.time_limit_sec, self.result_wait_sec = max_steps, time_limit_sec, result_wait_sec
        self.poll_sec, self.wait_sec, self.clock = poll_sec, wait_sec, clock
        self.started = clock()
        self.steps = 0
        self.history: list[str] = []
        self.observation: PageObservation | None = None
        self.action: Action | None = None
        self.last_signature = ""
        self.repeats = 0
        self.claimed_checkout = 0
        self.decider_errors = 0
        self.flow: PaymentFlow | None = None
        self.payment: Any = None
        self.help_reason = ""
        self.approved_commits: set[str] = set()
        self.pending_commit: str | None = None
        self.blocked_commits = 0
        self.last_total: int | None = None

    async def run(self) -> dict:
        state = "preflight"
        while state not in TERMINAL:
            if self.session.stopped.is_set() and state not in ("pay", "await_result"):
                self._finish("STOPPED", "stopped by the user")
                return self.session.result
            self.session.state = state
            state = await getattr(self, f"_{state}")()
        return self.session.result

    def _finish(self, status: str, detail: str = "") -> str:
        self.session.result = {"status": status, "detail": detail}
        self.session.emit("result", detail or status, status=status)
        return {"STOPPED": "halted"}.get(status, "failed")

    # ---- states ---------------------------------------------------------------------------------

    async def _preflight(self) -> str:
        try:
            ok, detail = await asyncio.to_thread(self._wallet_can_cover)
        except Exception as e:
            return self._finish("FAILED", f"couldn't check the wallet: {type(e).__name__}")
        if not ok:
            return self._finish("REFUSED", detail)
        self.session.emit("preflight", detail)
        return "observe"

    def _wallet_can_cover(self) -> tuple[bool, str]:
        signer = self.make_flow(self.session).signer
        config = self.client.config()
        account = self.client.account(self.chain, signer.address())
        token = config["chains"][self.chain]["token"]
        rate = float(self.client.health()["fx"][f"{token['symbol']}INR"]["bid"])
        needed = int(self.session.max_paise / 100 / rate * 10 ** int(token.get("decimals", 6)))
        balance = int(account["balance"])
        if balance < needed:
            return False, f"wallet balance {balance} is below the {needed} needed for this session's limit"
        if signer.uses_allowance and int(account["allowance"]) < needed:
            return False, f"vault allowance {account['allowance']} is below the {needed} needed for this session's limit"
        return True, f"wallet can cover Rs {self.session.max_paise // 100}"

    async def _observe(self) -> str:
        if self.steps >= self.max_steps:
            return self._finish("FAILED", f"stopped after {self.max_steps} steps")
        if self.clock() - self.started > self.time_limit_sec:
            return self._finish("FAILED", "ran out of time")
        reply = await self.exchange({"type": "observe"})
        if reply.get("type") != "observation":
            return self._finish("FAILED", str(reply.get("error") or "the extension couldn't read the page"))
        self.observation = PageObservation.model_validate(reply)
        self.session.emit("observe", self.observation.url, screenshot=self.observation.screenshot_b64)
        self.last_total = find_total(self.observation.page_text) or self.last_total
        if not origin_allowed(self.observation.url, self.session.allowed_domains):
            return self._finish("REFUSED", "the tab left the allowed sites")
        return "pay" if await asyncio.to_thread(self._checkout_reached) else "decide"

    def _checkout_reached(self) -> bool:
        obs = self.observation
        if not obs or not obs.screenshot_b64 or not MAYBE_PAYMENT.search(obs.page_text):
            return False
        try:
            payment = find_payment(obs.page_text, base64.b64decode(obs.screenshot_b64))
        except QrError:
            return False
        return self.last_total is not None or payment.amount_paise is not None

    def _state_text(self) -> str:
        obs = self.observation
        history = "\n".join(self.history[-6:]) or "(none)"
        return (f"Goal: {self.session.goal}\nLimit: Rs {self.session.max_paise // 100}\nActions so far:\n{history}\n\n"
                f"Current page: {obs.title} ({obs.url})\nUntrusted page text:\n{obs.page_text[:3000]}")

    async def _decide(self) -> str:
        obs = self.observation
        signature = hashlib.sha256(f"{obs.url}|{obs.page_text}|{self.action and self.action.model_dump_json(exclude={'reason'})}".encode()).hexdigest()
        self.repeats = self.repeats + 1 if signature == self.last_signature else 0
        self.last_signature = signature
        if self.repeats >= REPEATS_BEFORE_HELP:
            self.help_reason = "the agent is stuck on this page"
            return "pause"
        wants_vision = getattr(self.decider, "uses_vision", False) or self.repeats >= REPEATS_BEFORE_SCREENSHOT or not obs.candidates
        screenshot = base64.b64decode(obs.screenshot_b64) if obs.screenshot_b64 and wants_vision else None
        try:
            action = await asyncio.to_thread(self.decider.choose_action, self._state_text(), obs.candidates, screenshot)
            if action.type == "type" and not action.text:
                action.text = await asyncio.to_thread(self.decider.write_text, "Write what to type next to reach the goal.", self._state_text())
        except DeciderError as e:
            self.decider_errors += 1
            self.session.emit("decide", str(e))
            if self.decider_errors >= 3:
                self.help_reason = "the model isn't responding"
                return "pause"
            await asyncio.sleep(self.wait_sec)
            return "observe"
        self.decider_errors = 0
        self.steps += 1
        self.action = action
        problem = self._invalid(action, obs.candidates)
        self.session.emit("decide", f"{action.type} {action.target or action.text or ''}: {action.reason}".strip(), model=action.model)
        if problem:
            self.history.append(f"rejected: {problem}")
            self.session.emit("decide", f"rejected the action: {problem}")
            return "observe"
        if action.type == "need_help":
            self.help_reason = action.reason or "the agent asked for help"
            return "pause"
        label = self._commit_label(action, obs.candidates)
        if label and label not in self.approved_commits and not self._upi_is_selected(obs):
            self.blocked_commits += 1
            if self.blocked_commits >= BLOCKED_COMMITS_BEFORE_HELP:
                self.pending_commit = label
                self.help_reason = f'it wants to click "{label}", but can\'t confirm UPI is selected. Check the payment method is UPI, not a saved card or cash on delivery, then continue'
                return "pause"
            self.history.append(f'blocked "{label}": it could charge something other than UPI. Select UPI first, or use Add to Cart')
            self.session.emit("decide", f'blocked "{label}" because UPI isn\'t confirmed as the payment method')
            return "observe"
        if action.type == "done":
            return self._finish("FAILED", "the agent stopped without reaching a payment")
        if action.type == "checkout_reached":  # only code can decide this; the QR may still be loading
            self.claimed_checkout += 1
            if self.claimed_checkout >= 4:
                self.help_reason = "the agent thinks this is the checkout, but no UPI QR and total could be read"
                return "pause"
            await asyncio.sleep(self.wait_sec)
            return "observe"
        return "act"

    @staticmethod
    def _upi_is_selected(obs: PageObservation) -> bool:
        """True only if code can see UPI chosen, and nothing else chosen or named as the payment method."""
        chosen = [f"{c.text} {c.ariaLabel}" for c in obs.candidates if c.checked and c.role in ("radio", "checkbox")]
        lines = PAYING_LINE.findall(obs.page_text)
        if any(OTHER_METHOD.search(text) for text in lines):
            return False
        if any(UPI.search(text) for text in lines):
            return not any(OTHER_METHOD.search(text) for text in chosen)
        payment_choices = [text for text in chosen if UPI.search(text) or OTHER_METHOD.search(text)]
        return bool(payment_choices) and all(UPI.search(t) and not OTHER_METHOD.search(t) for t in payment_choices)

    @staticmethod
    def _commit_label(action: Action, candidates: list[Candidate]) -> str | None:
        if action.type != "click":
            return None
        target = next((c for c in candidates if c.id == action.target), None)
        label = f"{target.text} {target.ariaLabel}".strip() if target else ""
        return label[:80] if ORDER_COMMIT.search(label) else None

    @staticmethod
    def _invalid(action: Action, candidates: list[Candidate]) -> str | None:
        ids = {c.id for c in candidates}
        if action.type in ("click", "type") and action.target not in ids:
            return f"{action.target} isn't on the page"
        if action.type == "type" and len(action.text or "") > MAX_TEXT:
            return "the text is too long"
        if action.type == "scroll" and (action.text or "down") not in ("up", "down"):
            return "scroll must be up or down"
        if action.type == "key" and action.text not in ALLOWED_KEYS:
            return "that key isn't allowed"
        return None

    async def _act(self) -> str:
        action = self.action
        self.history.append(f"{action.type} {action.target or ''} {action.text or ''}".strip())
        if action.type == "wait":
            await asyncio.sleep(self.wait_sec)
            return "observe"
        reply = await self.exchange({"type": "action", "action": action.model_dump(exclude={"model", "reason"})})
        if not reply.get("ok"):
            self.history.append(f"failed: {str(reply.get('error'))[:100]}")
            self.session.emit("act", f"action failed: {str(reply.get('error'))[:100]}")
        return "observe"

    async def _pause(self) -> str:
        self.session.emit("need_help", self.help_reason)
        self.session.state = "paused"
        resumed = await self.session.wait_for_resume(PAUSE_LIMIT_SEC)
        if not resumed:
            if self.session.stopped.is_set():
                return self._finish("STOPPED", "stopped by the user")
            return self._finish("FAILED", f"no help arrived: {self.help_reason}")
        if self.pending_commit:
            self.approved_commits.add(self.pending_commit)
            self.pending_commit = None
        self.blocked_commits = 0
        self.repeats = 0
        self.last_signature = ""
        self.claimed_checkout = 0
        self.decider_errors = 0
        return "observe"

    async def _pay(self) -> str:
        obs = self.observation
        self.flow = self.make_flow(self.session)
        shot = base64.b64decode(obs.screenshot_b64) if obs.screenshot_b64 else None
        try:
            self.payment = await asyncio.to_thread(self.flow.pay, Observation(obs.url, obs.page_text, shot, self.last_total))
        except Exception as e:
            if self.flow.submitted and self.flow.offramp_id:  # it may have gone through, so keep watching it
                self.session.emit("pay", f"lost contact while submitting: {type(e).__name__}")
                return "await_result"
            return self._finish("FAILED", _safe_error(e))
        if self.payment.status == "REFUSED":
            return self._finish("REFUSED", self.payment.detail)
        if self.payment.status == "STOPPED":
            return self._finish("STOPPED", self.payment.detail)
        return "await_result"

    async def _await_result(self) -> str:
        offramp_id = self.flow.offramp_id
        self.session.state = "await_result"
        self.session.emit("await_result", "waiting for the payer's result")
        deadline = self.clock() + self.result_wait_sec
        status = None
        while self.clock() < deadline:
            payer = await asyncio.to_thread(self.results.result, offramp_id)
            try:
                onchain = (await asyncio.to_thread(self.client.status, offramp_id))["status"]
            except Exception:
                onchain = ""
            status = outcome(onchain, payer)
            if status:
                break
            await asyncio.sleep(self.poll_sec)
        status = status or "NO_RESULT"
        payment = self.payment
        payload = build_payload(self.session.id, self.session.goal, status, offramp_id, payment and payment.tx_hash,
                                payment and payment.amount_paise, payment and payment.payee)
        await asyncio.to_thread(send_webhook, self.webhook_url, payload)
        self.session.result = payload
        self.session.emit("result", _RESULT_TEXT[status], status=status, offramp_id=offramp_id, tx_hash=payload["txHash"],
                          token_amount=payment.token_amount if payment else None)
        return "done"


_RESULT_TEXT = {
    "PAID": "The merchant was paid.",
    "NOT_PAID": "The merchant wasn't paid. If the funds weren't returned, dispute this payment.",
    "CHECK_THE_PHONE": "The payer couldn't confirm the result. Check the phone.",
    "DRY_RUN": "Dry run: the payer was in dry-run mode, so no rupees moved.",
    "NO_RESULT": "No result from the payer. It may be offline. If so, the merchant wasn't paid.",
}


def _safe_error(error: Exception) -> str:
    # These messages are written by this project and the backend. Others could contain a URL or a key.
    if type(error).__name__ in ("OfframpError", "CircleSignerError"):
        return str(error)[:300]
    return f"the payment failed: {type(error).__name__}"
