"""Where the dashboard's data comes from. No web framework here, so it is tested on its own.

Five sources, each answered as a `Result` and cached for DASHBOARD_REFRESH_SEC so any number of
open browsers costs one read per source per period:
- `pipeline`: this repo's ledger (`dashboard.gather_status`, plus each task's timestamps);
- `board`: the Vikunja board, read with GETs only and grouped by bucket;
- `planner` / `explorer`: `uv run <cli> status --json` in that checkout, schema 1;
- `services`: `systemctl --user show` for each unit in DASHBOARD_UNITS (`services.py`).

A source that fails, times out or is not configured becomes a Result with an error, so one broken
service never breaks the page.
"""

from __future__ import annotations

import datetime as dt
import json
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from ..config import Config
from ..dashboard import gather_status
from ..db import Ledger
from ..log import get
from ..releases import is_tracker, labels_of
from ..vikunja import Vikunja
from .services import read_services

log = get("web")

SCHEMA = 1
STATUS_TIMEOUT_SEC = 20
CLIS = {"planner": "claude-planner", "explorer": "explorer"}


@dataclass
class Result:
    name: str
    configured: bool = True
    data: dict | None = None
    error: str = ""
    fetched_at: str = field(default_factory=lambda: dt.datetime.now(dt.UTC).isoformat(timespec="seconds"))

    @property
    def ok(self) -> bool:
        return self.configured and self.data is not None and not self.error


def web_root(cfg: Config) -> str:
    """Vikunja's site address (VIKUNJA_URL without `/api/v1`), for task links."""
    return cfg.vikunja_url.rsplit("/api/", 1)[0]


# --- the four sources -----------------------------------------------------------------------------
def read_pipeline(cfg: Config, ledger: Ledger, logs_root: Path = Path("logs")) -> dict:
    st = gather_status(cfg, ledger, logs_root)
    times = {r["task_id"]: (r["started_at"], r["updated_at"]) for r in ledger.all_tasks()}
    root = web_root(cfg)
    for t in st["tasks"]:
        t["started_at"], t["updated_at"] = times.get(t["task_id"], (None, None))
        t["url"] = f"{root}/tasks/{t['task_id']}" if root else ""
    st["tasks"].sort(key=lambda t: t["updated_at"] or "", reverse=True)
    return st


def read_board(cfg: Config, vk: Vikunja) -> dict:
    view = vk.kanban_view_id(cfg.project_id)
    root = web_root(cfg)
    order = [n.strip().lower() for n in cfg.bucket_names.values()]
    buckets = []
    for b in vk.board(cfg.project_id, view):
        tasks = [{"id": t["id"], "title": t.get("title", ""), "done": bool(t.get("done")),
                  "labels": sorted(labels_of(t)), "tracker": is_tracker(t),
                  "updated": t.get("updated") or "", "url": f"{root}/tasks/{t['id']}"}
                 for t in b.get("tasks") or []]
        tasks.sort(key=lambda t: t["updated"], reverse=True)
        buckets.append({"title": b.get("title", ""), "tasks": tasks})
    rank = {name: i for i, name in enumerate(order)}
    buckets.sort(key=lambda b: rank.get(b["title"].strip().lower(), len(rank)))
    return {"buckets": buckets, "releases": [t for b in buckets for t in b["tasks"] if t["tracker"]]}


def read_status(name: str, repo_dir: str, *, run=subprocess.run, timeout: int = STATUS_TIMEOUT_SEC) -> dict:
    """`uv run <cli> status --json` in `repo_dir`, checked against schema 1. Raises ValueError with
    a short reason the page can show."""
    path = Path(repo_dir).expanduser()
    if not path.is_dir():
        raise ValueError(f"{path} is not a directory on this host")
    try:
        r = run(["uv", "run", "--quiet", CLIS[name], "status", "--json"], cwd=path,
                capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as e:
        raise ValueError(f"`{CLIS[name]} status --json` took longer than {timeout}s") from e
    except OSError as e:
        raise ValueError(f"could not run uv: {e}") from e
    if r.returncode != 0:
        tail = (r.stderr or r.stdout or "").strip().splitlines()[-1:] or [""]
        raise ValueError(f"`{CLIS[name]} status --json` exited {r.returncode}: {tail[0][:200]}")
    try:
        data = json.loads(r.stdout)
    except json.JSONDecodeError as e:
        raise ValueError(f"`{CLIS[name]} status --json` did not print JSON") from e
    if not isinstance(data, dict) or data.get("schema") != SCHEMA:
        raise ValueError(f"unexpected status schema {data.get('schema') if isinstance(data, dict) else '?'};"
                         f" this dashboard reads schema {SCHEMA}")
    return data


# --- caching --------------------------------------------------------------------------------------
class Sources:
    """The sources behind one TTL cache. The readers are injectable for tests."""

    NAMES = ("pipeline", "board", "planner", "explorer", "services")

    def __init__(self, cfg: Config, *, ttl: float | None = None, clock: Callable[[], float] = time.monotonic,
                 readers: dict[str, Callable[[], dict]] | None = None):
        self.cfg, self.ttl, self.clock = cfg, float(ttl if ttl is not None else cfg.dashboard_refresh_sec), clock
        self._cache: dict[str, tuple[float, Result]] = {}
        self._locks = {n: threading.Lock() for n in self.NAMES}
        self._readers = readers if readers is not None else self._default_readers()

    def _default_readers(self) -> dict[str, Callable[[], dict] | None]:
        cfg = self.cfg
        def pipeline() -> dict:
            ledger = Ledger(cfg.db_path)
            try:
                return read_pipeline(cfg, ledger)
            finally:
                ledger.close()

        return {
            "pipeline": pipeline,
            "board": (lambda: read_board(cfg, Vikunja(cfg.vikunja_url, cfg.vikunja_token)))
            if cfg.vikunja_url and cfg.project_id else None,
            "planner": (lambda: read_status("planner", cfg.dashboard_planner_dir))
            if cfg.dashboard_planner_dir else None,
            "explorer": (lambda: read_status("explorer", cfg.dashboard_explorer_dir))
            if cfg.dashboard_explorer_dir else None,
            "services": (lambda: read_services(list(cfg.dashboard_units))) if cfg.dashboard_units else None,
        }

    def get(self, name: str) -> Result:
        if name not in self.NAMES:
            raise KeyError(name)
        with self._locks[name]:                 # one read per source at a time; others wait for it
            hit = self._cache.get(name)
            if hit and self.clock() - hit[0] < self.ttl:
                return hit[1]
            res = self._read(name)
            self._cache[name] = (self.clock(), res)
            return res

    def fresh(self, name: str) -> Result:
        """Read a source now, ignoring (and refreshing) the cache; for checks before an action."""
        if name not in self.NAMES:
            raise KeyError(name)
        with self._locks[name]:
            res = self._read(name)
            self._cache[name] = (self.clock(), res)
            return res

    def all(self) -> dict[str, Result]:
        return {n: self.get(n) for n in self.NAMES}

    def _read(self, name: str) -> Result:
        reader = self._readers.get(name)
        if reader is None:
            return Result(name, configured=False)
        try:
            return Result(name, data=reader())
        except Exception as e:  # noqa: BLE001 - a broken source becomes an error card, never a 500
            log.warning("dashboard source %s failed: %s", name, e)
            return Result(name, error=str(e)[:300] or e.__class__.__name__)
