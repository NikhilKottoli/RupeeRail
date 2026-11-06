"""Agents and their run history, in one local SQLite file.

The file holds people's profiles, so it's created private and stays git-ignored. Only people write agents, through
the panel. Runs are written by code, and nothing a model or a page says is stored as something to act on later.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import uuid
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS agents (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT NOT NULL,
    avatar TEXT NOT NULL,
    colour TEXT NOT NULL,
    instructions TEXT NOT NULL,
    profile TEXT NOT NULL,
    sites TEXT NOT NULL,
    start_url TEXT NOT NULL,
    per_order_max_inr INTEGER NOT NULL,
    budget_usdc TEXT NOT NULL,
    wallet TEXT UNIQUE,
    wallet_status TEXT NOT NULL,
    wallet_detail TEXT NOT NULL,
    created REAL NOT NULL,
    archived REAL
);
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL REFERENCES agents(id),
    prompt TEXT NOT NULL,
    rehearse INTEGER NOT NULL,
    max_paise INTEGER NOT NULL,
    status TEXT,
    detail TEXT NOT NULL DEFAULT '',
    site TEXT NOT NULL DEFAULT '',
    amount_paise INTEGER,
    offramp_id TEXT,
    tx_hash TEXT,
    started REAL NOT NULL,
    finished REAL
);
CREATE INDEX IF NOT EXISTS runs_by_agent ON runs (agent_id, started DESC);
"""
EDITABLE = ("name", "description", "avatar", "colour", "instructions", "profile", "sites", "start_url", "per_order_max_inr")
BUSY_WALLET_STATES = ("pending", "creating", "funding", "approving")


