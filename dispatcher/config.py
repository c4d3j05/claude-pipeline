"""Configuration, loaded from a .env file (falling back to the environment).

`load()` re-reads the file every call, with **file values winning**, so the dashboard
can edit .env and the daemon picks the change up on its next tick (hot-reload).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _read_dotenv(path: Path) -> dict[str, str]:
    vals: dict[str, str] = {}
    if not path.exists():
        return vals
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        vals[key.strip()] = value.strip().strip('"').strip("'")
    return vals


def _src(env_file: str | None) -> dict[str, str]:
    """Merged config source: env-file values win over the process environment."""
    fv = _read_dotenv(Path(env_file)) if env_file else {}
    return {**os.environ, **fv}


def _int(src: dict, name: str, default: int) -> int:
    try:
        return int((src.get(name, "") or "").strip() or default)
    except ValueError:
        return default


def _float(src: dict, name: str, default: float) -> float:
    try:
        return float((src.get(name, "") or "").strip() or default)
    except ValueError:
        return default


def _str(src: dict, name: str, default: str = "") -> str:
    return (src.get(name, default) or default).strip()


@dataclass(frozen=True)
class Config:
    # Vikunja
    vikunja_url: str
    vikunja_token: str
    project_id: int
    claude_label: str
    bucket_ready: str
    bucket_in_progress: str
    bucket_review: str
    bucket_done: str
    bucket_blocked: str

    # Target repo
    repo_path: Path
    base_branch: str
    pr_label: str
    test_cmd: str

    # Workers (hot-reloadable)
    max_workers: int
    poll_interval: int
    max_turns: int
    wall_clock_limit_min: int
    max_test_retries: int
    max_review_retries: int
    worker_model: str
    review_model: str
    permission_mode: str

    # Budget (hot-reloadable)
    daily_cap_usd: float

    # Notifications / paths
    slack_webhook: str
    db_path: Path
    log_level: str

    settings_file: str = ".claude/worker-settings.json"

    # Knobs the dashboard may edit and the daemon hot-reloads each tick.
    HOT_KEYS = (
        "MAX_WORKERS", "POLL_INTERVAL", "DAILY_CAP_USD",
        "WORKER_MODEL", "REVIEW_MODEL", "PERMISSION_MODE",
        "MAX_TURNS", "WALL_CLOCK_LIMIT_MIN", "MAX_TEST_RETRIES", "MAX_REVIEW_RETRIES",
    )

    @property
    def bucket_names(self) -> dict[str, str]:
        return {
            "ready": self.bucket_ready,
            "in_progress": self.bucket_in_progress,
            "review": self.bucket_review,
            "done": self.bucket_done,
            "blocked": self.bucket_blocked,
        }


def load(env_file: str | None = ".env") -> Config:
    src = _src(env_file)
    repo = _str(src, "TARGET_REPO_PATH")
    return Config(
        vikunja_url=_str(src, "VIKUNJA_URL").rstrip("/"),
        vikunja_token=_str(src, "VIKUNJA_API_TOKEN"),
        project_id=_int(src, "VIKUNJA_PROJECT_ID", 0),
        claude_label=_str(src, "CLAUDE_LABEL", "claude"),
        bucket_ready=_str(src, "BUCKET_READY", "Ready"),
        bucket_in_progress=_str(src, "BUCKET_IN_PROGRESS", "In Progress"),
        bucket_review=_str(src, "BUCKET_REVIEW", "Review"),
        bucket_done=_str(src, "BUCKET_DONE", "Done"),
        bucket_blocked=_str(src, "BUCKET_BLOCKED", "Blocked"),
        repo_path=Path(repo).expanduser() if repo else Path.cwd(),
        base_branch=_str(src, "BASE_BRANCH", "main"),
        pr_label=_str(src, "PR_LABEL", "claude-worker"),
        test_cmd=_str(src, "TEST_CMD", "uv run pytest -x -q"),
        max_workers=_int(src, "MAX_WORKERS", 4),
        poll_interval=_int(src, "POLL_INTERVAL", 60),
        max_turns=_int(src, "MAX_TURNS", 60),
        wall_clock_limit_min=_int(src, "WALL_CLOCK_LIMIT_MIN", 45),
        max_test_retries=_int(src, "MAX_TEST_RETRIES", 2),
        max_review_retries=_int(src, "MAX_REVIEW_RETRIES", 1),
        worker_model=_str(src, "WORKER_MODEL"),
        review_model=_str(src, "REVIEW_MODEL"),
        permission_mode=_str(src, "PERMISSION_MODE", "acceptEdits"),
        daily_cap_usd=_float(src, "DAILY_CAP_USD", 50.0),
        slack_webhook=_str(src, "SLACK_WEBHOOK_URL"),
        db_path=Path(_str(src, "PIPELINE_DB", "pipeline.db")).expanduser(),
        log_level=_str(src, "LOG_LEVEL", "INFO").upper(),
    )


def set_values(env_file: str, updates: dict[str, str]) -> None:
    """Rewrite the given keys in the .env file in place (used by the dashboard).

    Preserves comments/order; appends any key not already present.
    """
    path = Path(env_file)
    lines = path.read_text().splitlines() if path.exists() else []
    remaining = dict(updates)
    out: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = stripped.split("=", 1)[0].strip()
            if key in remaining:
                out.append(f"{key}={remaining.pop(key)}")
                continue
        out.append(line)
    for key, value in remaining.items():
        out.append(f"{key}={value}")
    path.write_text("\n".join(out) + "\n")


def validate(cfg: Config) -> list[str]:
    problems: list[str] = []
    if not cfg.vikunja_url:
        problems.append("VIKUNJA_URL is not set")
    if not cfg.vikunja_token:
        problems.append("VIKUNJA_API_TOKEN is not set")
    if not cfg.project_id:
        problems.append("VIKUNJA_PROJECT_ID is not set")
    if not cfg.repo_path.exists():
        problems.append(f"TARGET_REPO_PATH does not exist: {cfg.repo_path}")
    elif not (cfg.repo_path / ".git").exists():
        problems.append(f"TARGET_REPO_PATH is not a git repo: {cfg.repo_path}")
    return problems
