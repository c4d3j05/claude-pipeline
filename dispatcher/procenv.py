"""Environment for spawned subprocesses.

The dispatcher runs under its own virtualenv (e.g. `uv run`), which exports
VIRTUAL_ENV and puts its bin on PATH. That leaks into `claude`, `ntree`, and the
target repo's `poetry run`, making poetry bind to the WRONG interpreter (and fail a
`>=3.12` constraint if the pipeline runs on older Python). Strip it so subprocesses
resolve the target repo's own environment.
"""

from __future__ import annotations

import os


def clean_env() -> dict[str, str]:
    env = dict(os.environ)
    venv = env.pop("VIRTUAL_ENV", None)
    env.pop("UV", None)
    env.pop("PYTHONHOME", None)
    if venv:
        bin_dir = os.path.join(venv, "bin")
        env["PATH"] = os.pathsep.join(
            p for p in env.get("PATH", "").split(os.pathsep) if p and p != bin_dir
        )
    return env