class AgentStore:
    def __init__(self, path: str) -> None:
        if path != ":memory:":
            target = Path(path)
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.close(os.open(target, os.O_CREAT | os.O_RDWR, 0o600))
            os.chmod(target, 0o600)  # also tightens a file made by an older version
        self.lock = threading.RLock()
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.db.commit()
        self._recover()

    def _recover(self) -> None:
        """A restart ends anything that was in progress, so nothing is left looking as if it's still running."""
        with self.lock:
            self.db.execute("UPDATE runs SET status = 'INTERRUPTED', detail = 'the agent server restarted during this run', finished = ? WHERE finished IS NULL", (time.time(),))
            marks = ",".join("?" for _ in BUSY_WALLET_STATES)
            self.db.execute(f"UPDATE agents SET wallet_status = 'failed', wallet_detail = 'the agent server restarted during setup. Try again' WHERE wallet_status IN ({marks})", BUSY_WALLET_STATES)
            self.db.commit()

    @staticmethod
    def _agent(row: sqlite3.Row) -> dict:
        return {**dict(row), "sites": json.loads(row["sites"])}

    # ---- agents ---------------------------------------------------------------------------------

    def create_agent(self, fields: dict, budget_usdc: str) -> dict:
        agent_id = str(uuid.uuid4())  # also Circle's idempotency key, which must be a UUID
        with self.lock:
            self.db.execute(
                "INSERT INTO agents (id, name, description, avatar, colour, instructions, profile, sites, start_url, per_order_max_inr, budget_usdc, wallet_status, wallet_detail, created) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,'pending','',?)",
                (agent_id, fields["name"], fields["description"], fields["avatar"], fields["colour"], fields["instructions"], fields["profile"],
                 json.dumps(fields["sites"]), fields["start_url"], fields["per_order_max_inr"], budget_usdc, time.time()),
            )
            self.db.commit()
        return self.agent(agent_id)

    def agent(self, agent_id: str) -> dict | None:
        with self.lock:
            row = self.db.execute("SELECT * FROM agents WHERE id = ?", (agent_id,)).fetchone()
        return self._agent(row) if row else None

    def agents(self) -> list[dict]:
        with self.lock:
            rows = self.db.execute("SELECT * FROM agents WHERE archived IS NULL ORDER BY created").fetchall()
        return [self._agent(row) for row in rows]

    def update_agent(self, agent_id: str, fields: dict) -> dict:
        values = {k: json.dumps(v) if k == "sites" else v for k, v in fields.items() if k in EDITABLE}
        if values:
            assignments = ", ".join(f"{k} = ?" for k in values)  # the keys come from EDITABLE, never from a request
            with self.lock:
                self.db.execute(f"UPDATE agents SET {assignments} WHERE id = ?", (*values.values(), agent_id))
                self.db.commit()
        return self.agent(agent_id)

    def archive_agent(self, agent_id: str) -> None:
        with self.lock:
            self.db.execute("UPDATE agents SET archived = ? WHERE id = ?", (time.time(), agent_id))
            self.db.commit()

    def set_wallet_state(self, agent_id: str, status: str, detail: str = "") -> None:
        with self.lock:
            self.db.execute("UPDATE agents SET wallet_status = ?, wallet_detail = ? WHERE id = ?", (status, detail, agent_id))
            self.db.commit()

    def set_budget(self, agent_id: str, budget_usdc: str) -> None:
        with self.lock:
            self.db.execute("UPDATE agents SET budget_usdc = ? WHERE id = ?", (budget_usdc, agent_id))
            self.db.commit()

    def assign_wallet(self, agent_id: str, address: str) -> None:
        with self.lock:
            self.db.execute("UPDATE agents SET wallet = ? WHERE id = ?", (address, agent_id))
            self.db.commit()

    def assigned_wallets(self) -> set[str]:
        """Includes archived agents, whose history still points at their wallets."""
        with self.lock:
            rows = self.db.execute("SELECT wallet FROM agents WHERE wallet IS NOT NULL").fetchall()
        return {row["wallet"].lower() for row in rows}

    # ---- runs -----------------------------------------------------------------------------------

    def start_run(self, run_id: str, agent_id: str, prompt: str, rehearse: bool, max_paise: int) -> None:
        with self.lock:
            self.db.execute("INSERT INTO runs (id, agent_id, prompt, rehearse, max_paise, started) VALUES (?,?,?,?,?,?)",
                            (run_id, agent_id, prompt, int(rehearse), max_paise, time.time()))
            self.db.commit()

    def note_submitted(self, run_id: str, offramp_id: str) -> None:
        """Written before the payment is sent, so a crash can't lose track of it."""
        with self.lock:
            self.db.execute("UPDATE runs SET offramp_id = ? WHERE id = ?", (offramp_id, run_id))
            self.db.commit()

    def finish_run(self, run_id: str, status: str, detail: str, site: str, amount_paise: int | None, offramp_id: str | None, tx_hash: str | None) -> None:
        with self.lock:
            self.db.execute("UPDATE runs SET status = ?, detail = ?, site = ?, amount_paise = ?, offramp_id = COALESCE(?, offramp_id), tx_hash = ?, finished = ? WHERE id = ?",
                            (status, detail, site, amount_paise, offramp_id, tx_hash, time.time(), run_id))
            self.db.commit()

    def runs(self, agent_id: str, limit: int = 20) -> list[dict]:
        with self.lock:
            rows = self.db.execute("SELECT * FROM runs WHERE agent_id = ? ORDER BY started DESC LIMIT ?", (agent_id, limit)).fetchall()
        return [dict(row) for row in rows]

    def past_runs_text(self, agent_id: str, limit: int = 5) -> list[str]:
        """What the agent gets told about its past: facts recorded by code, with no page or model text."""
        with self.lock:
            rows = self.db.execute("SELECT * FROM runs WHERE agent_id = ? AND rehearse = 0 AND finished IS NOT NULL ORDER BY started DESC LIMIT ?", (agent_id, limit)).fetchall()
        lines = []
        for row in rows:
            day = time.strftime("%Y-%m-%d", time.localtime(row["started"]))
            amount = f", Rs {row['amount_paise'] / 100:g}" if row["amount_paise"] is not None else ""
            site = f" on {row['site']}" if row["site"] else ""
            lines.append(f"{day}: asked {json.dumps(row['prompt'], ensure_ascii=False)}{site}{amount}, result {row['status']}")
        return lines
