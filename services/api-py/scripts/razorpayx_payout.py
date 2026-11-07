#!/usr/bin/env python3
"""Prod payout rail (APP_ENV=prod, PAYOUT_RAIL=razorpayx): RazorpayX Payouts.

The contract, the same one dev_payout_stub.py and every PAYOUT_COMMAND follow:
  stdin   one JSON job: {idempotencyKey, offrampId, chain, upiId, payeeName, inrPaise, amountInr, lockTx}
  exit 0  only once the merchant is paid. RazorpayX payouts go queued -> processing -> processed,
          so wait for "processed" within PAYOUT_TIMEOUT_SEC; "accepted" is not "paid"
  stdout  a JSON line {"utr": "<bank UTR>"} so the onchain payoutRef is keccak256(utr)
  exit !0 not paid (rejected, reversed, cancelled, or still pending at the timeout)

To implement:
  1. POST /v1/payouts with mode "UPI", purpose "payout", amount = inrPaise, a vpa fund account for
     upiId, account_number = RAZORPAYX_ACCOUNT_NUMBER, and header
     X-Payout-Idempotency: idempotencyKey, so a retried job can't pay twice.
  2. Poll GET /v1/payouts/{id} until a final status, then print the UTR.
  3. Set `implemented=True` on the "razorpayx" rail in app/environment.py.
Keys come from the environment or the .env file (RAZORPAY_API_KEY, RAZORPAY_API_SECRET); never log them.
"""

import json
import sys


def main() -> int:
    job = json.load(sys.stdin)
    print(f"[razorpayx] not implemented: refusing payout {job.get('idempotencyKey')}; nobody was paid",
          file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
