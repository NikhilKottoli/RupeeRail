"""Tells a hosted agent server how each payment ended, because it can't read this machine's ledger.

It's a separate process that only reads the ledger, so a slow or failing server can't affect a payment. It sends the
offramp id and the status, and never the payee, amount or UTR. The server may hear a result twice, which does no harm.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
import urllib.request
from pathlib import Path
from typing import Callable

from agent import check_api_url
from payer import log

FINISHED = ("paid", "failed", "unknown", "dry_run")
RECENT_SEC = 24 * 60 * 60  # an agent waits minutes for a result, so older payments have nobody waiting
POLL_SEC = 3.0
TIMEOUT_SEC = 10.0


def post_json(url: str, token: str, body: dict) -> None:
    request = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                     headers={"content-type": "application/json", "authorization": f"Bearer {token}"})
    with urllib.request.urlopen(request, timeout=TIMEOUT_SEC):
        pass


class Reporter:
    def __init__(self, db_path: str, url: str, token: str, post: Callable[[str, str, dict], None] = post_json,
                 clock: Callable[[], float] = time.time, sleep: Callable[[float], None] = time.sleep) -> None:
        self.db_path, self.url, self.token, self.post, self.clock, self.sleep = Path(db_path), url, token, post, clock, sleep
        self.sent: dict[str, str] = {}

    def finished(self) -> list[tuple[str, str]]:
        if not self.db_path.exists():
            return []
        marks = ",".join("?" * len(FINISHED))
        db = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True, timeout=2)
        try:
            return db.execute(f"SELECT key, status FROM payments WHERE updated > ? AND status IN ({marks}) ORDER BY updated",
                              (self.clock() - RECENT_SEC, *FINISHED)).fetchall()
        finally:
            db.close()

    def once(self) -> int:
        """Returns how many it sent. A failure stops this pass, and the rest go on the next one."""
        count = 0
        for key, status in self.finished():
            if self.sent.get(key) == status:  # an unclear payment can later be settled, so a new status goes too
                continue
            self.post(self.url, self.token, {"key": key, "status": status})
            self.sent[key] = status
            count += 1
        return count

    def run(self) -> None:
        log(f"reporting payment results to {self.url}")
        while True:
            try:
                self.once()
            except Exception as e:  # the token is never in this, and the server may just be restarting
                log(f"couldn't report a result, and will retry: {type(e).__name__}")
            self.sleep(POLL_SEC)


def from_env(db_path: str) -> Reporter:
    url, token = os.environ.get("AGENT_RESULTS_URL"), os.environ.get("AGENT_RESULTS_TOKEN")
    if not url or not token:
        raise ValueError("set AGENT_RESULTS_URL and AGENT_RESULTS_TOKEN (see .env.example)")
    return Reporter(db_path, check_api_url(url), token)
