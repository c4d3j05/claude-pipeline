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


def diff_against_base(worktree: Path, base: str) -> str:
    cp = subprocess.run(
        ["git", "diff", f"{base}...HEAD"],
        cwd=worktree,
        capture_output=True,
        text=True,
    )
    return cp.stdout


def adversarial_review(
    cfg: Config, worktree: Path, spec: str, session_log: Path
) -> dict:
    """Run a read-only reviewer session; return {"verdict": ..., "findings": [...]}."""
    diff = diff_against_base(worktree, cfg.base_branch)
    if not diff.strip():
        return {"verdict": "block", "findings": [
            {"severity": "high", "file": "-", "line": 0,
             "note": "empty diff against base — nothing was changed"}]}

    prompt = review_prompt(spec, diff[:60000])
    result, _rc = launch(
        worktree=worktree,
        prompt=prompt,
        settings_file=cfg.settings_file,
        session_log=session_log,
        max_turns=8,
        wall_clock_limit_min=15,
        model=cfg.review_model,
        permission_mode="plan",  # read-only
        read_only=True,
    )
    verdict = _extract_json(result.result_text or result.last_message)
    if verdict is None:
        log.warning("review produced no parseable JSON; treating as block")
        return {"verdict": "block", "findings": [
            {"severity": "medium", "file": "-", "line": 0,
             "note": "reviewer returned no valid JSON verdict"}]}
    verdict.setdefault("findings", [])
    verdict.setdefault("verdict", "block")
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
