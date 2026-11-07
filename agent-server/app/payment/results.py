"""The payer's own record of whether the merchant was paid. On-chain COMPLETED doesn't prove that."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Callable, Protocol

KNOWN = {"paid", "failed", "unknown", "dry_run"}


class PayoutResults(Protocol):
    def result(self, offramp_id: str) -> str | None:
        """paid, failed, unknown or dry_run. None until the payer has finished."""
        ...


class LedgerResults:
    """Reads apps/upi-payer's ledger, read-only. Both run on the same machine."""

    def __init__(self, path: str) -> None:
        self.path = Path(path)

    def result(self, offramp_id: str) -> str | None:
        if not self.path.exists():
            return None
        try:
            db = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True, timeout=2)
            try:
                row = db.execute("SELECT status FROM payments WHERE key = ?", (offramp_id.lower(),)).fetchone()
            finally:
                db.close()
        except sqlite3.Error:
            return None
        return row[0] if row and row[0] in KNOWN else None


class ReportedResults:
    """What a payer on another machine has reported, for a server that can't read its ledger."""

    def __init__(self, lookup: Callable[[str], str | None]) -> None:
        self.lookup = lookup

    def result(self, offramp_id: str) -> str | None:
        status = self.lookup(offramp_id.lower())
        return status if status in KNOWN else None


def outcome(onchain: str, payer: str | None) -> str | None:
    """What to report, or None to keep waiting."""
    if onchain in ("REFUNDED", "REVERSED"):
        return "NOT_PAID"
    return {"paid": "PAID", "failed": "NOT_PAID", "unknown": "CHECK_THE_PHONE", "dry_run": "DRY_RUN"}.get(payer or "")
