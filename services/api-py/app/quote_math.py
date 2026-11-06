"""Quote maths. Integers only, so the backend rounds exactly like the amount the user signs.

  offramp: rateE6 = floor(bid_e6 * (10000 - spreadBps) / 10000)
           amount = ceil(inrPaise * 10^dec * 10^4 / rateE6)     (round UP: never under-collect)
  onramp:  rateE6 = floor(ask_e6 * (10000 + spreadBps) / 10000)
           amount = floor(inrPaise * 10^dec * 10^4 / rateE6)    (round DOWN: never over-pay the pool)

rateE6 is INR per token * 10^6; inrPaise is INR * 100; so inrPaise * 10^4 = INR * 10^6.
"""

from __future__ import annotations

import re

_PRICE_RE = re.compile(r"^(\d+)(?:\.(\d+))?$")
_INR_RE = re.compile(r"^(\d+)(?:\.(\d{1,2}))?$")


def price_to_e6(price: str | float | int) -> int:
    """'100.14' -> 100140000. Digits past the 6th decimal are truncated."""
    m = _PRICE_RE.match(str(price).strip())
    if not m:
        raise ValueError(f"bad price: {price!r}")
    frac = (m.group(2) or "")[:6].ljust(6, "0")
    return int(m.group(1)) * 1_000_000 + int(frac)


def offramp_rate_e6(bid_e6: int, spread_bps: int) -> int:
    return bid_e6 * (10_000 - spread_bps) // 10_000


def onramp_rate_e6(ask_e6: int, spread_bps: int) -> int:
    return ask_e6 * (10_000 + spread_bps) // 10_000


def _scaled(inr_paise: int, decimals: int) -> int:
    return inr_paise * 10**decimals * 10_000


def offramp_amount(inr_paise: int, decimals: int, rate_e6: int) -> int:
    if rate_e6 <= 0:
        raise ValueError("rate must be positive")
    return -(-_scaled(inr_paise, decimals) // rate_e6)  # ceil


def onramp_amount(inr_paise: int, decimals: int, rate_e6: int) -> int:
    if rate_e6 <= 0:
        raise ValueError("rate must be positive")
    return _scaled(inr_paise, decimals) // rate_e6  # floor


def rupees_to_paise(rupees: str | int | float) -> int:
    """'10' / '10.5' / '10.50' -> paise. More than 2 decimals is rejected."""
    s = str(rupees).strip()
    m = _INR_RE.match(s)
    if not m:
        raise ValueError(f"bad INR amount: {rupees!r} (use up to 2 decimals)")
    return int(m.group(1)) * 100 + int((m.group(2) or "").ljust(2, "0"))


def format_units(value: int, decimals: int) -> str:
    s = str(value).rjust(decimals + 1, "0")
    return f"{s[:-decimals]}.{s[-decimals:]}" if decimals else s


def format_paise(paise: int) -> str:
    return f"{paise // 100}.{paise % 100:02d}"


def format_rate_e6(rate_e6: int) -> str:
    return format_units(rate_e6, 6)
