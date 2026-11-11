"""Launch a Claude Code worker session inside a worktree and capture its stream."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import threading
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
    wall_clock_limit_min: float,
    model: str = "",
    permission_mode: str = "acceptEdits",
    read_only: bool = False,
    resume_session: str = "",
    on_pid=None,
) -> tuple[SessionResult, int]:
    """Run `claude -p` in `worktree`, tee the stream to `session_log`, and parse it.

    The prompt goes in on stdin (not argv): it avoids the 128 KiB single-argument limit
    on Linux and keeps task text / diffs out of `ps` output.

    The wall-clock limit is enforced by a watchdog timer, so it fires even if the
    session goes completely silent. A timeout kills the process group and is reported
    as returncode 124 (like `timeout`).
    """
    cmd = [
        "claude",
        "-p",
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
    result = SessionResult()

    log.info("launch worker in %s (max_turns=%s, wall=%smin%s)",
             worktree.name, max_turns, wall_clock_limit_min,
             ", read-only" if read_only else "")

    timed_out = threading.Event()
    with open(session_log, "a") as logf:
        proc = subprocess.Popen(
            cmd,
            cwd=worktree,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            start_new_session=True,  # own process group, so we can kill children too
            env=clean_env(),
        )

        def _expire() -> None:
            log.warning("wall-clock limit hit; killing worker %s", worktree.name)
            timed_out.set()
            kill_group(proc.pid)

        timer = threading.Timer(wall_clock_limit_min * 60, _expire)
        timer.daemon = True
        timer.start()
        try:
            if on_pid:
                on_pid(proc.pid)
            assert proc.stdin is not None and proc.stdout is not None
            try:
                proc.stdin.write(prompt)
                proc.stdin.close()
            except (BrokenPipeError, OSError):
                pass  # process died before reading; the stream/returncode will say why
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
        finally:
            timer.cancel()
            rc = proc.wait()
    return result, (124 if timed_out.is_set() else rc)


def kill_group(pid: int, grace: float = 2.0) -> None:
    """SIGTERM a process group, then SIGKILL whatever is left."""
    try:
        pgid = os.getpgid(pid)
    except (ProcessLookupError, PermissionError):
        return
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(pgid, sig)
        except (ProcessLookupError, PermissionError):
            return
        if sig == signal.SIGTERM:
            time.sleep(grace)
