"""Messages between the extension and the agent server, and the shape of an agent."""

from __future__ import annotations

import re
from decimal import Decimal
from typing import Literal
from urllib.parse import urlparse

from pydantic import BaseModel, Field, field_validator, model_validator

from .payment.verify import origin_allowed
from .templates import AVATARS, COLOURS

ActionType = Literal["click", "type", "scroll", "key", "back", "wait", "done", "need_help", "checkout_reached"]

DOMAIN_RE = re.compile(r"^(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+[a-z0-9]{2,}$")
IP_RE = re.compile(r"^\d{1,3}(?:\.\d{1,3}){3}$")
# A site is matched with its subdomains, so one of these would allow every site under it.
SHARED_SUFFIXES = {"co.in", "org.in", "net.in", "gov.in", "ac.in", "co.uk", "com.au"}


def clean_site(site: str) -> str:
    domain = site.strip().lower()
    if domain != "localhost" and not IP_RE.match(domain) and (not DOMAIN_RE.match(domain) or domain in SHARED_SUFFIXES):
        raise ValueError(f"{site!r} isn't a site like swiggy.com")
    return domain


class Candidate(BaseModel):
    id: str
    role: str = ""
    text: str = ""
    context: str = ""  # the text around it, such as the dish beside an "Add" button
    ariaLabel: str = ""
    placeholder: str = ""
    value: str = ""
    href: str = ""
    checked: bool = False
    bbox: list[float] = Field(default_factory=list)
    inViewport: bool = True

    def describe(self) -> str:
        parts = [self.role or "element"]
        if self.checked:
            parts.append("SELECTED")
        for label, value in (("text", self.text), ("label", self.ariaLabel), ("placeholder", self.placeholder), ("value", self.value)):
            if value:
                parts.append(f'{label} "{value[:80]}"')
        if self.context:
            parts.append(f'next to "{self.context[:160]}"')
        target = urlparse(self.href)
        if target.path not in ("", "/") or target.query:
            parts.append(f'goes to "{(target.path + ("?" + target.query if target.query else ""))[:60]}"')
        return ", ".join(parts)


class Action(BaseModel):
    type: ActionType
    target: str | None = None
    text: str | None = None
    reason: str = ""
    model: str = ""


class AgentFields(BaseModel):
    """What a person sets on an agent. Its wallet and budget are handled separately."""

    name: str = Field(min_length=1, max_length=40)
    description: str = Field(default="", max_length=120)
    avatar: str
    colour: str
    instructions: str = Field(min_length=1, max_length=4000)
    profile: str = Field(default="", max_length=4000)
    sites: list[str] = Field(min_length=1, max_length=10)
    start_url: str = Field(max_length=500)
    per_order_max_inr: int = Field(gt=0)

    @field_validator("avatar")
    @classmethod
    def known_avatar(cls, value: str) -> str:
        if value not in AVATARS:
            raise ValueError("unknown avatar")
        return value

    @field_validator("colour")
    @classmethod
    def known_colour(cls, value: str) -> str:
        if value not in COLOURS:
            raise ValueError("unknown colour")
        return value

    @field_validator("sites")
    @classmethod
    def clean_sites(cls, value: list[str]) -> list[str]:
        return list(dict.fromkeys(clean_site(site) for site in value))

    @model_validator(mode="after")
    def start_url_is_on_a_site(self) -> "AgentFields":
        if urlparse(self.start_url).scheme not in ("http", "https") or not origin_allowed(self.start_url, self.sites):
            raise ValueError("the start URL must be on one of the agent's sites")
        return self


class AgentCreate(AgentFields):
    budget_usdc: Decimal = Field(gt=0)


class AgentPatch(BaseModel):
    name: str | None = None
    description: str | None = None
    avatar: str | None = None
    colour: str | None = None
    instructions: str | None = None
    profile: str | None = None
    sites: list[str] | None = None
    start_url: str | None = None
    per_order_max_inr: int | None = None


class BudgetRequest(BaseModel):
    usdc: Decimal = Field(gt=0)


class RunRequest(BaseModel):
    prompt: str = Field(min_length=1, max_length=500)
    max_inr: int | None = Field(default=None, gt=0)  # can lower the agent's limit, never raise it
    rehearse: bool = False


class QueuedRehearsal(BaseModel):
    agent_id: str
    prompt: str = Field(min_length=1, max_length=500)
    max_inr: int | None = Field(default=None, gt=0)


class PageObservation(BaseModel):
    url: str
    title: str = ""
    page_text: str = ""
    candidates: list[Candidate] = Field(default_factory=list)
    screenshot_b64: str | None = None
    skipped_hosts: list[str] = Field(default_factory=list)  # frames the extension wouldn't read, being off the agent's sites
    dialog_open: bool = False  # a dialog covers the page, so only its controls are listed
