"""The verification gate: independent tests, then an adversarial review session."""

from __future__ import annotations

import json
import shlex
import subprocess
from pathlib import Path

from .config import Config
from .log import get
from .ntree import Ntree
from .prompt import review_prompt
from .worker import launch

log = get("review")


def run_tests(cfg: Config, ntree: Ntree, slug: str) -> tuple[bool, str]:
    """Run the gate's test command inside the worktree. Returns (green, tail)."""
    cp = ntree.run(slug, shlex.split(cfg.test_cmd), timeout=cfg.wall_clock_limit_min * 60)
    output = (cp.stdout or "") + (cp.stderr or "")
    tail = "\n".join(output.splitlines()[-40:])
    return cp.returncode == 0, tail


def working_tree_dirty(worktree: Path) -> bool:
    cp = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=worktree,
        capture_output=True,
        text=True,
    )
    return bool(cp.stdout.strip())


MAX_DIFF_CHARS = 200_000   # prompt goes via stdin, so this is a cost limit, not an argv limit


def diff_against_base(worktree: Path, base: str) -> str:
    cp = subprocess.run(
        ["git", "diff", f"{base}...HEAD"],
        cwd=worktree,
        capture_output=True,
        text=True,
    )
    return cp.stdout


def review_settings(cfg: Config, worktree: Path, session_log: Path) -> str:
    """Settings for the reviewer: the repo's worker settings minus hooks.

    The reviewer is read-only; the Stop hook would run the whole test gate every time the
    reviewer stops. Returns an absolute path (the original is relative to the worktree).
    """
    src = worktree / cfg.settings_file
    try:
        data = json.loads(src.read_text())
    except (OSError, ValueError):
        return cfg.settings_file  # unreadable: fall back, the hook failing is harmless
    data.pop("hooks", None)
    dest = session_log.parent / "review-settings.json"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(data, indent=2))
    return str(dest.resolve())


def adversarial_review(
    cfg: Config, worktree: Path, spec: str, session_log: Path, base_ref: str | None = None
) -> dict:
    """Run a read-only reviewer session; return the verdict dict.

    Keys: verdict ("pass"|"block"), findings, cost_usd (so the caller can account it),
    truncated (the diff exceeded MAX_DIFF_CHARS), error (the reviewer session itself
    failed — distinct from a real "block", and not the worker's fault).
    """
    diff = diff_against_base(worktree, base_ref or cfg.base_branch)
    if not diff.strip():
        return {"verdict": "block", "cost_usd": 0.0, "findings": [
            {"severity": "high", "file": "-", "line": 0,
             "note": "empty diff against base — nothing was changed"}]}

    truncated = len(diff) > MAX_DIFF_CHARS
    shown = diff[:MAX_DIFF_CHARS]
    if truncated:
        shown += f"\n... [diff truncated: showing {MAX_DIFF_CHARS} of {len(diff)} chars]"
    result, rc = launch(
        worktree=worktree,
        prompt=review_prompt(spec, shown),
        settings_file=review_settings(cfg, worktree, session_log),
        session_log=session_log,
        max_turns=8,
        wall_clock_limit_min=15,
        model=cfg.review_model,
        permission_mode="plan",  # read-only
        read_only=True,
    )
    cost = result.cost_usd
    verdict = _extract_json(result.result_text or result.last_message)
    if verdict is None:
        failed = rc != 0 or result.is_error
        log.warning("review produced no parseable JSON (rc=%s); treating as block", rc)
        return {"verdict": "block", "cost_usd": cost, "error": failed, "findings": [
            {"severity": "medium", "file": "-", "line": 0,
             "note": f"reviewer session failed (rc={rc})" if failed
             else "reviewer returned no valid JSON verdict"}]}
    verdict.setdefault("findings", [])
    if verdict.get("verdict") not in ("pass", "block"):
        verdict["verdict"] = "block"   # anything unrecognised is not a pass
    verdict["cost_usd"] = cost
    verdict["truncated"] = truncated
    if truncated and verdict["verdict"] == "pass":
        verdict["findings"].append({
            "severity": "medium", "file": "-", "line": 0,
            "note": f"reviewer saw only the first {MAX_DIFF_CHARS} of {len(diff)} diff chars"})
    return verdict


def _extract_json(text: str) -> dict | None:
    if not text:
        return None
    text = text.strip()
    # Strip code fences if present.
    if text.startswith("```"):
        text = text.split("```", 2)[1]
        text = text.removeprefix("json")
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1:
        return None
    try:
        return json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
