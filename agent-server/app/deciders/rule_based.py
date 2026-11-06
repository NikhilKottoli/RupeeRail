"""A decider with no AI: matches the goal against common shopping labels.

It lets the whole loop run without a model key, and works on simple stores such as apps/mock-store.
"""

from __future__ import annotations

import re

from ..models import Action, Candidate

STOP_WORDS = {"order", "buy", "get", "me", "a", "an", "the", "some", "please", "one", "on", "from", "under", "for", "of", "and", "to", "with"}
CHECKOUT = re.compile(r"proceed|checkout|check out|place order|continue to pay|go to pay|pay now", re.I)
CART = re.compile(r"\b(cart|basket|bag)\b", re.I)
SEARCH = re.compile(r"search", re.I)
ADD = re.compile(r"\badd\b", re.I)
CART_COUNT = re.compile(r"\b(?:cart|basket|bag)\s*\(?\s*(\d+)", re.I)


def goal_words(goal: str) -> list[str]:
    cleaned = re.sub(r"(?:₹|rs\.?|inr)\s*\d[\d,]*", " ", goal, flags=re.I)
    return [w for w in re.findall(r"[a-z0-9]+", cleaned.lower()) if w not in STOP_WORDS and not w.isdigit()]


def _label(c: Candidate) -> str:
    return " ".join((c.text, c.ariaLabel, c.placeholder))


class RuleDecider:
    name = "rule-based"

    def __init__(self, goal: str) -> None:
        self.words = goal_words(goal)

    def choose_action(self, state_text: str, candidates: list[Candidate], screenshot: bytes | None = None) -> Action:
        def act(kind: str, target: Candidate | None = None, text: str | None = None, reason: str = "") -> Action:
            return Action(type=kind, target=target.id if target else None, text=text, reason=reason, model=self.name)

        for c in candidates:
            if c.role in ("button", "link") and CHECKOUT.search(_label(c)):
                return act("click", c, reason="go to checkout")

        cart_count = next((int(m.group(1)) for c in candidates if (m := CART_COUNT.search(_label(c)))), 0)
        if cart_count == 0:
            matches = [c for c in candidates if c.role == "button" and ADD.search(_label(c))]
            wanted = [c for c in matches if all(w in _label(c).lower() for w in self.words)]
            if len(wanted) == 1:
                return act("click", wanted[0], reason="add the item from the goal")
            if len(wanted) > 1:
                return act("need_help", reason="more than one item matches the goal")
            search = next((c for c in candidates if c.role in ("input", "textbox", "searchbox") and SEARCH.search(_label(c))), None)
            query = " ".join(self.words)
            if search and query and search.value.lower() != query:
                return act("type", search, query, reason="search for the item")
            return act("need_help", reason="couldn't find the item from the goal")

        for c in candidates:
            if c.role in ("button", "link") and CART.search(_label(c)):
                return act("click", c, reason="open the cart")
        return act("wait", reason="waiting for the page")

    def write_text(self, instruction: str, context: str) -> str:
        return " ".join(self.words)
