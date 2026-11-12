"""Push the branch and open the PR. The dispatcher does this; the worker never does."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

from .config import Config
from .log import get
from .procenv import clean_env
from .prompt import acceptance_criteria

log = get("pr")


def _git(worktree: Path, *args: str, timeout: int = 300) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=worktree, capture_output=True, text=True, timeout=timeout,
        env=clean_env("github"),
    )


def changed_files(worktree: Path, base_ref: str) -> list[str]:
    """The files the branch changed against its base, as repository-relative paths."""
    r = _git(worktree, "diff", "--name-only", f"{base_ref}...HEAD")
    return [ln for ln in r.stdout.splitlines() if ln.strip()] if r.returncode == 0 else []


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

    warn = ""
    if review.get("truncated"):
        warn = "\n> ⚠️ The diff exceeded the reviewer's size limit; part of it was **not** reviewed.\n"

    return f"""## Vikunja task
[{task_title}]({vikunja_link}) · task-{task_id}

## Acceptance criteria
{checklist}

## Worker summary
{_cap_words(worker_summary)}

## Review notes
Verdict: **{review.get('verdict', 'n/a')}**
{warn}
{notes}

---
_Session `{session_id}` · cost ${cost_usd:.2f} · opened by claude-pipeline._
"""


_PIPELINE_BRANCH_RE = re.compile(r"^task-\d+-")


def is_pipeline_branch(branch: str) -> bool:
    return bool(_PIPELINE_BRANCH_RE.match(branch or ""))


def _open_pr_url(cfg: Config, worktree: Path, branch: str) -> str:
    cp = subprocess.run(
        ["gh", "pr", "list", "--head", branch, "--state", "open", "--json", "url"],
        cwd=worktree, capture_output=True, text=True, timeout=120,
        env=clean_env("github"),
    )
    try:
        rows = json.loads(cp.stdout or "[]") if cp.returncode == 0 else []
    except json.JSONDecodeError:
        return ""
    return rows[0]["url"] if rows else ""


def _ensure_label(cfg: Config, name: str, color: str, desc: str) -> None:
    """Create a GitHub label if it doesn't already exist (idempotent, best-effort)."""
    subprocess.run(
        ["gh", "label", "create", name, "--color", color, "--description", desc, "--force"],
        cwd=cfg.repo_path,
        capture_output=True,
        text=True,
        timeout=60,
        env=clean_env("github"),
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
    # Pipeline branches (task-<id>-…) are rebuilt from scratch on every run, so a branch
    # left on the remote by an earlier attempt must be overwritten, or the push is
    # rejected as non-fast-forward and the task can never recover.
    push_args = ["push", "-u", "origin", branch]
    if is_pipeline_branch(branch):
        push_args.insert(1, "--force")
    push = _git(worktree, *push_args)
    if push.returncode != 0:
        raise RuntimeError(f"git push failed: {push.stderr.strip()}")

    existing = _open_pr_url(cfg, worktree, branch)
    if existing:  # an earlier attempt already opened the PR; the force-push updated it
        log.info("PR already open for %s: %s", branch, existing)
        return existing

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

    cp = subprocess.run(args, cwd=worktree, capture_output=True, text=True, timeout=300,
                        env=clean_env("github"))
    body_file.unlink(missing_ok=True)
    if cp.returncode != 0:
        raise RuntimeError(f"gh pr create failed: {cp.stderr.strip() or cp.stdout.strip()}")
    return cp.stdout.strip().splitlines()[-1]


# Fields every token can read. CI status (`statusCheckRollup`) needs extra permission
# (checks/statuses: read) that fine-grained tokens often lack; asking for it in the same
# call makes the WHOLE call fail, which would also break closed-PR cleanup.
_CORE_FIELDS = "number,state,headRefName,url,mergedAt,mergeCommit,mergeable,reviewDecision"
_checks_unavailable = False


def _gh_pr_list(cfg: Config, fields: str, state: str = "all") -> list[dict] | None:
    cp = subprocess.run(
        ["gh", "pr", "list", "--label", cfg.pr_label, "--state", state,
         "--json", fields, "--limit", "500"],
        cwd=cfg.repo_path, capture_output=True, text=True, timeout=120,
        env=clean_env("github"),
    )
    if cp.returncode != 0:
        return None if fields == _CORE_FIELDS else []
    try:
        return json.loads(cp.stdout or "[]")
    except json.JSONDecodeError:
        return None if fields == _CORE_FIELDS else []


def open_worker_prs(cfg: Config) -> list[dict]:
    """List the pipeline's PRs (all states). Used by cleanup and the PR watch.

    CI status is added best-effort in a second call, so a token that cannot read it still
    gets everything else (cleanup, conflicts, requested changes).
    """
    global _checks_unavailable
    rows = _gh_pr_list(cfg, _CORE_FIELDS)
    if rows is None:
        log.warning("gh pr list failed; treating as no PRs this round")
        return []
    if _checks_unavailable or not any(r.get("state") == "OPEN" for r in rows):
        return rows
    checks = _gh_pr_list(cfg, "number,statusCheckRollup", state="open")
    if not checks and any(r.get("state") == "OPEN" for r in rows):
        probe = subprocess.run(
            ["gh", "pr", "list", "--label", cfg.pr_label, "--state", "open",
             "--json", "number,statusCheckRollup", "--limit", "1"],
            cwd=cfg.repo_path, capture_output=True, text=True, timeout=60,
            env=clean_env("github"))
        if probe.returncode != 0:
            _checks_unavailable = True
            log.info("token cannot read CI status (statusCheckRollup); PR watch will report "
                     "conflicts and requested changes only")
    by_number = {c["number"]: c.get("statusCheckRollup") for c in checks or []}
    for r in rows:
        if r.get("number") in by_number:
            r["statusCheckRollup"] = by_number[r["number"]]
    return rows
