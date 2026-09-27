"""Launch a Claude Code worker session inside a worktree and capture its stream."""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

from .log import get
from .procenv import clean_env
from .stream import SessionResult, parse_event

log = get("worker")


def launch(
    *,
    worktree: Path,
    prompt: str,
    settings_file: str,
    session_log: Path,
    max_turns: int,
    wall_clock_limit_min: int,
    model: str = "",
    permission_mode: str = "acceptEdits",
    read_only: bool = False,
    resume_session: str = "",
    on_pid=None,
) -> tuple[SessionResult, int]:
    """Run `claude -p` in `worktree`, tee the stream to `session_log`, and parse it.

    Returns (SessionResult, returncode). A wall-clock timeout kills the process group
    and is reported as returncode 124 (like `timeout`).
    """
    cmd = [
        "claude",
        "-p",
        prompt,
        "--output-format",
        "stream-json",
        "--verbose",
        "--max-turns",
        str(max_turns),
        "--permission-mode",
        permission_mode,
        "--settings",
        settings_file,
    ]
    if model:
        cmd += ["--model", model]
    if resume_session:
        cmd += ["--resume", resume_session]

    session_log.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + wall_clock_limit_min * 60
    result = SessionResult()

    log.info("launch worker in %s (max_turns=%s, wall=%smin%s)",
             worktree.name, max_turns, wall_clock_limit_min,
             ", read-only" if read_only else "")

    with open(session_log, "w") as logf:
        proc = subprocess.Popen(
            cmd,
            cwd=worktree,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            start_new_session=True,  # own process group, so we can kill children too
            env=clean_env(),
        )
        if on_pid:
            on_pid(proc.pid)
        assert proc.stdout is not None
        try:
            for line in proc.stdout:
                logf.write(line)
                logf.flush()
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(ev, dict):
                    parse_event(ev, result)
                if time.monotonic() > deadline:
                    log.warning("wall-clock limit hit; killing worker %s", worktree.name)
                    _kill(proc)
                    proc.wait(timeout=30)
                    return result, 124
        finally:
            rc = proc.wait()
    return result, rc


def _kill(proc: subprocess.Popen) -> None:
    import os
    import signal

    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        time.sleep(2)
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except ProcessLookupError:
        pass
