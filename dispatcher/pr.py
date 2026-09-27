"""Push the branch and open the PR. The dispatcher does this; the worker never does."""

from __future__ import annotations

import subprocess
from pathlib import Path

from .config import Config
from .log import get
from .prompt import acceptance_criteria

log = get("pr")


def _git(worktree: Path, *args: str, timeout: int = 300) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=worktree, capture_output=True, text=True, timeout=timeout
    )


def _cap_words(text: str, n: int = 300) -> str:
    words = (text or "").split()
    return " ".join(words[:n]) + (" …" if len(words) > n else "")


def build_body(
    *,
    cfg: Config,
    task_id: int,
    task_title: str,
    description_html: str,
    worker_summary: str,
    review: dict,
    session_id: str,
    cost_usd: float,
) -> str:
    vikunja_link = f"{cfg.vikunja_url.rsplit('/api/', 1)[0]}/tasks/{task_id}"
    criteria = acceptance_criteria(description_html)
    passed = review.get("verdict") == "pass"
    checklist = "\n".join(f"- [{'x' if passed else ' '}] {c}" for c in criteria) or "- (none listed)"

    findings = review.get("findings", [])
    if findings:
        notes = "\n".join(
            f"- **{f.get('severity', '?')}** `{f.get('file', '?')}:{f.get('line', '?')}` — {f.get('note', '')}"
            for f in findings
        )
    else:
        notes = "_No blocking findings._"

    return f"""## Vikunja task
[{task_title}]({vikunja_link}) · task-{task_id}

## Acceptance criteria
{checklist}

## Worker summary
{_cap_words(worker_summary)}

## Review notes
Verdict: **{review.get('verdict', 'n/a')}**

{notes}

---
_Session `{session_id}` · cost ${cost_usd:.2f} · opened by claude-pipeline._
"""


def _ensure_label(cfg: Config, name: str, color: str, desc: str) -> None:
    """Create a GitHub label if it doesn't already exist (idempotent, best-effort)."""
    subprocess.run(
        ["gh", "label", "create", name, "--color", color, "--description", desc, "--force"],
        cwd=cfg.repo_path,
        capture_output=True,
        text=True,
        timeout=60,
    )


def push_and_create(
    *,
    cfg: Config,
    worktree: Path,
    branch: str,
    title: str,
    body: str,
    needs_qa: bool = False,
) -> str:
    """Push the branch and open a PR. Returns the PR URL."""
    push = _git(worktree, "push", "-u", "origin", branch)
    if push.returncode != 0:
        raise RuntimeError(f"git push failed: {push.stderr.strip()}")

    body_file = worktree / "pr-body.md"
    body_file.write_text(body)

    # `gh pr create --label` fails if the label doesn't exist in the repo; ensure it.
    _ensure_label(cfg, cfg.pr_label, "5319e7", "Opened by claude-pipeline")
    if needs_qa:
        _ensure_label(cfg, "needs-qa", "d93f0b", "Needs manual QA")

    args = [
        "gh", "pr", "create",
        "--base", cfg.base_branch,
        "--head", branch,
        "--title", title,
        "--body-file", str(body_file),
        "--label", cfg.pr_label,
    ]
    if needs_qa:
        args += ["--label", "needs-qa"]

    cp = subprocess.run(args, cwd=worktree, capture_output=True, text=True, timeout=300)
    body_file.unlink(missing_ok=True)
    if cp.returncode != 0:
        raise RuntimeError(f"gh pr create failed: {cp.stderr.strip() or cp.stdout.strip()}")
    return cp.stdout.strip().splitlines()[-1]


def open_worker_prs(cfg: Config) -> list[dict]:
    """List open PRs with the pipeline's label. Used by the cleanup poller."""
    cp = subprocess.run(
        [
            "gh", "pr", "list",
            "--label", cfg.pr_label,
            "--state", "all",
            "--json", "number,state,headRefName,url,mergedAt,mergeCommit",
            "--limit", "100",
        ],
        cwd=cfg.repo_path,
        capture_output=True,
        text=True,
        timeout=120,
    )
    if cp.returncode != 0:
        log.warning("gh pr list failed: %s", cp.stderr.strip())
        return []
    import json

    try:
        return json.loads(cp.stdout or "[]")
    except json.JSONDecodeError:
        return []
