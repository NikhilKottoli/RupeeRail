"""Finds the UPI payment on a checkout page. Written for any site, so nothing here is specific to one store."""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import unquote_plus

import cv2
import numpy as np

UPI_LINK_RE = re.compile(r"upi://pay\?[^\s\"'<>]+", re.IGNORECASE)
VPA_RE = re.compile(r"^[a-zA-Z0-9._-]{2,256}@[a-zA-Z0-9]{2,64}$")
AMOUNT_RE = re.compile(r"^(\d+)(?:\.(\d{1,2}))?$")
# A total on the page: a label such as "Amount to pay", then a rupee amount within a short distance.
AMOUNT = r"\b[^\d₹]{0,24}(?:₹|rs\.?|inr)\s*([\d,]+(?:\.\d{1,2})?)"
# The final amount's label beats a plain "Total", which on a real page may be for one line item.
FINAL_TOTAL_RE = re.compile(rf"(?:amount\s+to\s+pay|amount\s+payable|to\s+pay|grand\s+total|order\s+total|payment\s+total){AMOUNT}", re.IGNORECASE)
TOTAL_RE = re.compile(rf"total{AMOUNT}", re.IGNORECASE)


class QrError(Exception):
    """The page doesn't show one clear UPI payment."""


@dataclass(frozen=True)
class UpiPayment:
    qr_text: str
    upi_id: str
    name: str | None
    amount_paise: int | None


def to_paise(text: str) -> int | None:
    match = AMOUNT_RE.match(text.replace(",", "").strip())
    if not match:
        return None
    return int(match.group(1)) * 100 + int((match.group(2) or "").ljust(2, "0"))


def parse_upi(text: str) -> UpiPayment:
    """Reads a UPI link the way the backend's app/qr.py does: the first occurrence of a parameter wins."""
    raw = text.strip()
    match = re.match(r"^upi://pay\?(.*)$", raw, re.IGNORECASE | re.DOTALL)
    if not match:
        raise QrError("not a upi://pay QR")
    params: dict[str, str] = {}
    for part in match.group(1).split("&"):
        key, _, value = part.partition("=")
        if part and key.lower() not in params:
            params[key.lower()] = unquote_plus(value)
    upi_id = params.get("pa", "")
    if not VPA_RE.match(upi_id):
        raise QrError("the QR has no valid UPI ID")
    if params.get("cu", "INR").upper() != "INR":
        raise QrError("the QR is not for INR")
    amount = None
    if params.get("am"):
        amount = to_paise(params["am"])
        if amount is None:
            raise QrError("the QR has an invalid amount")
    return UpiPayment(raw, upi_id, params.get("pn") or None, amount)


def _decode_all(image: np.ndarray) -> set[str]:
    detector = cv2.QRCodeDetector()
    found: set[str] = set()
    try:
        ok, texts, _, _ = detector.detectAndDecodeMulti(image)
        if ok:
            found.update(t for t in texts if t)
    except cv2.error:
        pass
    if not found:
        try:
            text, _, _ = detector.detectAndDecode(image)
            if text:
                found.add(text)
        except cv2.error:
            pass
    return found


def decode_screenshot(image_bytes: bytes) -> set[str]:
    """Every QR text in a screenshot. Retries larger and in greyscale, because page screenshots vary a lot."""
    array = np.frombuffer(image_bytes, dtype=np.uint8)
    if array.size == 0:
        return set()
    image = cv2.imdecode(array, cv2.IMREAD_COLOR)
    if image is None:
        return set()
    grey = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    attempts = (
        image,
        grey,
        cv2.resize(grey, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC),
        cv2.threshold(grey, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)[1],
    )
    for attempt in attempts:
        found = _decode_all(attempt)
        if found:
            return found
    return set()


def find_payment(page_text: str, screenshot: bytes | None) -> UpiPayment:
    """Prefers an exact upi:// link in the page, then the QR in the screenshot. Refuses if they disagree."""
    links = {m.group(0) for m in UPI_LINK_RE.finditer(page_text or "")}
    codes = {t for t in decode_screenshot(screenshot) if t.lower().startswith("upi://")} if screenshot else set()
    candidates = {parse_upi(text).upi_id.lower(): text for text in links | codes}
    if not candidates:
        raise QrError("no UPI QR found on the page")
    if len(candidates) > 1:
        raise QrError(f"the page shows more than one UPI payee: {sorted(candidates)}")
    return parse_upi(next(iter(candidates.values())))


def find_total(page_text: str) -> int | None:
    """The amount the page asks for, in paise. None if it shows no total, or conflicting ones."""
    for pattern in (FINAL_TOTAL_RE, TOTAL_RE):
        totals = {to_paise(m.group(1)) for m in pattern.finditer(page_text or "")} - {None}
        if totals:
            return totals.pop() if len(totals) == 1 else None
    return None
