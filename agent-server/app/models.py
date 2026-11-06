"""Messages between the extension and the agent server. See section 6 of README-agents.md."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

ActionType = Literal["click", "type", "scroll", "key", "wait", "done", "need_help", "checkout_reached"]


class Candidate(BaseModel):
    id: str
    role: str = ""
    text: str = ""
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
        return ", ".join(parts)


class Action(BaseModel):
    type: ActionType
    target: str | None = None
    text: str | None = None
    reason: str = ""
    model: str = ""


class SessionRequest(BaseModel):
    goal: str = Field(min_length=1, max_length=500)
    max_inr: int = Field(gt=0)
    allowed_domains: list[str] | None = None


class PageObservation(BaseModel):
    url: str
    title: str = ""
    page_text: str = ""
    candidates: list[Candidate] = Field(default_factory=list)
    screenshot_b64: str | None = None
