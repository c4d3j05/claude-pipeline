"""Cleanup: tear down a task's resources when its PR closes, plus the nightly pass."""

from __future__ import annotations

import re
import subprocess

from .config import Config
from .db import Ledger
from .log import get
from .notify import Slack
from .ntree import Ntree
from .pr import open_worker_prs

log = get("cleanup")

_BRANCH_RE = re.compile(r"task-(\d+)-")


def task_id_from_branch(branch: str) -> int | None:
    m = _BRANCH_RE.match(branch or "")
    return int(m.group(1)) if m else None


def _dropdb(slug: str) -> None:
    """Best-effort drop of the per-workspace database (spec: dropdb app_<slug>)."""
    db = f"app_{slug.replace('-', '_')}"
    cp = subprocess.run(["dropdb", "--if-exists", db], capture_output=True, text=True)
    if cp.returncode != 0:
        log.debug("dropdb %s: %s", db, cp.stderr.strip())


def process_closed_prs(cfg: Config, ledger: Ledger, ntree: Ntree, vikunja, buckets, slack: Slack) -> None:
    """Poll worker PRs; for any that closed, tear down and update Vikunja.

    `buckets` is the {ready/in_progress/review/done/blocked -> bucket dict} map,
    `vikunja` is the Vikunja client. `view_id` comes from buckets via caller.
    """
    for pr in open_worker_prs(cfg):
        if pr.get("state") not in ("MERGED", "CLOSED"):
            continue
        branch = pr.get("headRefName", "")
        task_id = task_id_from_branch(branch)
        if task_id is None:
            continue
        row = ledger.get(task_id)
        if row is None or row["state"] in ("done", "blocked"):
            continue  # already handled

        merged = pr.get("state") == "MERGED"
        slug = row["slug"]
        log.info("PR %s for task %s %s — tearing down", pr.get("number"), task_id,
                 "merged" if merged else "closed unmerged")

        # 1. ntree rm --force (stops process, frees ports, removes worktree)
        ntree.rm(slug)
        # 2. drop the per-workspace database
        _dropdb(slug)
        # 3. delete local branch (remote deleted by GitHub auto-delete)
        subprocess.run(["git", "branch", "-D", branch], cwd=cfg.repo_path,
                       capture_output=True, text=True)
        # 4. Vikunja: move + comment
        target = buckets["done"] if merged else buckets["blocked"]
        try:
            vikunja.move_task(cfg.project_id, target["project_view_id"], target["id"], task_id)
            sha = pr.get("mergeCommit", {}).get("oid", "") if isinstance(pr.get("mergeCommit"), dict) else ""
            note = f"merged: {sha}" if merged else "closed without merge"
            vikunja.comment(task_id, f"<p>{note} ({pr.get('url')})</p>")
        except Exception as e:  # noqa: BLE001
            log.warning("vikunja update on close failed for task %s: %s", task_id, e)

        ledger.update(task_id, state="done" if merged else "blocked", pr_url=pr.get("url"))


def nightly(cfg: Config, ntree: Ntree, slack: Slack) -> str:
    """ntree doctor + report. Returns a human summary (also sent to Slack by caller)."""
    lines: list[str] = []
    doc = ntree.doctor()
    lines.append("ntree doctor: " + ("ok" if doc.returncode == 0 else "issues"))
    if doc.stdout.strip():
        lines.append(doc.stdout.strip()[-500:])
    summary = "\n".join(lines)
    log.info("nightly summary:\n%s", summary)
    return summary
