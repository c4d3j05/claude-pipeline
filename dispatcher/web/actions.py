"""What the dashboard may do (OpenSpec change dashboard-actions).

Every method validates its input first and answers a `Result`; nothing here raises to the caller.
Commands are always argument lists (never a shell string), and the only programs run are
`uv run explorer ...`, `systemctl --user restart <unit>` and `systemd-run`, with ids, letters,
actions, lanes and units checked against fixed patterns or the configured unit list.
"""

from __future__ import annotations

import datetime as dt
import json
import re
import subprocess
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .. import dashboard
from ..config import Config
from ..db import Ledger
from ..log import get
from . import settings, views
from .sources import Sources

log = get("web")

COMMAND_TIMEOUT_SEC = 60
SELF_UNIT = "claude-pipeline-web"
QUESTION_RE = re.compile(r"^q-[0-9a-f]{12}$")
BATCH_RE = re.compile(r"^B-\d+$")
LANE_RE = re.compile(r"^[a-z][a-z0-9-]{0,39}$")
LETTERS = ("A", "B", "C")
BATCH_ACTIONS = ("send", "hold", "unhold")
PLANNER_BUSY = ("provisioning", "working", "filing")


@dataclass
class Result:
    ok: bool            # done
    refused: bool       # not done on purpose (busy, unknown target)
    message: str        # one line shown to the user and logged


def done(message: str) -> Result:
    return Result(True, False, message)


def refuse(message: str) -> Result:
    return Result(False, True, message)


def failed(message: str) -> Result:
    return Result(False, False, message)


def _last_line(text: str | None) -> str:
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    return lines[-1][:300] if lines else ""


