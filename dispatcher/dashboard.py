"""Terminal control panel for the pipeline.

`gather_status()` reads the same SQLite ledger the daemon writes (plus each worker's
stream log), so status is always accurate. Controls go through the ledger's `control`
row / `cancel_requested` flag, which the daemon honors each tick — the TUI never
touches the daemon's threads directly.

Textual is imported lazily so `dashboard --once` and the tests work without it.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import json
import subprocess
from pathlib import Path

from .config import Config
from .db import ACTIVE_STATES, Ledger
from .vikunja import Vikunja

_PIPELINE_PREFIXES = ("dispatched:", "gate:", "blocked:", "pr:", "cap:")


# --- data gathering -----------------------------------------------------
def _elapsed(started_at: str | None) -> str:
    if not started_at:
        return "-"
    try:
        start = dt.datetime.fromisoformat(started_at)
    except ValueError:
        return "-"
    secs = int((dt.datetime.now() - start).total_seconds())
    if secs < 0:
        return "-"
    h, rem = divmod(secs, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def _last_message(logs_root: Path, slug: str) -> str:
    path = logs_root / slug / "session.jsonl"
    if not path.exists():
        return ""
    last = ""
    try:
        lines = path.read_text(errors="ignore").splitlines()[-150:]
    except OSError:
        return ""
    for ln in lines:
        try:
            ev = json.loads(ln)
        except json.JSONDecodeError:
            continue
        if ev.get("type") == "assistant":
            for b in ev.get("message", {}).get("content", []) or []:
                if isinstance(b, dict) and b.get("type") == "text" and b.get("text"):
                    last = b["text"].strip().replace("\n", " ")
        elif ev.get("type") == "result" and ev.get("result"):
            last = "✓ " + str(ev["result"]).strip().replace("\n", " ")
    return last[:80]


def gather_status(cfg: Config, ledger: Ledger, logs_root: Path = Path("logs")) -> dict:
    tasks = []
    active = 0
    for row in ledger.all_tasks():
        d = dict(row)
        if d["state"] in ACTIVE_STATES:
            active += 1
        tasks.append({
            "task_id": d["task_id"],
            "slug": d["slug"],
            "state": d["state"],
            "cost": d["cost_usd"],
            "retries": f"{d['test_retries']}/{d['review_retries']}",
            "pr_url": d["pr_url"] or "",
            "elapsed": _elapsed(d["started_at"]) if d["state"] in ACTIVE_STATES else "-",
            "cancel": bool(d["cancel_requested"]),
            "activity": _last_message(logs_root, d["slug"]) if d["state"] in ACTIVE_STATES else "",
        })
    return {
        "paused": ledger.is_paused(),
        "today_cost": ledger.today_cost(),
        "cap": cfg.daily_cap_usd,
        "max_workers": cfg.max_workers,
        "active": active,
        "tasks": tasks,
    }


def snapshot_text(cfg: Config, ledger: Ledger, logs_root: Path = Path("logs")) -> str:
    """Plain-text one-shot status (used by `dashboard --once`; no Textual needed)."""
    st = gather_status(cfg, ledger, logs_root)
    header = (
        f"workers {st['active']}/{st['max_workers']}  |  "
        f"today ${st['today_cost']:.2f}/${st['cap']:.0f}  |  "
        f"{'PAUSED' if st['paused'] else 'running'}"
    )
    lines = [header, "-" * len(header)]
    if not st["tasks"]:
        lines.append("(no tasks yet)")
    for t in st["tasks"]:
        pr = t["pr_url"].rsplit("/", 1)[-1] if t["pr_url"] else ""
        lines.append(
            f"{t['task_id']:>5}  {t['state']:<10} {t['elapsed']:>8}  ${t['cost']:>5.2f}  "
            f"r{t['retries']:<5} {('#'+pr) if pr else '':<6} {t['activity']}"
        )
    return "\n".join(lines)


# --- control operations -------------------------------------------------
def toggle_pause(ledger: Ledger) -> bool:
    new = not ledger.is_paused()
    ledger.set_paused(new)
    return new


def request_cancel(ledger: Ledger, task_id: int) -> None:
    ledger.request_cancel(task_id)


def open_pr(pr_url: str) -> None:
    if pr_url:
        subprocess.run(["open", pr_url], capture_output=True)


def retry_task(cfg: Config, ledger: Ledger, task_id: int) -> str:
    """Reset a blocked task so the dispatcher re-claims it: drop the pipeline's
    comments (so the re-claim guard clears), move it back to Ready, clear its row."""
    vk = Vikunja(cfg.vikunja_url, cfg.vikunja_token)
    view = vk.kanban_view_id(cfg.project_id)
    bmap = vk.bucket_map(cfg.project_id, view)
    ready = bmap.get(cfg.bucket_ready.strip().lower())
    if not ready:
        return "no Ready bucket"
    for c in vk.comments(task_id):
        body = (c.get("comment") or "").lower().replace("<p>", "")
        if any(body.startswith(p) for p in _PIPELINE_PREFIXES):
            with contextlib.suppress(Exception):
                vk.delete_comment(task_id, c["id"])
    vk.move_task(cfg.project_id, view, ready["id"], task_id)
    ledger.delete_task(task_id)
    return "re-queued"


def set_config(env_file: str, updates: dict[str, str]) -> None:
    from . import config as cfgmod
    cfgmod.set_values(env_file, updates)


# --- Textual TUI --------------------------------------------------------
def run(env_file: str = ".env") -> int:
    try:
        from .tui import DashboardApp
    except ModuleNotFoundError as e:
        if "textual" in str(e):
            print("The dashboard needs Textual. Install it with:\n"
                  "  uv sync --group dashboard\nOr use `claude-pipeline dashboard --once`.")
            return 1
        raise
    from . import config as cfgmod

    cfg = cfgmod.load(env_file)
    DashboardApp(cfg, env_file).run()
    return 0
