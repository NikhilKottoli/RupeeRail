"""Code-enforced checks between the quote and the signature. No model is involved, so none can talk its way past them.

Covers section 7 of README-agents.md. The backend's "section 5" rules aren't in this repo, so the signed-payload
checks below come from reading the contract and the backend, and need comparing with that document when it arrives.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import urlparse

from .qr import UpiPayment

DEADLINE_MARGIN_SEC = 20
# The quoted rate may sit below the market by the spread, but a wider gap means something is wrong.
MAX_RATE_GAP = 0.05


class VerificationError(Exception):
    def __init__(self, problems: list[str]) -> None:
        super().__init__("; ".join(problems))
        self.problems = problems


@dataclass
class Constraints:
    max_paise: int
    allowed_domains: list[str] = field(default_factory=list)


def origin_allowed(url: str, allowed_domains: list[str]) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return any(host == d.lower() or host.endswith("." + d.lower()) for d in allowed_domains)


def check_page(payment: UpiPayment, page_total: int | None, url: str, constraints: Constraints) -> list[str]:
    """What must hold before even asking for a quote."""
    problems = []
    if not origin_allowed(url, constraints.allowed_domains):
        problems.append(f"{urlparse(url).hostname} is not in the allowed domains")
    if page_total is None:
        problems.append("couldn't read one clear total from the page")
    else:
        if payment.amount_paise is not None and payment.amount_paise != page_total:
            problems.append(f"the QR asks for {payment.amount_paise} paise but the page says {page_total}")
        if page_total <= 0:
            problems.append("the page total isn't positive")
        if page_total > constraints.max_paise:
            problems.append(f"{page_total} paise is over the {constraints.max_paise} paise limit")
    return problems


def _same_address(a: object, b: object) -> bool:
    return str(a).lower() == str(b).lower()


def check_quote(quote: dict, *, payment: UpiPayment, page_total: int, wallet: str, chain_info: dict,
                constraints: Constraints, now: float) -> list[str]:
    """Compares the quote and the payloads about to be signed with what the page and /config say."""
    problems = []
    intent = quote["intent"]
    permit = quote["signing"]["permit"]
    typed_intent = quote["signing"]["intent"]

    if not _same_address(quote["payee"]["upiId"], payment.upi_id):
        problems.append(f"quote pays {quote['payee']['upiId']}, but the page's QR pays {payment.upi_id}")
    inr_paise = int(intent["inrPaise"])
    if inr_paise != page_total:
        problems.append(f"quote is for {inr_paise} paise, but the page says {page_total}")
    if payment.amount_paise is not None and inr_paise != payment.amount_paise:
        problems.append(f"quote is for {inr_paise} paise, but the QR says {payment.amount_paise}")
    if inr_paise > constraints.max_paise:
        problems.append(f"{inr_paise} paise is over the {constraints.max_paise} paise limit")
    if int(intent["amount"]) <= 0:
        problems.append("quote has no token amount")

    if not _same_address(intent["user"], wallet):
        problems.append("the intent isn't for this wallet")
    if not _same_address(permit["message"]["owner"], wallet):
        problems.append("the permit isn't for this wallet")
    if not _same_address(permit["message"]["spender"], chain_info["vault"]):
        problems.append("the permit's spender isn't the vault")
    if int(permit["message"]["value"]) != int(intent["amount"]):
        problems.append("the permit's value differs from the intent's amount")

    expected = chain_info["vaultDomain"]
    domain = typed_intent["domain"]
    if int(domain["chainId"]) != int(chain_info["chainId"]) or not _same_address(domain["verifyingContract"], expected["verifyingContract"]):
        problems.append("the intent's domain doesn't match this chain's vault")
    if domain.get("name") != expected.get("name") or domain.get("version") != expected.get("version"):
        problems.append("the intent's domain name or version is unexpected")
    if int(permit["domain"]["chainId"]) != int(chain_info["chainId"]) or not _same_address(permit["domain"]["verifyingContract"], chain_info["token"]["address"]):
        problems.append("the permit's domain doesn't match this chain's token")
    if typed_intent["message"] != intent:
        problems.append("the payload to sign differs from the intent being submitted")
    if int(intent["qrCommitment"], 16) == 0:
        problems.append("the intent has no QR commitment")

    deadline = int(intent["deadline"])
    if deadline < now + DEADLINE_MARGIN_SEC:
        problems.append("the quote is about to expire")
    if int(permit["message"]["deadline"]) < now + DEADLINE_MARGIN_SEC:
        problems.append("the permit is about to expire")

    rate, market = float(quote["display"]["rate"]), float(quote["display"]["marketRate"])
    if market <= 0 or abs(market - rate) / market > MAX_RATE_GAP:
        problems.append(f"quoted rate {rate} is too far from market {market}")
    return problems
