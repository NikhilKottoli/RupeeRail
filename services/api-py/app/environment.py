"""Dev and prod. APP_ENV=dev (default) or prod picks how INR moves; nothing else differs.

  payout  (tokens -> INR to the merchant)  dev: dev_payout_stub.py, or the phone (apps/upi-payer)
                                           prod: razorpayx_payout.py (RazorpayX Payouts)
  collect (user's INR for an onramp)       dev: operator calls /admin/onramp/{id}/fiat-received
                                           prod: Razorpay Payment Link + webhook

Dev is exactly today's behaviour. Prod refuses to start until its Razorpay rails are implemented
and configured (they need a current account and a business PAN), so a dev stand-in never runs there.
"""

from __future__ import annotations

import os
import shlex
import sys
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
PLACEHOLDER_COLLECT_UPI_ID = "rupeerail@okaxis"  # config.py's default; not a real collection account


class AppEnv(str, Enum):
    DEV = "dev"
    PROD = "prod"


@dataclass(frozen=True)
class Rail:
    name: str
    env: AppEnv
    implemented: bool
    required_env: tuple[str, ...] = ()  # only checked for "is it set"; values are never read here
    script: str | None = None  # payout rails only


PAYOUT_RAILS = {
    "stub": Rail("stub", AppEnv.DEV, True, script="dev_payout_stub.py"),
    "phone": Rail("phone", AppEnv.DEV, True, script="dev_payout_stub.py"),  # the phone pays, not the backend
    "razorpayx": Rail("razorpayx", AppEnv.PROD, False, ("RAZORPAY_KEY_ID", "RAZORPAY_KEY_SECRET",
                                                         "RAZORPAYX_ACCOUNT_NUMBER"), "razorpayx_payout.py"),
}
COLLECT_RAILS = {
    "manual": Rail("manual", AppEnv.DEV, True),
    "razorpay": Rail("razorpay", AppEnv.PROD, False, ("RAZORPAY_KEY_ID", "RAZORPAY_KEY_SECRET",
                                                       "RAZORPAY_WEBHOOK_SECRET")),
}
DEFAULT_RAILS = {AppEnv.DEV: ("stub", "manual"), AppEnv.PROD: ("razorpayx", "razorpay")}


def _value(environ, var: str, default: str) -> str:
    """The variable, lowercased; unset, empty or blank means `default`."""
    return (environ.get(var) or "").strip().lower() or default


def _pick(table: dict[str, Rail], var: str, default: str, environ) -> Rail:
    name = _value(environ, var, default)
    if name not in table:
        raise ValueError(f"{var} must be one of {sorted(table)}, not {name!r}")
    return table[name]


def current_env(environ=None) -> AppEnv:
    """Unset, empty or blank APP_ENV is dev."""
    raw = _value(os.environ if environ is None else environ, "APP_ENV", AppEnv.DEV.value)
    try:
        return AppEnv(raw)
    except ValueError:
        raise ValueError(f"APP_ENV must be one of {[e.value for e in AppEnv]}, not {raw!r}") from None


def payout_rail(environ=None) -> Rail:
    environ = os.environ if environ is None else environ
    return _pick(PAYOUT_RAILS, "PAYOUT_RAIL", DEFAULT_RAILS[current_env(environ)][0], environ)


def collect_rail(environ=None) -> Rail:
    environ = os.environ if environ is None else environ
    return _pick(COLLECT_RAILS, "COLLECT_RAIL", DEFAULT_RAILS[current_env(environ)][1], environ)


def prod_payout_command(environ=None) -> str | None:
    """The prod payout rail's command, or None in dev (config.py then keeps its stub, unchanged)."""
    if current_env(environ) is not AppEnv.PROD:
        return None
    script = SCRIPTS_DIR / payout_rail(environ).script
    return f"{shlex.quote(sys.executable)} {shlex.quote(str(script))}"


def problems(settings, environ=None) -> list[str]:
    """Why prod can't start with these settings. Always empty in dev."""
    environ = os.environ if environ is None else environ
    if current_env(environ) is not AppEnv.PROD:
        return []
    out: list[str] = []
    for var, rail in (("PAYOUT_RAIL", payout_rail(environ)), ("COLLECT_RAIL", collect_rail(environ))):
        if rail.env is not AppEnv.PROD:
            out.append(f"{var}={rail.name} is a dev rail")
        elif not rail.implemented:
            out.append(f"{var}={rail.name} is declared but not implemented yet")
        out += [f"{name} is not set" for name in rail.required_env if not environ.get(name)]
    if PAYOUT_RAILS["stub"].script in settings.payout_command:
        out.append("PAYOUT_COMMAND runs the dev stub, which pays nobody")
    if settings.collect_upi_id == PLACEHOLDER_COLLECT_UPI_ID:
        out.append(f"COLLECT_UPI_ID is the placeholder {PLACEHOLDER_COLLECT_UPI_ID}")
    out += [f"{n} is not set" for n, v in (("ADMIN_TOKEN", settings.admin_token),
                                           ("VIEW_TOKEN_SECRET", settings.view_token_secret)) if not v]
    return sorted(set(out))


def assert_ready(settings, environ=None) -> None:
    found = problems(settings, environ)
    if found:
        raise RuntimeError("APP_ENV=prod is not ready: " + "; ".join(found))