class Actions:
    def __init__(self, cfg: Config, sources: Sources, *, run=subprocess.run,
                 ledger_factory: Callable[[], Ledger] | None = None,
                 now: Callable[[], dt.datetime] | None = None):
        self.cfg, self.sources, self.run = cfg, sources, run
        self.ledger_factory = ledger_factory or (lambda: Ledger(cfg.db_path))
        self.now = now or (lambda: dt.datetime.now(dt.UTC))
        self._log_lock = threading.Lock()

    # --- running a command ------------------------------------------------------------------
    def _exec(self, cmd: list[str], *, cwd=None, fallback: str = "done") -> Result:
        kw = {"cwd": cwd} if cwd is not None else {}
        try:
            r = self.run(cmd, capture_output=True, text=True, timeout=COMMAND_TIMEOUT_SEC, **kw)
        except subprocess.TimeoutExpired:
            return failed(f"failed: took longer than {COMMAND_TIMEOUT_SEC}s")
        except OSError as e:
            return failed(f"failed: could not run {cmd[0]}: {e}"[:300])
        if r.returncode == 0:
            return done(_last_line(r.stdout) or fallback)
        return failed("failed: " + (_last_line(r.stderr) or _last_line(r.stdout)
                                    or f"exited {r.returncode}"))

    def _explorer(self, args: list[str], fallback: str) -> Result:
        d = (self.cfg.dashboard_explorer_dir or "").strip()
        if not d:
            return refuse("explorer is not configured (DASHBOARD_EXPLORER_DIR)")
        path = Path(d).expanduser()
        if not path.is_dir():
            return refuse(f"{path} is not a directory on this host")
        return self._exec(["uv", "run", "--quiet", "explorer", *args], cwd=path, fallback=fallback)

    # --- explorer -----------------------------------------------------------------------------
    def explorer_run(self, lane: str | None) -> Result:
        lane = (lane or "").strip() or None
        args = ["request-run"]
        if lane == "global":
            args.append("--global")
        elif lane is not None:
            if not LANE_RE.fullmatch(lane):
                return refuse("unknown lane")
            args += ["--lane", lane]
        return self._explorer([*args, "--by", "dashboard"], "run requested")

    def explorer_answer(self, question_id: str, letter: str) -> Result:
        if not isinstance(question_id, str) or not QUESTION_RE.fullmatch(question_id):
            return refuse("not a question id")
        if letter not in LETTERS:
            return refuse("the answer must be A, B or C")
        return self._explorer(["answer", question_id, letter], f"answered {question_id} with {letter}")

    def explorer_batch(self, action: str, batch_id: str) -> Result:
        if action not in BATCH_ACTIONS:
            return refuse("the batch action must be send, hold or unhold")
        if not isinstance(batch_id, str) or not BATCH_RE.fullmatch(batch_id):
            return refuse("not a batch id")
        return self._explorer(["batch", action, batch_id], f"{batch_id}: {action} done")

    # --- pipeline -----------------------------------------------------------------------------
    def pipeline_retry(self, task_id: int) -> Result:
        if isinstance(task_id, bool) or not isinstance(task_id, int) or task_id <= 0:
            return refuse("not a task id")
        try:
            ledger = self.ledger_factory()
        except Exception as e:  # noqa: BLE001
            return failed(f"failed: cannot open the ledger: {e}"[:300])
        try:
            row = ledger.get(task_id)
            if row is None:
                return refuse(f"task {task_id} is not tracked")
            if row["state"] != "blocked":
                return refuse(f"task {task_id} is {row['state']}")
            dashboard.retry_task(self.cfg, ledger, task_id)
            return done(f"#{task_id} re-queued")
        except Exception as e:  # noqa: BLE001 - Vikunja or the ledger failing is a message, not a 500
            return failed(f"failed: {e}"[:300] or "failed")
        finally:
            close = getattr(ledger, "close", None)
            if close:
                close()

    # --- services -----------------------------------------------------------------------------
    def restart(self, unit: str) -> Result:
        allowed = [u for u in self.cfg.dashboard_units if not u.endswith(".timer")]
        if not isinstance(unit, str) or unit not in allowed:
            return refuse("unknown unit")
        busy = self._busy(unit)
        if busy:
            return refuse(busy)
        if unit == SELF_UNIT:
            r = self._exec(["systemd-run", "--user", "--on-active=2", "systemctl", "--user",
                            "restart", SELF_UNIT], fallback="")
            return done("restarting the dashboard in 2 s; reload the page") if r.ok else r
        return self._exec(["systemctl", "--user", "restart", unit], fallback=f"{unit} restarted")

    def _busy(self, unit: str) -> str | None:
        """Why this unit must not be restarted now, or None."""
        if unit == "explorer-serve":
            res = self.sources.fresh("explorer")
            if not res.ok:
                return f"cannot tell whether explorer is busy: {res.error or 'not configured'}"
            run = res.data.get("run") or res.data.get("active_run")
            if run:
                run = run if isinstance(run, dict) else {}
                since = views.ago(run.get("started") or run.get("started_at"), self.now())
                return (f"explorer is running a {run.get('reason') or 'scheduled'} run "
                        f"({run.get('id') or '?'}) since {since}")
        elif unit == "claude-pipeline-dispatcher":
            try:
                ledger = self.ledger_factory()
            except Exception as e:  # noqa: BLE001
                return f"cannot tell whether tickets are building: {e}"[:300]
            try:
                rows = ledger.active()
            finally:
                close = getattr(ledger, "close", None)
                if close:
                    close()
            if rows:
                ids = ", ".join(f"#{r['task_id']}" for r in rows)
                return f"{len(rows)} ticket(s) building: {ids}"
        elif unit == "claude-planner":
            res = self.sources.fresh("planner")
            if not res.ok:
                return f"cannot tell whether planner is busy: {res.error or 'not configured'}"
            for i in res.data.get("investigations") or []:
                if str(i.get("state") or "").lower() in PLANNER_BUSY:
                    return f"planner is working on {i.get('id')}"
        return None

    # --- settings -----------------------------------------------------------------------------
    def save_setting(self, service: str, key: str, value: str) -> Result:
        """Change one allow-listed setting of a service's .env (see web/settings.py)."""
        setting = settings.find(service, key)
        if setting is None:
            return refuse("not a setting that can be changed here")
        if settings.path_of(self.cfg, service) is None:
            return refuse(f"{service} is not configured")
        error = settings.validate(service, key, value, settings.read(self.cfg, service))
        if error:
            return refuse(error)
        try:
            settings.write(self.cfg, service, key, value, self.now())
        except (OSError, ValueError) as e:
            return failed(f"failed: {e}"[:300])
        shown = f"{key}={settings.normalize(service, key, value)}"
        if setting.restart:
            return done(f"{shown} saved; restart {setting.restart} to apply")
        return done(f"{shown} saved; applies now")

    # --- the action log -----------------------------------------------------------------------
    def log(self, request_ip: str, action: str, target: str, result: Result) -> None:
        entry = {"at": self.now().isoformat(timespec="seconds"), "ip": request_ip, "action": action,
                 "target": target, "ok": result.ok, "refused": result.refused,
                 "message": result.message}
        try:
            with self._log_lock:
                path = self.cfg.dashboard_action_log
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(entry) + "\n")
        except Exception as e:  # noqa: BLE001 - logging must never break an action
            log.warning("could not write the dashboard action log: %s", e)

    def recent(self, n: int = 20) -> list[dict]:
        try:
            lines = self.cfg.dashboard_action_log.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        out: list[dict] = []
        for line in reversed(lines):
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict):
                out.append(row)
                if len(out) >= n:
                    break
        return out
