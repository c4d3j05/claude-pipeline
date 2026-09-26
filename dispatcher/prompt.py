"""Slug derivation and worker/review prompt construction."""

from __future__ import annotations

import html
import re

MAX_SLUG_WORDS = 5


def _strip_html(text: str) -> str:
    text = re.sub(r"<br\s*/?>", "\n", text or "", flags=re.IGNORECASE)
    text = re.sub(r"</(p|li|h[1-6]|ul|ol|div)>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<li[^>]*>", "- ", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    return html.unescape(text).strip()


def slugify(title: str) -> str:
    words = re.sub(r"[^a-z0-9\s-]", "", title.lower()).split()
    return "-".join(words[:MAX_SLUG_WORDS]) or "task"


def task_slug(task_id: int, title: str) -> str:
    """`task-123-short-title` — the branch name and workspace name."""
    return f"task-{task_id}-{slugify(title)}"


def acceptance_criteria(description_html: str) -> list[str]:
    """Best-effort pull of the Definition of Done / acceptance checklist items."""
    text = _strip_html(description_html)
    lines = [ln.strip("-• \t") for ln in text.splitlines()]
    # Prefer items under a "Definition of Done" / "Acceptance" heading.
    items: list[str] = []
    capture = False
    for ln in lines:
        low = ln.lower()
        if any(h in low for h in ("definition of done", "acceptance criteria")):
            capture = True
            continue
        if capture:
            if not ln:
                continue
            if ln.endswith(":") and len(ln) < 40:  # a new heading ends the section
                break
            items.append(ln)
    return [i for i in items if i]


PREAMBLE = """\
You are an autonomous worker in an unattended pipeline. No human is watching this
session, so never wait for input — if you cannot proceed, stop as described below.

Workspace:
- Branch name and workspace slug: {slug}
- You are already inside the git worktree for this branch.
- NTREE_WORKSPACE and $PORT exist in the environment; use them for anything that
  binds a port or names a database/queue. Never hardcode ports or DB names.

Rules:
- Commit as you go. Do NOT push. Do NOT open a PR — the pipeline does that.
- Do not modify migrations that already exist. Do not delete tests.
- Do not touch .env*, secrets/, infra/, or the pipeline's own .claude/ files.
- If blocked, write "BLOCKED: <reason>" as your final message and stop. Never guess
  at a missing requirement.

Definition of done: tests pass, no new lint errors, every acceptance criterion met,
all changes committed (nothing left uncommitted, nothing pushed).
"""


def worker_prompt(task_id: int, title: str, description_html: str, slug: str) -> str:
    body = _strip_html(description_html)
    criteria = acceptance_criteria(description_html)
    checklist = (
        "\n".join(f"- [ ] {c}" for c in criteria)
        if criteria
        else "(no explicit acceptance criteria — infer from the description)"
    )
    return (
        PREAMBLE.format(slug=slug)
        + f"\n\n# Task {task_id}: {title}\n\n{body}\n\n"
        + f"## Acceptance criteria (copied verbatim)\n{checklist}\n"
    )


def retry_prompt_tests(tail: str) -> str:
    return (
        "The test gate is still red. Fix the failures, commit, and finish.\n\n"
        f"Last test output:\n```\n{tail}\n```"
    )


def retry_prompt_review(findings: list[dict]) -> str:
    lines = [
        f"- [{f.get('severity', '?')}] {f.get('file', '?')}:{f.get('line', '?')} — {f.get('note', '')}"
        for f in findings
    ]
    return (
        "The review gate blocked this change. Address every finding below, commit, "
        "and finish. Do not push or open a PR.\n\n" + "\n".join(lines)
    )


def commit_or_discard_prompt() -> str:
    return (
        "You have uncommitted changes but the session ended. Either commit them with a "
        "clear message or discard them, then finish. Do not push."
    )


REVIEW_INSTRUCTION = """\
You are a code reviewer. You did NOT write this code. Review it read-only.

Find reasons this should NOT merge. Judge ONLY correctness, security, or a missed
acceptance criterion. Style is a note, not a block.

Task description and acceptance criteria:
{spec}

Diff under review:
```diff
{diff}
```

Output ONLY JSON matching the schema: {{"verdict": "pass"|"block",
"findings": [{{"severity": "...", "file": "...", "line": 0, "note": "..."}}]}}.
Block only on correctness, security, or a missed acceptance criterion.
"""


def review_prompt(spec: str, diff: str) -> str:
    return REVIEW_INSTRUCTION.format(spec=spec, diff=diff)
