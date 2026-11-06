#!/usr/bin/env python3
"""Buys from the mock store: a scripted browser reaches the payment page, then the real payment path takes over.

The browser steps stand in for the extension and the agent loop (milestones 2 and 3). The Observation is what the
extension will send. REAL TESTNET TRANSACTIONS, and a live payer will send real rupees to the QR's payee.

    uv run --group demo python -m app.demo_store --store http://127.0.0.1:5180 --item "Cutting chai" \
        --wallet 0x... --max-inr 1 --yes
"""

from __future__ import annotations

import argparse
import json
import sys

from playwright.sync_api import sync_playwright

from .payment.flow import Observation, PaymentFlow
from .payment.offramp_client import OfframpClient
from .payment.verify import Constraints
from .signers.circle_cli import CircleCliSigner

BACKEND = "https://rupee-rail.indiasouthcentral.cloudapp.azure.com"


def reach_payment_page(store: str, item: str) -> Observation:
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome")
        page = browser.new_page(viewport={"width": 1000, "height": 1100})
        page.goto(store)
        page.get_by_label("Search the menu").fill(item)
        page.get_by_role("button", name=f"Add {item}").click()
        page.get_by_role("button", name="Open cart").click()
        page.get_by_role("button", name="Proceed to payment").click()
        page.get_by_alt_text("UPI payment QR code").wait_for()
        page.wait_for_function("document.querySelector('.qr').complete && document.querySelector('.qr').naturalWidth > 0")
        observation = Observation(url=page.url, page_text=page.inner_text("body"), screenshot=page.screenshot(full_page=True))
        browser.close()
    return observation


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--store", default="http://127.0.0.1:5180")
    ap.add_argument("--item", default="Cutting chai")
    ap.add_argument("--wallet", required=True)
    ap.add_argument("--chain", default="arc")
    ap.add_argument("--max-inr", type=int, default=1)
    ap.add_argument("--backend", default=BACKEND)
    ap.add_argument("--allow-domain", action="append", default=["127.0.0.1"])
    ap.add_argument("--yes", action="store_true", help="really submit")
    args = ap.parse_args()

    if not args.yes:
        print("Refusing to run without --yes: this submits a payment.", file=sys.stderr)
        return 2
    observation = reach_payment_page(args.store, args.item)
    flow = PaymentFlow(
        OfframpClient(args.backend), CircleCliSigner(args.wallet), args.chain,
        Constraints(max_paise=args.max_inr * 100, allowed_domains=args.allow_domain), emit=lambda m: print(f"  {m}"),
    )
    result = flow.pay(observation)
    print(json.dumps({k: getattr(result, k) for k in ("status", "offramp_id", "tx_hash", "amount_paise", "payee", "token_amount", "detail")}, indent=2))
    return 0 if result.status in ("COMPLETED", "RELEASED") else 1


if __name__ == "__main__":
    sys.exit(main())
