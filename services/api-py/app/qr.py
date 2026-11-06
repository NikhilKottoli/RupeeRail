"""UPI QR parsing. Phase 1 supports `upi://pay?...` only.

Refund reasons (same numbers as the vault): 1 INVALID_QR, 2 AMOUNT_MISMATCH, 3 DISPATCH_FAILED,
4 OPERATOR, 5 USER_RECLAIM. The quote service runs this BEFORE signing, so a bad QR is refused
up front; the listener runs it again before paying.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import unquote_plus

REASON_INVALID_QR = 1
REASON_AMOUNT_MISMATCH = 2
REASON_DISPATCH_FAILED = 3
REASON_OPERATOR = 4
REASON_USER_RECLAIM = 5

REFUND_REASONS = {
    1: "INVALID_QR",
    2: "AMOUNT_MISMATCH",
    3: "DISPATCH_FAILED",
    4: "OPERATOR",
    5: "USER_RECLAIM",
}

VPA_RE = re.compile(r"^[a-zA-Z0-9._-]{2,256}@[a-zA-Z0-9]{2,64}$")
MAX_QR_BYTES = 512
_UPI_RE = re.compile(r"^upi://pay\?(.*)$", re.IGNORECASE | re.DOTALL)
_AM_RE = re.compile(r"^(\d+)(?:\.(\d{1,2}))?$")


@dataclass
class ParsedQr:
    ok: bool
    reason: int | None = None
    detail: str = ""
    upi_id: str | None = None
    payee_name: str | None = None
    amount_paise: int | None = None
    params: dict[str, str] = field(default_factory=dict)


def _fail(reason: int, detail: str) -> ParsedQr:
    return ParsedQr(ok=False, reason=reason, detail=detail)


def _decode(v: str) -> str:
    try:
        return unquote_plus(v, errors="strict")
    except UnicodeDecodeError:
        return v


def parse_upi_qr(qr_text: str, signed_inr_paise: int | None = None) -> ParsedQr:
    """Parses a UPI QR. If `signed_inr_paise` is given, a present `am` must equal it."""
    n = len(qr_text.encode("utf-8"))
    if n == 0 or n > MAX_QR_BYTES:
        return _fail(REASON_INVALID_QR, f"QR length {n} bytes (allowed 1-{MAX_QR_BYTES})")
    text = qr_text.strip()
    if text.startswith("000201"):
        return _fail(REASON_INVALID_QR, "BharatQR/EMVCo QR is not supported in phase 1")
    m = _UPI_RE.match(text)
    if not m:
        return _fail(REASON_INVALID_QR, "not a upi://pay QR")

    params: dict[str, str] = {}
    for part in m.group(1).split("&"):
        if not part:
            continue
        k, _, v = part.partition("=")
        k = k.lower()
        if k not in params:  # first occurrence wins
            params[k] = _decode(v)

    pa = params.get("pa")
    if not pa or not VPA_RE.match(pa):
        return _fail(REASON_INVALID_QR, "invalid or missing payee VPA (pa)")
    cu = params.get("cu")
    if cu is not None and cu.upper() != "INR":
        return _fail(REASON_INVALID_QR, f"currency {cu!r} is not INR")

    amount_paise: int | None = None
    am = params.get("am")
    if am not in (None, ""):
        am_m = _AM_RE.match(am)
        if not am_m:
            return _fail(REASON_INVALID_QR, f"bad am {am!r}")
        amount_paise = int(am_m.group(1)) * 100 + int((am_m.group(2) or "").ljust(2, "0"))
        if signed_inr_paise is not None and amount_paise != signed_inr_paise:
            return _fail(
                REASON_AMOUNT_MISMATCH,
                f"QR asks for {amount_paise} paise but the signed amount is {signed_inr_paise} paise",
            )
    return ParsedQr(
        ok=True,
        upi_id=pa,
        payee_name=params.get("pn") or None,
        amount_paise=amount_paise,
        params=params,
    )
