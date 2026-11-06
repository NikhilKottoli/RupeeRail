#!/usr/bin/env python3
"""Dev stand-in for the INR payout black box.

Reads one JSON job on stdin ({idempotencyKey, upiId, payeeName, inrPaise, amountInr, ...}),
prints "pay Rs X to upiId" and exits 0. STUB_FAIL=1 makes it exit 1 (payout failure).
A real provider would print a JSON line like {"utr": "..."} so the payoutRef is keccak256(utr).
"""

import json
import os
import sys


def main() -> int:
    job = json.load(sys.stdin)
    if os.environ.get("STUB_FAIL") == "1":
        print(f"[stub] FAILING payout for {job.get('idempotencyKey')}", file=sys.stderr)
        return 1
    name = f" ({job['payeeName']})" if job.get("payeeName") else ""
    print(f"[stub] pay ₹{job['amountInr']} to {job['upiId']}{name} key={job['idempotencyKey']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
