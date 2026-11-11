"""Rendering and checking the per-repo worker settings (shared by prepare-repo/selftest)."""

from __future__ import annotations

import json
import shlex
from pathlib import Path

TEMPLATES = Path(__file__).resolve().parent.parent / "templates"


def stop_hook_command(test_cmd: str) -> str:
    """Stop-hook command that runs the gate's own TEST_CMD.

    Wrapped in `bash -c` with pipefail: the hook runner may use dash (Debian's /bin/sh),
    which has neither PIPESTATUS nor pipefail, and without pipefail `| tail` would mask
    a failing test run behind tail's exit code.
    """
    inner = f'set -o pipefail; ntree run "$NTREE_WORKSPACE" -- {test_cmd} 2>&1 | tail -40'
    return f"bash -c {shlex.quote(inner)}"


def render_worker_settings(test_cmd: str) -> str:
    data = json.loads((TEMPLATES / "worker-settings.json").read_text())
    data["hooks"]["Stop"][0]["hooks"][0]["command"] = stop_hook_command(test_cmd)
    return json.dumps(data, indent=2) + "\n"


def hook_command(settings_text: str) -> str | None:
    try:
        return json.loads(settings_text)["hooks"]["Stop"][0]["hooks"][0]["command"]
    except (ValueError, KeyError, IndexError, TypeError):
        return None
