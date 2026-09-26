"""Configuration, loaded from the environment (optionally seeded from a .env file)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _load_dotenv(path: Path) -> None:
    """Minimal .env loader: KEY=VALUE lines, no export, no interpolation.

    Existing environment variables win, so `VAR=x claude-pipeline` still overrides.
    """
    if not path.exists():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default


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

    # Workers
    max_workers: int
    poll_interval: int
    max_turns: int
    wall_clock_limit_min: int
    max_test_retries: int
    max_review_retries: int
    worker_model: str
    review_model: str
    permission_mode: str

    # Budget
    daily_cap_usd: float

    # Notifications / paths
    slack_webhook: str
    db_path: Path
    log_level: str

    # Derived at runtime
    settings_file: str = ".claude/worker-settings.json"

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
    if env_file:
        _load_dotenv(Path(env_file))

    repo = os.environ.get("TARGET_REPO_PATH", "").strip()
    return Config(
        vikunja_url=os.environ.get("VIKUNJA_URL", "").rstrip("/"),
        vikunja_token=os.environ.get("VIKUNJA_API_TOKEN", "").strip(),
        project_id=_int("VIKUNJA_PROJECT_ID", 0),
        claude_label=os.environ.get("CLAUDE_LABEL", "claude").strip(),
        bucket_ready=os.environ.get("BUCKET_READY", "Ready").strip(),
        bucket_in_progress=os.environ.get("BUCKET_IN_PROGRESS", "In Progress").strip(),
        bucket_review=os.environ.get("BUCKET_REVIEW", "Review").strip(),
        bucket_done=os.environ.get("BUCKET_DONE", "Done").strip(),
        bucket_blocked=os.environ.get("BUCKET_BLOCKED", "Blocked").strip(),
        repo_path=Path(repo).expanduser() if repo else Path.cwd(),
        base_branch=os.environ.get("BASE_BRANCH", "main").strip(),
        pr_label=os.environ.get("PR_LABEL", "claude-worker").strip(),
        test_cmd=os.environ.get("TEST_CMD", "uv run pytest -x -q").strip(),
        max_workers=_int("MAX_WORKERS", 4),
        poll_interval=_int("POLL_INTERVAL", 60),
        max_turns=_int("MAX_TURNS", 60),
        wall_clock_limit_min=_int("WALL_CLOCK_LIMIT_MIN", 45),
        max_test_retries=_int("MAX_TEST_RETRIES", 2),
        max_review_retries=_int("MAX_REVIEW_RETRIES", 1),
        worker_model=os.environ.get("WORKER_MODEL", "").strip(),
        review_model=os.environ.get("REVIEW_MODEL", "").strip(),
        permission_mode=os.environ.get("PERMISSION_MODE", "acceptEdits").strip(),
        daily_cap_usd=_float("DAILY_CAP_USD", 50.0),
        slack_webhook=os.environ.get("SLACK_WEBHOOK_URL", "").strip(),
        db_path=Path(os.environ.get("PIPELINE_DB", "pipeline.db")).expanduser(),
        log_level=os.environ.get("LOG_LEVEL", "INFO").strip().upper(),
    )


def validate(cfg: Config) -> list[str]:
    """Return a list of human-readable configuration problems (empty == ok)."""
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
