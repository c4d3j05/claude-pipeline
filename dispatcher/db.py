"""SQLite ledger: restart recovery and the cost ledger.

Vikunja is the source of truth for the queue; this file exists only so a restarted
dispatcher can reap orphaned workers by PID and so daily cost survives restarts.
"""

from __future__ import annotations

import datetime as dt
import sqlite3
import threading
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    task_id        INTEGER PRIMARY KEY,
    slug           TEXT NOT NULL,
    branch         TEXT NOT NULL,
    session_id     TEXT,
    workspace      TEXT,
    pid            INTEGER,
    state          TEXT NOT NULL,           -- claimed|working|testing|reviewing|pr_open|blocked|done
    test_retries   INTEGER NOT NULL DEFAULT 0,
    review_retries INTEGER NOT NULL DEFAULT 0,
    cost_usd       REAL NOT NULL DEFAULT 0,
    pr_url         TEXT,
    started_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS daily_cost (
    day          TEXT PRIMARY KEY,          -- YYYY-MM-DD (local)
    cost_usd     REAL NOT NULL DEFAULT 0,
    cap_notified INTEGER NOT NULL DEFAULT 0
);
"""

ACTIVE_STATES = ("claimed", "working", "testing", "reviewing")


def _now() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


def _today() -> str:
    return dt.date.today().isoformat()


class Ledger:
    """Thread-safe (one RLock; low volume) SQLite wrapper."""

    def __init__(self, path: Path):
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)
        self.conn.commit()
        self._lock = threading.RLock()

    # --- tasks -----------------------------------------------------------
    def upsert_claim(self, task_id: int, slug: str, branch: str) -> None:
        with self._lock:
            self.conn.execute(
                """INSERT INTO tasks (task_id, slug, branch, state, started_at, updated_at)
                   VALUES (?, ?, ?, 'claimed', ?, ?)
                   ON CONFLICT(task_id) DO UPDATE SET
                       slug=excluded.slug, branch=excluded.branch,
                       state='claimed', updated_at=excluded.updated_at""",
                (task_id, slug, branch, _now(), _now()),
            )
            self.conn.commit()

    def update(self, task_id: int, **fields) -> None:
        if not fields:
            return
        fields["updated_at"] = _now()
        cols = ", ".join(f"{k}=?" for k in fields)
        with self._lock:
            self.conn.execute(
                f"UPDATE tasks SET {cols} WHERE task_id=?", (*fields.values(), task_id)
            )
            self.conn.commit()

    def get(self, task_id: int) -> sqlite3.Row | None:
        with self._lock:
            cur = self.conn.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,))
            return cur.fetchone()

    def active(self) -> list[sqlite3.Row]:
        q = ",".join("?" * len(ACTIVE_STATES))
        with self._lock:
            cur = self.conn.execute(
                f"SELECT * FROM tasks WHERE state IN ({q})", ACTIVE_STATES
            )
            return cur.fetchall()

    def active_count(self) -> int:
        return len(self.active())

    def is_tracked_active(self, task_id: int) -> bool:
        row = self.get(task_id)
        return bool(row and row["state"] in ACTIVE_STATES)

    # --- cost ------------------------------------------------------------
    def add_cost(self, task_id: int, delta_usd: float) -> None:
        if not delta_usd:
            return
        with self._lock:
            row = self.get(task_id)
            base = row["cost_usd"] if row else 0.0
            self.conn.execute(
                "UPDATE tasks SET cost_usd=?, updated_at=? WHERE task_id=?",
                (base + delta_usd, _now(), task_id),
            )
            self.conn.execute(
                """INSERT INTO daily_cost (day, cost_usd) VALUES (?, ?)
                   ON CONFLICT(day) DO UPDATE SET cost_usd = cost_usd + ?""",
                (_today(), delta_usd, delta_usd),
            )
            self.conn.commit()

    def today_cost(self) -> float:
        with self._lock:
            cur = self.conn.execute(
                "SELECT cost_usd FROM daily_cost WHERE day=?", (_today(),)
            )
            row = cur.fetchone()
            return row["cost_usd"] if row else 0.0

    def cap_already_notified(self) -> bool:
        with self._lock:
            cur = self.conn.execute(
                "SELECT cap_notified FROM daily_cost WHERE day=?", (_today(),)
            )
            row = cur.fetchone()
            return bool(row and row["cap_notified"])

    def mark_cap_notified(self) -> None:
        with self._lock:
            self.conn.execute(
                """INSERT INTO daily_cost (day, cost_usd, cap_notified) VALUES (?, 0, 1)
                   ON CONFLICT(day) DO UPDATE SET cap_notified=1""",
                (_today(),),
            )
            self.conn.commit()

    def close(self) -> None:
        self.conn.close()
