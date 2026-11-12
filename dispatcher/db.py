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

CREATE TABLE IF NOT EXISTS baseline (
    sha        TEXT PRIMARY KEY,           -- base commit the gate was run against
    green      INTEGER NOT NULL,
    tail       TEXT NOT NULL DEFAULT '',
    checked_at TEXT NOT NULL,
    boot_id    TEXT                        -- machine boot the result is valid for
);

CREATE TABLE IF NOT EXISTS control (
    id     INTEGER PRIMARY KEY CHECK (id = 1),
    paused INTEGER NOT NULL DEFAULT 0
);
INSERT OR IGNORE INTO control (id, paused) VALUES (1, 0);
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
        self.conn.execute("PRAGMA busy_timeout=5000")  # tolerate TUI/daemon concurrent writes
        self.conn.executescript(SCHEMA)
        self._migrate()
        self.conn.commit()
        self._lock = threading.RLock()

    def _migrate(self) -> None:
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(tasks)")}
        bcols = {r[1] for r in self.conn.execute("PRAGMA table_info(baseline)")}
        if bcols and "boot_id" not in bcols:
            self.conn.execute("ALTER TABLE baseline ADD COLUMN boot_id TEXT")
        if "tested_sha" not in cols:
            self.conn.execute("ALTER TABLE tasks ADD COLUMN tested_sha TEXT")
        if "session_cum" not in cols:
            self.conn.execute("ALTER TABLE tasks ADD COLUMN session_cum REAL NOT NULL DEFAULT 0")
        if "resumable" not in cols:
            self.conn.execute("ALTER TABLE tasks ADD COLUMN resumable INTEGER NOT NULL DEFAULT 0")
        if "pr_status" not in cols:
            self.conn.execute("ALTER TABLE tasks ADD COLUMN pr_status TEXT")
        if "lifetime_cost" not in cols:
            self.conn.execute("ALTER TABLE tasks ADD COLUMN lifetime_cost REAL NOT NULL DEFAULT 0")
        if "pruned" not in cols:
            self.conn.execute("ALTER TABLE tasks ADD COLUMN pruned INTEGER NOT NULL DEFAULT 0")
        if "cancel_requested" not in cols:
            self.conn.execute(
                "ALTER TABLE tasks ADD COLUMN cancel_requested INTEGER NOT NULL DEFAULT 0"
            )
        if "slack_channel" not in cols:      # the planner thread to post this task's events in
            self.conn.execute("ALTER TABLE tasks ADD COLUMN slack_channel TEXT")
        if "slack_thread" not in cols:
            self.conn.execute("ALTER TABLE tasks ADD COLUMN slack_thread TEXT")
        if "touches" not in cols:            # planned files (from the task) and changed files (from the diff)
            self.conn.execute("ALTER TABLE tasks ADD COLUMN touches TEXT")
        if "changed" not in cols:
            self.conn.execute("ALTER TABLE tasks ADD COLUMN changed TEXT")

    # --- tasks -----------------------------------------------------------
    def upsert_claim(self, task_id: int, slug: str, branch: str) -> None:
        with self._lock:
            self.conn.execute(
                """INSERT INTO tasks (task_id, slug, branch, state, started_at, updated_at)
                   VALUES (?, ?, ?, 'claimed', ?, ?)
                   ON CONFLICT(task_id) DO UPDATE SET
                       slug=excluded.slug, branch=excluded.branch,
                       state='claimed', updated_at=excluded.updated_at,
                       started_at=excluded.started_at,
                       -- a re-queued task is a fresh attempt: fresh budget and retries
                       test_retries=0, review_retries=0, cost_usd=0,
                       cancel_requested=0, pruned=0, pid=NULL, pr_url=NULL, pr_status=NULL,
                       touches=NULL, changed=NULL""",
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

    def thread(self, task_id: int):
        """(channel, thread_ts) of the Slack thread this task reports into, or None."""
        row = self.get(task_id)
        if row is None or not row["slack_channel"] or not row["slack_thread"]:
            return None
        return row["slack_channel"], row["slack_thread"]

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

    def occupied(self) -> list[sqlite3.Row]:
        """Tasks holding files: in progress, or with a PR open and not yet merged."""
        states = (*ACTIVE_STATES, "pr_open")
        with self._lock:
            return self.conn.execute(
                f"SELECT * FROM tasks WHERE state IN ({','.join('?' * len(states))})", states
            ).fetchall()

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
                "UPDATE tasks SET cost_usd=?, lifetime_cost=lifetime_cost+?, updated_at=? "
                "WHERE task_id=?",
                (base + delta_usd, delta_usd, _now(), task_id),
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

    def all_tasks(self) -> list[sqlite3.Row]:
        with self._lock:
            return self.conn.execute(
                "SELECT * FROM tasks ORDER BY task_id"
            ).fetchall()

    # --- control (pause / cancel) ---------------------------------------
    def is_paused(self) -> bool:
        with self._lock:
            row = self.conn.execute("SELECT paused FROM control WHERE id=1").fetchone()
            return bool(row and row["paused"])

    def set_paused(self, paused: bool) -> None:
        with self._lock:
            self.conn.execute("UPDATE control SET paused=? WHERE id=1", (1 if paused else 0,))
            self.conn.commit()

    def request_cancel(self, task_id: int) -> None:
        self.update(task_id, cancel_requested=1)

    def clear_cancel(self, task_id: int) -> None:
        self.update(task_id, cancel_requested=0)

    def cancel_requested(self, task_id: int) -> bool:
        row = self.get(task_id)
        return bool(row and row["cancel_requested"])

    def pending_cancels(self) -> list[sqlite3.Row]:
        q = ",".join("?" * len(ACTIVE_STATES))
        with self._lock:
            return self.conn.execute(
                f"SELECT * FROM tasks WHERE cancel_requested=1 AND state IN ({q})",
                ACTIVE_STATES,
            ).fetchall()

    def stale_blocked(self, older_than_days: int) -> list[sqlite3.Row]:
        """Blocked tasks whose worktree is still around past the retention window."""
        cutoff = (dt.datetime.now() - dt.timedelta(days=older_than_days)).isoformat(timespec="seconds")
        with self._lock:
            return self.conn.execute(
                "SELECT * FROM tasks WHERE state='blocked' AND pruned=0 AND updated_at < ?",
                (cutoff,),
            ).fetchall()

    # --- baseline (gate result per base commit) -----------------------------------------
    def get_baseline(self, sha: str, boot_id: str | None = None,
                     max_age_h: float = 6.0) -> sqlite3.Row | None:
        """A cached gate result for this base commit, if it is recent AND from this boot.

        A reboot can break the environment (a container that does not restart) without any
        code change, so a result recorded before the last boot is not trusted. Results from
        before this column existed have no boot id and are treated the same way.
        """
        cutoff = (dt.datetime.now() - dt.timedelta(hours=max_age_h)).isoformat(timespec="seconds")
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM baseline WHERE sha=? AND checked_at >= ?", (sha, cutoff)
            ).fetchone()
        if row is not None and boot_id and (row["boot_id"] or "") != boot_id:
            return None
        return row

    def set_baseline(self, sha: str, green: bool, tail: str = "", boot_id: str = "") -> None:
        with self._lock:
            self.conn.execute(
                """INSERT INTO baseline (sha, green, tail, checked_at, boot_id) VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(sha) DO UPDATE SET green=excluded.green, tail=excluded.tail,
                       checked_at=excluded.checked_at, boot_id=excluded.boot_id""",
                (sha, 1 if green else 0, tail[-2000:], _now(), boot_id or None),
            )
            self.conn.commit()

    def finished_before(self, days: int) -> list[sqlite3.Row]:
        """Tasks that are done/blocked and haven't changed for `days` (for log pruning)."""
        cutoff = (dt.datetime.now() - dt.timedelta(days=days)).isoformat(timespec="seconds")
        with self._lock:
            return self.conn.execute(
                "SELECT * FROM tasks WHERE state IN ('done','blocked') AND updated_at < ?",
                (cutoff,),
            ).fetchall()

    def delete_task(self, task_id: int) -> None:
        with self._lock:
            self.conn.execute("DELETE FROM tasks WHERE task_id=?", (task_id,))
            self.conn.commit()

    def close(self) -> None:
        self.conn.close()
