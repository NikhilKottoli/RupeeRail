"""SQLite store. The `quotes` table (qrText + salt) is the backend's private source of truth;
it is never onchain and must never be served to anyone but the quoting client.

All state transitions that guard money movement are compare-and-swap UPDATEs
(`... WHERE id = ? AND state = ?`), so two concurrent callers can never both win.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterable

STATUS_NAMES = ["NONE", "LOCKED", "COMPLETED", "REFUNDED", "REVERSED", "RELEASED", "DISPUTED"]
DISPATCH_STATES = ("NONE", "DISPATCHING", "DISPATCHED", "FAILED", "NEEDS_OPERATOR", "SKIPPED")

SCHEMA = """
CREATE TABLE IF NOT EXISTS quotes (
  id             TEXT PRIMARY KEY,          -- 0x bytes32, also the payout idempotency key
  chain          TEXT NOT NULL,
  user           TEXT NOT NULL,             -- checksum address
  amount         TEXT NOT NULL,             -- token base units (decimal string)
  inr_paise      INTEGER NOT NULL,
  rate_e6        TEXT NOT NULL,
  qr_text        TEXT NOT NULL,             -- PRIVATE: never onchain, never served by id
  salt           TEXT NOT NULL,             -- PRIVATE: 0x bytes32
  qr_commitment  TEXT NOT NULL,
  deadline       INTEGER NOT NULL,
  quote_sig      TEXT NOT NULL,
  upi_id         TEXT,
  payee_name     TEXT,
  market         TEXT,
  bid            TEXT,
  status         TEXT NOT NULL DEFAULT 'QUOTED',   -- QUOTED | SUBMITTED | USED
  view_token     TEXT,                      -- lets the quoting client read the payee back by id
  created_at     INTEGER NOT NULL,
  used_at        INTEGER,
  offramp_tx     TEXT,
  last_error     TEXT
);
CREATE INDEX IF NOT EXISTS quotes_user ON quotes(user, created_at);

