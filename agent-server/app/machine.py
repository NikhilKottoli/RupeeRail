"""The agent's state machine.

    preflight -> observe -> decide -> act -> observe ... -> pay -> await_result -> done

Each state is a method that returns the next state. Models only choose page actions. Code decides when the checkout
has been reached, and the payment path has its own checks, so no model can cause a payment. It's given a signer
through the payment flow, and never anything that can change an agent or its budget.

A rehearsal browses the same way, but stops at the first order-placing click or QR, and never reaches the payment path.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import re
import time
from pathlib import Path
from typing import Any, Awaitable, Callable
from urllib.parse import urlparse

from .callbacks import build_payload, send_webhook
from .deciders.base import Decider, DeciderError
from .models import Action, Candidate, PageObservation
from .payment.flow import Observation, PaymentFlow
from .payment.offramp_client import OfframpClient
from .payment.qr import QrError, find_payment, find_total
from .payment.results import PayoutResults, outcome
from .payment.verify import origin_allowed
from .run import AgentRun

TERMINAL = {"done", "failed", "halted"}
ALLOWED_KEYS = {"Enter", "Tab", "Escape"}
MAYBE_PAYMENT = re.compile(r"upi|scan|qr\b|upi://", re.IGNORECASE)
MAX_TEXT = 200
REPEATS_BEFORE_SCREENSHOT = 1
REPEATS_BEFORE_HELP = 4
BLOCKED_COMMITS_BEFORE_HELP = 2
# These can spend through a saved card or cash on delivery, outside every limit here. A person checks first.
ORDER_COMMIT = re.compile(
    r"\b(place (your )?order|buy now|pay now|confirm (your )?(order|payment|purchase)|complete (your )?(order|purchase)|submit order|make payment|pay (?:₹|rs\b|inr\b|\d))",
    re.IGNORECASE,
)
# A rehearsal must never pay, on a site whose wording isn't known yet, so it also stops at anything like these. Reaching
# the payment page is fine.
REHEARSAL_STOP = re.compile(r"\bpay\b|\border\b|\bconfirm\b|\bpurchase\b|\bbuy\b", re.IGNORECASE)
REHEARSAL_SAFE = re.compile(r"proceed to (?:pay|buy|checkout)|continue to pay|order (history|details|summary)|my orders?|reorder|item total", re.IGNORECASE)
# Also a marketing link on a home page, so it only counts once the page shows a total.
ORDER_NOW = re.compile(r"\border now\b", re.IGNORECASE)
# Whether code can see that UPI is what would be charged. Anything else stops the click.
OTHER_METHOD = re.compile(r"cash|card|ending in|emi\b|wallet|balance|net ?banking|\bcod\b", re.IGNORECASE)
PAYING_LINE = re.compile(r"(?:paying (?:with|by|via|using)|pay(?:ment)? (?:method|with|by|via|using))[^\n]{0,60}", re.IGNORECASE)
UPI = re.compile(r"\bupi\b", re.IGNORECASE)
PAUSE_LIMIT_SEC = 300.0
# Each add can change the page, so repeat detection can't see a model that keeps adding the same item.
ADD_TO_CART = re.compile(r"\badd\b", re.IGNORECASE)
NOT_AN_ITEM = re.compile(r"\badd\b(?: an?| new| your| delivery| another)*\s*(?:address|tip|card|upi|money|note|instructions?|coupon|payment|phone|number)", re.IGNORECASE)
BLOCKED_ADDS_BEFORE_HELP = 3
DIALOG_EXTRA_BLOCKS = 3
# Models give up easily, so asking for help is sent back this many times unless it's something only a person can do.
GIVE_UPS_SENT_BACK = 2
DIALOG_GIVE_UPS = 2
REAL_BLOCKER = re.compile(r"log.?in|sign.?in|otp|captcha|one.time|password|verif|no upi|upi (isn't|is not|option)|isn't responding", re.IGNORECASE)
UNTOLD_ADDS = 3  # "Add" buttons with no text around them can't be told apart, so they share this allowance
DEAD_ADDS_BEFORE_HELP = 3
# A count of items on a cart button, such as "0 items in cart". Long pages can cut this out of the page text.
CART_COUNT = re.compile(r"\b\d+\s*(?:items?\b|cart\b)|\bcart\s*\(?\s*\d+", re.IGNORECASE)
# Different items, unless the prompt asks for more. A real model otherwise kept adding snacks and never opened the cart.
ITEMS_BEFORE_CART = 3
CART_LINE = re.compile(r"[^\n]*\b(?:cart|basket|bag|items?|added|quantity|qty)\b[^\n]*", re.IGNORECASE)
ADD_DIALOG = re.compile(r"add item|add to|continue|customi[sz]e|next step|choose", re.IGNORECASE)
MONEY = re.compile(r"(?:₹|rs\.?|inr)\s*\d[\d,.]*|\d[\d,.]*\s*(?:rs\b|rupees|inr\b)", re.IGNORECASE)
NUMBER_WORDS = {"two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10}
MAX_QUANTITY = 10
DIALOG_NOTE = ("A dialog is open over the page, and only its controls are listed. Click one of them now: choose an option, then "
               "click the button that confirms it, or click the one that closes the dialog. A dialog is never a reason to ask for help.\n\n")

Exchange = Callable[[dict], Awaitable[dict]]


def wanted_quantity(prompt: str) -> int:
    """How many of one item the person could mean, from the numbers in their prompt. One unless they say otherwise."""
    words = MONEY.sub(" ", prompt).lower()
    numbers = [int(n) for n in re.findall(r"\b\d{1,2}\b", words)] + [v for k, v in NUMBER_WORDS.items() if re.search(rf"\b{k}\b", words)]
    return max([1, *(n for n in numbers if n <= MAX_QUANTITY)])


class Machine:
    def __init__(self, agent_run: AgentRun, exchange: Exchange, decider: Decider, client: OfframpClient,
                 make_flow: Callable[[AgentRun], PaymentFlow], results: PayoutResults, chain: str,
                 webhook_url: str = "", max_steps: int = 40, time_limit_sec: float = 300.0, result_wait_sec: float = 120.0,
                 poll_sec: float = 3.0, wait_sec: float = 1.5, clock: Callable[[], float] = time.monotonic, debug_dir: str = "") -> None:
        self.agent_run, self.exchange, self.decider, self.client = agent_run, exchange, decider, client
        self.make_flow, self.results, self.chain, self.webhook_url = make_flow, results, chain, webhook_url
        self.max_steps, self.time_limit_sec, self.result_wait_sec = max_steps, time_limit_sec, result_wait_sec
        self.poll_sec, self.wait_sec, self.clock = poll_sec, wait_sec, clock
        self.debug_dir = Path(debug_dir) / agent_run.id if debug_dir else None
        self.debug_step = 0
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
        self.over_limit_commits = 0
        self.add_clicks: dict[str, int] = {}
        self.blocked_adds = 0
        self.give_ups = 0
        self.items_added: set[str] = set()
        self.pending_add: str | None = None  # an add that hasn't yet been seen to change the page
        self.page_before_add: PageObservation | None = None
        self.add_rechecked = False
        self.dead_adds = 0
        self.last_total: int | None = None

    async def run(self) -> dict:
        state = "preflight"
        while state not in TERMINAL:
            if self.agent_run.stopped.is_set() and state not in ("pay", "await_result"):
                self._finish("STOPPED", "stopped by the user")
                return self.agent_run.result
            self.agent_run.state = state
            state = await getattr(self, f"_{state}")()
        return self.agent_run.result

    def _finish(self, status: str, detail: str = "") -> str:
        detail = self.agent_run.redact(detail)
        self.agent_run.result = {"status": status, "detail": detail}
        self.agent_run.emit("result", detail or status, status=status)
        return {"STOPPED": "halted"}.get(status, "failed")

    # ---- states ---------------------------------------------------------------------------------

    def _rehearsal_report(self, stopped_at: str, qr_readable: bool, off_site_host: str | None = None) -> str:
        """What a real run would need on this site, with nothing placed or paid."""
        obs = self.observation
        hosts = sorted({*(obs.skipped_hosts if obs else ()), *([off_site_host] if off_site_host else ())})[:5]
        total = f"Rs {self.last_total / 100:g}" if self.last_total is not None else "not seen"
        upi = bool(obs) and self._upi_is_selected(obs)
        lines = [f"Stopped before paying, at {stopped_at}.", f"Site: {self.agent_run.site or 'none'}.", f"Total: {total}.",
                 f"UPI selected: {'yes' if upi else 'no'}.", f"UPI QR readable: {'yes' if qr_readable else 'not seen'}."]
        if hosts:
            lines.append(f"Off-site hosts, which a real run may need added to this agent's sites: {', '.join(hosts)}.")
        detail = self.agent_run.redact(" ".join(lines))
        self.agent_run.result = {"status": "REHEARSED", "detail": detail, "site": self.agent_run.site, "total_paise": self.last_total,
                                 "upi_selected": upi, "qr_readable": qr_readable, "off_site_hosts": hosts}
        self.agent_run.emit("result", detail, status="REHEARSED")
        return "done"

    async def _preflight(self) -> str:
        if self.agent_run.rehearse:  # it can't spend, so the wallet doesn't matter
            self.agent_run.emit("preflight", "rehearsal: nothing will be ordered or paid")
            return "observe"
        try:
            ok, detail = await asyncio.to_thread(self._wallet_can_cover)
        except Exception as e:
            return self._finish("FAILED", f"couldn't check the wallet: {type(e).__name__}")
        if not ok:
            return self._finish("REFUSED", detail)
        self.agent_run.emit("preflight", detail)
        return "observe"

    def _wallet_can_cover(self) -> tuple[bool, str]:
        signer = self.make_flow(self.agent_run).signer
        config = self.client.config()
        account = self.client.account(self.chain, signer.address())
        token = config["chains"][self.chain]["token"]
        rate = float(self.client.health()["fx"][f"{token['symbol']}INR"]["bid"])
        needed = int(self.agent_run.max_paise / 100 / rate * 10 ** int(token.get("decimals", 6)))
        balance = int(account["balance"])
        if balance < needed:
            return False, f"wallet balance {balance} is below the {needed} needed for this run's limit"
        if signer.uses_allowance and int(account["allowance"]) < needed:
            return False, f"vault allowance {account['allowance']} is below the {needed} needed for this run's limit"
        return True, f"wallet can cover Rs {self.agent_run.max_paise // 100}"

    async def _observe(self) -> str:
        if self.steps >= self.max_steps:
            return self._finish("FAILED", f"stopped after {self.max_steps} steps")
        if self.clock() - self.started > self.time_limit_sec:
            return self._finish("FAILED", "ran out of time")
        reply = await self.exchange({"type": "observe"})
        if reply.get("type") != "observation":
            if self.agent_run.rehearse and reply.get("host"):
                return self._rehearsal_report("a page off this agent's sites", False, str(reply["host"]))
            return self._finish("FAILED", str(reply.get("error") or "the extension couldn't read the page"))
        self.observation = PageObservation.model_validate(reply)
        self._save_debug()
        self.agent_run.emit("observe", self.observation.url, screenshot=self.observation.screenshot_b64)
        self.last_total = find_total(self.observation.page_text) or self.last_total
        if not origin_allowed(self.observation.url, self.agent_run.allowed_domains):
            if self.agent_run.rehearse:
                return self._rehearsal_report("a page off this agent's sites", False, urlparse(self.observation.url).hostname)
            return self._finish("REFUSED", "the tab left the allowed sites")
        self.agent_run.site = (urlparse(self.observation.url).hostname or "").lower()
        if self.pending_add:
            next_state = await self._check_add_worked()
            if next_state:
                return next_state
        if not await asyncio.to_thread(self._checkout_reached):
            return "decide"
        return self._rehearsal_report("the UPI QR", True) if self.agent_run.rehearse else "pay"

    @classmethod
    def _add_took_effect(cls, before: PageObservation, after: PageObservation, key: str) -> bool:
        """Clicking scrolls and many pages load more as they scroll, so a changed page proves nothing. These do: the
        button turning into a quantity control, the cart changing, a new page, or a dialog asking about options."""
        if before.url != after.url:
            return True
        click = lambda c: Action(type="click", target=c.id)
        if not any(cls._add_key(click(c), after.candidates) == key for c in after.candidates):
            return True
        if set(CART_LINE.findall(before.page_text)) != set(CART_LINE.findall(after.page_text)):
            return True
        counts = lambda obs: {f"{c.text} {c.ariaLabel}" for c in obs.candidates if CART_COUNT.search(f"{c.text} {c.ariaLabel}")}
        if counts(before) != counts(after):
            return True
        labels = lambda obs: {f"{c.text} {c.ariaLabel}".strip() for c in obs.candidates}
        return any(ADD_DIALOG.search(label) for label in labels(after) - labels(before))

    def _save_debug(self, action: Action | None = None) -> None:
        """Development only: keeps what the agent saw at each step, on this computer."""
        if not self.debug_dir:
            return
        self.debug_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        obs = self.observation
        if action:
            path = self.debug_dir / f"step-{self.debug_step:02d}.json"
            record = json.loads(path.read_text())
            path.write_text(json.dumps({**record, "action": action.model_dump()}, ensure_ascii=False, indent=1))
            return
        self.debug_step += 1
        record = {"url": obs.url, "title": obs.title, "page_text": obs.page_text, "skipped_hosts": obs.skipped_hosts,
                  "candidates": [f"{c.id}: {c.describe()}" for c in obs.candidates], "history": self.history[-10:]}
        (self.debug_dir / f"step-{self.debug_step:02d}.json").write_text(json.dumps(record, ensure_ascii=False, indent=1))
        if obs.screenshot_b64:
            (self.debug_dir / f"step-{self.debug_step:02d}.jpg").write_bytes(base64.b64decode(obs.screenshot_b64))

    async def _check_add_worked(self) -> str | None:
        """Tells the model what really happened, because a click can be ignored and a model may assume it worked."""
        unchanged = not self._add_took_effect(self.page_before_add, self.observation, self.pending_add)
        if unchanged and not self.add_rechecked:  # a slow site can still be updating, so look once more
            self.add_rechecked = True
            await asyncio.sleep(self.wait_sec * 2)
            return "observe"
        key, self.pending_add, self.add_rechecked = self.pending_add, None, False
        if not unchanged:
            self.dead_adds = 0
            if not self.observation.dialog_open:
                self.history.append(f'that add worked: "{key[:80]}" is in the cart now. Don\'t add it again')
            return None
        self.add_clicks[key] -= 1  # it wasn't added, so it doesn't count
        self.dead_adds += 1
        self.history.append(f'clicked "{key[:80]}", but the page didn\'t change, so it is NOT in the cart. Don\'t click that one again: try '
                            "another button that adds it, or look at the screenshot to find the visible one")
        self.agent_run.emit("act", "that click didn't change the page, so nothing was added")
        if self.dead_adds >= DEAD_ADDS_BEFORE_HELP:
            self.help_reason = "the site isn't responding to the agent's clicks on Add, so nothing is in the cart"
            return "pause"
        return None

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
        history = "\n".join(self.history[-10:]) or "(none)"
        past = "\n".join(self.agent_run.past_runs) or "(none)"
        return (f"The person's request: {self.agent_run.prompt}\nLimit: Rs {self.agent_run.max_paise // 100}\n"
                f"Past runs, recorded by this system:\n{past}\nActions so far:\n{history}\n\n"
                f"{DIALOG_NOTE if obs.dialog_open else ''}"
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
            self.agent_run.emit("decide", str(e))
            if e.lasting or self.decider_errors >= 3:
                self.help_reason = f"the model isn't responding: {e}"
                return "pause"
            await asyncio.sleep(self.wait_sec)
            return "observe"
        self.decider_errors = 0
        self.steps += 1
        self.action = action
        self._save_debug(action)
        problem = self._invalid(action, obs.candidates)
        self.agent_run.emit("decide", f"{action.type} {action.target or action.text or ''}: {action.reason}".strip(), model=action.model)
        if problem:
            self.history.append(f"rejected: {problem}")
            self.agent_run.emit("decide", f"rejected the action: {problem}")
            return "observe"
        if action.type == "need_help":
            allowed = GIVE_UPS_SENT_BACK + (DIALOG_GIVE_UPS if obs.dialog_open else 0)
            if getattr(self.decider, "uses_vision", False) and self.give_ups < allowed and not REAL_BLOCKER.search(action.reason):
                self.give_ups += 1
                self.history.append(f'asked for help ("{action.reason[:100]}"), but that isn\'t a reason to stop yet. Look at the page again: '
                                    "open the cart if it has items, use back or search, or try other options")
                self.agent_run.emit("decide", "sent back: it should try more before asking for help")
                return "observe"
            self.help_reason = action.reason or "the agent asked for help"
            return "pause"
        label = self._commit_label(action, obs.candidates, obs.page_text)
        if self.agent_run.rehearse:
            risky = self._rehearsal_stop_label(action, obs.candidates)
            if label or risky:
                return self._rehearsal_report(f'the "{label or risky}" button', False)
        if label and self.last_total is not None and self.last_total > self.agent_run.max_paise:
            self.over_limit_commits += 1
            amounts = f"the total of Rs {self.last_total / 100:g} is over this run's limit of Rs {self.agent_run.max_paise / 100:g}"
            if self.over_limit_commits >= BLOCKED_COMMITS_BEFORE_HELP:
                return self._finish("REFUSED", amounts)
            self.history.append(f'blocked "{label}": {amounts}. Bring the total under the limit, or use need_help')
            self.agent_run.emit("decide", f'blocked "{label}" because {amounts}')
            return "observe"
        if label and label not in self.approved_commits and not self._upi_is_selected(obs):
            self.blocked_commits += 1
            if self.blocked_commits >= BLOCKED_COMMITS_BEFORE_HELP:
                self.pending_commit = label
                self.help_reason = f'it wants to click "{label}", but can\'t confirm UPI is selected. Check the payment method is UPI, not a saved card or cash on delivery, then continue'
                return "pause"
            self.history.append(f'blocked "{label}": it could charge something other than UPI. Select UPI first, or use Add to Cart')
            self.agent_run.emit("decide", f'blocked "{label}" because UPI isn\'t confirmed as the payment method')
            return "observe"
        add = self._add_key(action, obs.candidates)
        new_item = add and not obs.dialog_open and add not in self.items_added  # a dialog's buttons are options for the same item
        if new_item and len(self.items_added) >= max(ITEMS_BEFORE_CART, wanted_quantity(self.agent_run.prompt)):
            self.blocked_adds += 1
            if self.blocked_adds >= BLOCKED_ADDS_BEFORE_HELP:
                self.help_reason = "it keeps trying to add more items instead of going to the cart"
                return "pause"
            self.history.append("blocked adding another item: the cart has enough for this request. Go to the cart now")
            self.agent_run.emit("decide", "blocked adding another item, because the cart has enough for this request")
            return "observe"
        if add:
            target = next(c for c in obs.candidates if c.id == action.target)
            allowed = wanted_quantity(self.agent_run.prompt)
            if self.add_clicks.get(add, 0) >= (allowed if target.context else max(allowed, UNTOLD_ADDS)):
                self.blocked_adds += 1
                if self.blocked_adds >= BLOCKED_ADDS_BEFORE_HELP + (DIALOG_EXTRA_BLOCKS if obs.dialog_open else 0):
                    self.help_reason = "it keeps trying to add more of the same item than was asked for"
                    return "pause"
                next_step = ("Now click the control that confirms or closes this dialog, not an add or + button" if obs.dialog_open else "Go to the cart")
                self.history.append(f'blocked "{add[:80]}": this item is already in the cart as many times as the person asked. {next_step}')
                self.agent_run.emit("decide", "blocked adding more of an item already in the cart")
                return "observe"
            self.add_clicks[add] = self.add_clicks.get(add, 0) + 1
            if new_item:
                self.items_added.add(add)
            self.pending_add, self.page_before_add, self.add_rechecked = add, obs, False
        if action.type == "done":
            if getattr(self.decider, "uses_vision", False):  # a model saying it's finished doesn't end a run that hasn't paid
                if self.give_ups < GIVE_UPS_SENT_BACK:
                    self.give_ups += 1
                    self.history.append('said it was done, but the job isn\'t done until the order is placed and the UPI QR shows. Carry on: open the cart, '
                                        'proceed to checkout, and select UPI. For something only a person can do, use need_help')
                    self.agent_run.emit("decide", "sent back: nothing has been ordered or paid yet")
                    return "observe"
                self.help_reason = action.reason or "the agent thinks it's finished, but nothing has been ordered or paid"
                return "pause"
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
    def _commit_label(action: Action, candidates: list[Candidate], page_text: str = "") -> str | None:
        if action.type != "click":
            return None
        target = next((c for c in candidates if c.id == action.target), None)
        label = f"{target.text} {target.ariaLabel}".strip() if target else ""
        if ORDER_COMMIT.search(label) or (ORDER_NOW.search(label) and find_total(page_text) is not None):
            return label[:80]
        return None

    @staticmethod
    def _rehearsal_stop_label(action: Action, candidates: list[Candidate]) -> str | None:
        if action.type != "click":
            return None
        target = next((c for c in candidates if c.id == action.target), None)
        label = f"{target.text} {target.ariaLabel}".strip() if target else ""
        return label[:80] if REHEARSAL_STOP.search(label) and not REHEARSAL_SAFE.search(label) else None

    @staticmethod
    def _add_key(action: Action, candidates: list[Candidate]) -> str | None:
        """Identifies the item an "Add" click is for, using the text around it because many buttons just say "Add"."""
        if action.type != "click":
            return None
        target = next((c for c in candidates if c.id == action.target), None)
        label = f"{target.text} {target.ariaLabel}".strip() if target else ""
        return f"{label} {target.context}".strip() if ADD_TO_CART.search(label) and not NOT_AN_ITEM.search(label) else None

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
        target = next((c for c in self.observation.candidates if c.id == action.target), None)
        named = f'"{(target.text or target.ariaLabel or target.placeholder)[:60]}"' if target else ""
        self.history.append(" ".join(part for part in (action.type, action.target, named, action.text) if part))
        if target and action.type == "click":
            # So a log can show whether a wrong turn was the model's choice or a click landing somewhere else.
            detail = target.describe()
            self.agent_run.emit("act", f"clicking {action.target}: {detail}")
        if action.type == "wait":
            await asyncio.sleep(self.wait_sec)
            return "observe"
        reply = await self.exchange({"type": "action", "action": action.model_dump(exclude={"model", "reason"})})
        if not reply.get("ok"):
            if self.pending_add:  # the click never happened, so it isn't an add, or one the site ignored
                self.add_clicks[self.pending_add] -= 1
                self.pending_add = None
            self.history.append(f"failed: {str(reply.get('error'))[:100]}")
            self.agent_run.emit("act", f"action failed: {str(reply.get('error'))[:100]}")
        elif reply.get("landed_on"):
            self.agent_run.emit("act", f"click landed on: {str(reply['landed_on'])[:130]}")
        return "observe"

    async def _pause(self) -> str:
        self.agent_run.emit("need_help", self.help_reason)
        self.agent_run.state = "paused"
        resumed = await self.agent_run.wait_for_resume(PAUSE_LIMIT_SEC)
        if not resumed:
            if self.agent_run.stopped.is_set():
                return self._finish("STOPPED", "stopped by the user")
            return self._finish("FAILED", f"no help arrived: {self.help_reason}")
        if self.pending_commit:
            self.approved_commits.add(self.pending_commit)
            self.pending_commit = None
        self.blocked_commits = 0
        self.blocked_adds = 0
        self.dead_adds = 0
        self.repeats = 0
        self.last_signature = ""
        self.claimed_checkout = 0
        self.decider_errors = 0
        return "observe"

    async def _pay(self) -> str:
        obs = self.observation
        self.flow = self.make_flow(self.agent_run)
        shot = base64.b64decode(obs.screenshot_b64) if obs.screenshot_b64 else None
        try:
            self.payment = await asyncio.to_thread(self.flow.pay, Observation(obs.url, obs.page_text, shot, self.last_total))
        except Exception as e:
            if self.flow.submitted and self.flow.offramp_id:  # it may have gone through, so keep watching it
                self.agent_run.emit("pay", f"lost contact while submitting: {type(e).__name__}")
                return "await_result"
            return self._finish("FAILED", _safe_error(e))
        if self.payment.status == "REFUSED":
            return self._finish("REFUSED", self.payment.detail)
        if self.payment.status == "STOPPED":
            return self._finish("STOPPED", self.payment.detail)
        return "await_result"

    async def _await_result(self) -> str:
        offramp_id = self.flow.offramp_id
        self.agent_run.state = "await_result"
        self.agent_run.emit("await_result", "waiting for the payer's result")
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
        run = self.agent_run
        payload = build_payload(run.id, run.agent_id, run.agent_name, run.wallet, run.redact(run.prompt), status, offramp_id,
                                payment and payment.tx_hash, payment and payment.amount_paise, payment and payment.payee)
        await asyncio.to_thread(send_webhook, self.webhook_url, payload)
        self.agent_run.result = payload
        self.agent_run.emit("result", _RESULT_TEXT[status], status=status, offramp_id=offramp_id, tx_hash=payload["txHash"],
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
