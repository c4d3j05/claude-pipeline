"""Environment for spawned subprocesses.

The dispatcher runs under its own virtualenv (e.g. `uv run`), which exports
VIRTUAL_ENV and puts its bin on PATH. That leaks into `claude`, `ntree`, and the
target repo's `poetry run`, making poetry bind to the WRONG interpreter (and fail a
`>=3.12` constraint if the pipeline runs on older Python). Strip it so subprocesses
resolve the target repo's own environment.
"""

from __future__ import annotations

import os
from pathlib import Path

# Per-session variables of an enclosing Claude Code session. If the dispatcher is started
# from inside one they leak into every worker (including a messaging socket/token), and a
# nested session can behave differently. Credentials (CLAUDE_CODE_OAUTH_TOKEN) are kept.
_SESSION_VARS = (
    "CLAUDECODE", "CLAUDE_PID", "CLAUDE_EFFORT", "CLAUDE_CODE_CHILD_SESSION",
    "CLAUDE_CODE_SESSION_ID", "CLAUDE_CODE_SESSION_ATTENDED", "CLAUDE_CODE_MESSAGING_SOCKET",
    "CLAUDE_CODE_MESSAGING_TOKEN", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_EXECPATH",
)


def worker_env_path() -> Path:
    return Path(os.environ.get("WORKER_ENV_FILE") or "~/.worker.env").expanduser()


def load_env_file(path: Path) -> dict[str, str]:
    """Parse a shell-style env file: `KEY=VALUE` / `export KEY=VALUE`, # comments, quotes.

    Read fresh on every call, so a rotated token is picked up by the next task — the same
    effect as sourcing the file before every task. No variable expansion is performed.
    """
    vals: dict[str, str] = {}
    try:
        text = path.read_text()
    except OSError:
        return vals
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, sep, value = line.partition("=")
        key = key.strip()
        if not sep or not key.isidentifier():
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        vals[key] = value
    return vals


def clean_env() -> dict[str, str]:
    env = dict(os.environ)
    for k in _SESSION_VARS:
        env.pop(k, None)
    env.update(load_env_file(worker_env_path()))  # credentials: CLAUDE_CODE_OAUTH_TOKEN, GH_TOKEN…
    venv = env.pop("VIRTUAL_ENV", None)
    env.pop("UV", None)
    env.pop("PYTHONHOME", None)
    if venv:
        bin_dir = os.path.join(venv, "bin")
        env["PATH"] = os.pathsep.join(
            p for p in env.get("PATH", "").split(os.pathsep) if p and p != bin_dir
        )
    return env


def with_git_config(env: dict[str, str], **pairs: str) -> dict[str, str]:
    """Return `env` with extra git config entries injected via GIT_CONFIG_COUNT/KEY/VALUE.

    Appends after any entries already present, so a user's own GIT_CONFIG_* survive.
    Config names use `__` for `.` (e.g. branch__autoSetupMerge) because kwargs can't hold dots.
    """
    out = dict(env)
    n = int(out.get("GIT_CONFIG_COUNT", "0") or 0)
    for key, value in pairs.items():
        out[f"GIT_CONFIG_KEY_{n}"] = key.replace("__", ".")
        out[f"GIT_CONFIG_VALUE_{n}"] = value
        n += 1
    out["GIT_CONFIG_COUNT"] = str(n)
    return out