CREATE TABLE IF NOT EXISTS payments (
  id                  TEXT PRIMARY KEY,
  chain               TEXT NOT NULL,
  user                TEXT,
  amount              TEXT,
  inr_paise           INTEGER,
  rate_e6             TEXT,
  qr_commitment       TEXT,
  status              TEXT NOT NULL,        -- onchain status mirror (STATUS_NAMES)
  refund_reason       INTEGER,
  dispatch_state      TEXT NOT NULL DEFAULT 'NONE',
  dispatch_detail     TEXT,
  dispatch_started_at INTEGER,
  dispatched_at       INTEGER,
  pending_refund      INTEGER,              -- reason the settler must still refund with, or NULL
  match_ok            INTEGER,              -- 1 matched a stored quote, 0 mismatch / unknown
  match_detail        TEXT,
  payout_ref          TEXT,
  lock_tx             TEXT,
  lock_block          INTEGER,
  locked_at           INTEGER,
  completed_at        INTEGER,
  completable_at      INTEGER,
  reclaimable_at      INTEGER,
  dispute_window      INTEGER,
  releasable_at       INTEGER,              -- end of the dispute window (never overwritten by a dispute)
  dispute_deadline    INTEGER,              -- arbiter resolve deadline (onchain: releasableAt while DISPUTED)
  txs                 TEXT NOT NULL DEFAULT '{}',
  alerted             INTEGER NOT NULL DEFAULT 0,
  created_at          INTEGER NOT NULL,
  updated_at          INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS payments_state ON payments(chain, status, dispatch_state);
CREATE INDEX IF NOT EXISTS payments_user ON payments(user);

CREATE TABLE IF NOT EXISTS onramp_orders (
  id          TEXT PRIMARY KEY,             -- 0x bytes32 orderId
  chain       TEXT NOT NULL,
  wallet      TEXT NOT NULL,
  inr_paise   INTEGER NOT NULL,
  amount      TEXT NOT NULL,
  rate_e6     TEXT NOT NULL,
  market      TEXT,
  ask         TEXT,
  status      TEXT NOT NULL,                -- AWAITING_FIAT | PAYING | PAID | FAILED
  bank_ref    TEXT,
  tx          TEXT,
  last_error  TEXT,
  created_at  INTEGER NOT NULL,
  updated_at  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS cursors (
  chain  TEXT PRIMARY KEY,
  block  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS disputes (
  id          TEXT PRIMARY KEY,             -- offramp id (one dispute per offramp)
  chain       TEXT NOT NULL,
  user        TEXT NOT NULL,
  deadline    INTEGER NOT NULL,
  user_sig    TEXT NOT NULL,
  status      TEXT NOT NULL,                -- RELAYING | RELAYED | FAILED
  tx          TEXT,
  error       TEXT,
  created_at  INTEGER NOT NULL,
  updated_at  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
  chain      TEXT NOT NULL,
  tx         TEXT NOT NULL,
  log_index  INTEGER NOT NULL,
  block      INTEGER NOT NULL,
  name       TEXT NOT NULL,
  ref_id     TEXT,
  args       TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  PRIMARY KEY (chain, tx, log_index)
);
CREATE INDEX IF NOT EXISTS events_ref ON events(ref_id);
"""


def now() -> int:
    return int(time.time())


class Store:
    def __init__(self, path: str | Path = ":memory:") -> None:
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA busy_timeout=5000")
            self._db.executescript(SCHEMA)

    # ---- helpers -------------------------------------------------------------------------------
    def _exec(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._db.execute(sql, tuple(params))

    def _one(self, sql: str, params: Iterable[Any] = ()) -> dict | None:
        row = self._exec(sql, params).fetchone()
        return dict(row) if row else None

    def _all(self, sql: str, params: Iterable[Any] = ()) -> list[dict]:
        return [dict(r) for r in self._exec(sql, params).fetchall()]

    def close(self) -> None:
        with self._lock:
            self._db.close()

    # ---- quotes --------------------------------------------------------------------------------
    def insert_quote(self, q: dict) -> None:
        q = {**q, "id": q["id"].lower(), "created_at": q.get("created_at", now())}
        cols = ",".join(q)
        self._exec(f"INSERT INTO quotes ({cols}) VALUES ({','.join('?' * len(q))})", q.values())

    def get_quote(self, id_: str) -> dict | None:
        return self._one("SELECT * FROM quotes WHERE id = ?", (id_.lower(),))

    def claim_quote(self, id_: str, from_status: str = "QUOTED", to_status: str = "SUBMITTED") -> bool:
        cur = self._exec(
            "UPDATE quotes SET status = ?, used_at = ? WHERE id = ? AND status = ?",
            (to_status, now(), id_.lower(), from_status),
        )
        return cur.rowcount == 1

    def set_quote_status(self, id_: str, status: str, **fields: Any) -> None:
        sets = ", ".join(["status = ?"] + [f"{k} = ?" for k in fields])
        self._exec(f"UPDATE quotes SET {sets} WHERE id = ?", (status, *fields.values(), id_.lower()))

    def user_quoted_paise(self, user: str, since: int, at: int) -> int:
        """Paise counted against the daily cap: used/submitted quotes since `since`, plus unexpired quoted ones."""
        row = self._one(
            "SELECT COALESCE(SUM(inr_paise), 0) AS s FROM quotes WHERE lower(user) = lower(?) AND created_at >= ? "
            "AND (status IN ('USED', 'SUBMITTED') OR (status = 'QUOTED' AND deadline >= ?))",
            (user, since, at),
        )
        return int(row["s"]) if row else 0

    # ---- payments ------------------------------------------------------------------------------
    def get_payment(self, id_: str) -> dict | None:
        return self._one("SELECT * FROM payments WHERE id = ?", (id_.lower(),))

    def upsert_locked(self, p: dict) -> bool:
        """Inserts a payment from OfframpLocked; returns True if it was new."""
        t = now()
        row = {
            "id": p["id"].lower(),
            "chain": p["chain"],
            "user": p["user"],
            "amount": str(p["amount"]),
            "inr_paise": int(p["inr_paise"]),
            "rate_e6": str(p["rate_e6"]),
            "qr_commitment": p["qr_commitment"].lower(),
            "status": "LOCKED",
            "lock_tx": p.get("lock_tx"),
            "lock_block": p.get("lock_block"),
            "locked_at": p.get("locked_at"),
            "completable_at": p.get("completable_at"),
            "reclaimable_at": p.get("reclaimable_at"),
            "dispute_window": p.get("dispute_window"),
            "txs": json.dumps({"lock": p.get("lock_tx")} if p.get("lock_tx") else {}),
            "created_at": t,
            "updated_at": t,
        }
        cols = ",".join(row)
        cur = self._exec(
            f"INSERT OR IGNORE INTO payments ({cols}) VALUES ({','.join('?' * len(row))})", row.values()
        )
        if cur.rowcount == 0:
            # A status event may have created a bare mirror row first; fill in the lock data.
            self._exec(
                "UPDATE payments SET user = COALESCE(user, ?), amount = COALESCE(amount, ?), "
                "inr_paise = COALESCE(inr_paise, ?), rate_e6 = COALESCE(rate_e6, ?), "
                "qr_commitment = COALESCE(qr_commitment, ?), lock_tx = COALESCE(lock_tx, ?), "
                "lock_block = COALESCE(lock_block, ?), locked_at = COALESCE(locked_at, ?), "
                "completable_at = COALESCE(completable_at, ?), reclaimable_at = COALESCE(reclaimable_at, ?), "
                "dispute_window = COALESCE(dispute_window, ?), updated_at = ? WHERE id = ?",
                (
                    row["user"], row["amount"], row["inr_paise"], row["rate_e6"], row["qr_commitment"],
                    row["lock_tx"], row["lock_block"], row["locked_at"], row["completable_at"],
                    row["reclaimable_at"], row["dispute_window"], t, row["id"],
                ),
            )
            return False
        return True

    def ensure_payment(self, id_: str, chain: str, status: str = "NONE") -> None:
        t = now()
        self._exec(
            "INSERT OR IGNORE INTO payments (id, chain, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
            (id_.lower(), chain, status, t, t),
        )

    def update_payment(self, id_: str, **fields: Any) -> None:
        if not fields:
            return
        fields["updated_at"] = now()
        sets = ", ".join(f"{k} = ?" for k in fields)
        self._exec(f"UPDATE payments SET {sets} WHERE id = ?", (*fields.values(), id_.lower()))

    def add_tx(self, id_: str, label: str, tx_hash: str) -> None:
        with self._lock:
            row = self.get_payment(id_)
            if not row:
                return
            txs = json.loads(row["txs"] or "{}")
            txs[label] = tx_hash
            self.update_payment(id_, txs=json.dumps(txs))

    def cas_dispatch(self, id_: str, from_state: str, to_state: str, detail: str | None = None, **fields: Any) -> bool:
        """Atomic dispatch-state transition; only one caller can move a row out of `from_state`."""
        assert to_state in DISPATCH_STATES
        extra = "".join(f", {k} = ?" for k in fields)
        cur = self._exec(
            f"UPDATE payments SET dispatch_state = ?, dispatch_detail = COALESCE(?, dispatch_detail), updated_at = ?{extra} "
            "WHERE id = ? AND dispatch_state = ?",
            (to_state, detail, now(), *fields.values(), id_.lower(), from_state),
        )
        return cur.rowcount == 1

    def payments_where(self, chain: str | None = None, statuses: Iterable[str] | None = None,
                       dispatch: Iterable[str] | None = None) -> list[dict]:
        sql, params = "SELECT * FROM payments WHERE 1=1", []
        if chain:
            sql += " AND chain = ?"
            params.append(chain)
        if statuses:
            st = list(statuses)
            sql += f" AND status IN ({','.join('?' * len(st))})"
            params += st
        if dispatch:
            ds = list(dispatch)
            sql += f" AND dispatch_state IN ({','.join('?' * len(ds))})"
            params += ds
        return self._all(sql + " ORDER BY created_at", params)

    def pending_refunds(self, chain: str | None = None) -> list[dict]:
        sql = "SELECT * FROM payments WHERE pending_refund IS NOT NULL AND status = 'LOCKED'"
        params: list[Any] = []
        if chain:
            sql += " AND chain = ?"
            params.append(chain)
        return self._all(sql, params)

    def list_payments(self, user: str | None = None, chain: str | None = None, limit: int = 50) -> list[dict]:
        sql, params = "SELECT * FROM payments WHERE 1=1", []
        if user:
            sql += " AND lower(user) = lower(?)"
            params.append(user)
        if chain:
            sql += " AND chain = ?"
            params.append(chain)
        sql += " ORDER BY COALESCE(locked_at, created_at) DESC LIMIT ?"
        params.append(limit)
        return self._all(sql, params)

    # ---- onramp --------------------------------------------------------------------------------
    def insert_onramp(self, o: dict) -> None:
        t = now()
        o = {**o, "created_at": t, "updated_at": t}
        self._exec(f"INSERT INTO onramp_orders ({','.join(o)}) VALUES ({','.join('?' * len(o))})", o.values())

    def get_onramp(self, id_: str) -> dict | None:
        return self._one("SELECT * FROM onramp_orders WHERE id = ?", (id_.lower(),))

    def cas_onramp(self, id_: str, from_status: str, to_status: str, **fields: Any) -> bool:
        extra = "".join(f", {k} = ?" for k in fields)
        cur = self._exec(
            f"UPDATE onramp_orders SET status = ?, updated_at = ?{extra} WHERE id = ? AND status = ?",
            (to_status, now(), *fields.values(), id_.lower(), from_status),
        )
        return cur.rowcount == 1

    def onramps_by_status(self, status: str) -> list[dict]:
        return self._all("SELECT * FROM onramp_orders WHERE status = ?", (status,))

    # ---- cursors -------------------------------------------------------------------------------
    def get_cursor(self, chain: str) -> int | None:
        row = self._one("SELECT block FROM cursors WHERE chain = ?", (chain,))
        return int(row["block"]) if row else None

    def set_cursor(self, chain: str, block: int) -> None:
        self._exec(
            "INSERT INTO cursors (chain, block) VALUES (?, ?) ON CONFLICT(chain) DO UPDATE SET block = excluded.block",
            (chain, int(block)),
        )

    # ---- disputes ------------------------------------------------------------------------------
    def get_dispute(self, id_: str) -> dict | None:
        return self._one("SELECT * FROM disputes WHERE id = ?", (id_.lower(),))

    def begin_dispute(self, d: dict) -> bool:
        """Claims the relay slot for one offramp. A FAILED attempt may be retried."""
        t = now()
        with self._lock:
            existing = self.get_dispute(d["id"])
            if existing and existing["status"] != "FAILED":
                return False
            self._exec("DELETE FROM disputes WHERE id = ? AND status = 'FAILED'", (d["id"].lower(),))
            cur = self._exec(
                "INSERT OR IGNORE INTO disputes (id, chain, user, deadline, user_sig, status, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, 'RELAYING', ?, ?)",
                (d["id"].lower(), d["chain"], d["user"], int(d["deadline"]), d["user_sig"], t, t),
            )
            return cur.rowcount == 1

    def finish_dispute(self, id_: str, status: str, tx: str | None = None, error: str | None = None) -> None:
        self._exec(
            "UPDATE disputes SET status = ?, tx = ?, error = ?, updated_at = ? WHERE id = ?",
            (status, tx, error, now(), id_.lower()),
        )

    # ---- events --------------------------------------------------------------------------------
    def insert_event(self, ev: dict) -> bool:
        cur = self._exec(
            "INSERT OR IGNORE INTO events (chain, tx, log_index, block, name, ref_id, args, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (ev["chain"], ev["tx"], ev["log_index"], ev["block"], ev["name"], (ev.get("ref_id") or "").lower() or None,
             json.dumps(ev["args"]), now()),
        )
        return cur.rowcount == 1

    def list_events(self, chain: str | None = None, ref_id: str | None = None, limit: int = 100) -> list[dict]:
        sql, params = "SELECT * FROM events WHERE 1=1", []
        if chain:
            sql += " AND chain = ?"
            params.append(chain)
        if ref_id:
            sql += " AND ref_id = ?"
            params.append(ref_id.lower())
        sql += " ORDER BY block DESC, log_index DESC LIMIT ?"
        params.append(limit)
        rows = self._all(sql, params)
        for r in rows:
            r["args"] = json.loads(r["args"])
        return rows
