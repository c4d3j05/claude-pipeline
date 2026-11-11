"""Subprocess helpers that never leak process trees.

`subprocess.run(timeout=…)` kills only the direct child; anything it spawned (pytest,
servers, package managers) keeps running. Everything long-running goes through here.
"""

from __future__ import annotations

import os
import signal
import subprocess
import time
from pathlib import Path

from .log import get

log = get("proc")

TIMEOUT_RC = 124  # same convention as coreutils `timeout`


def kill_group(pid: int, grace: float = 15.0, proc: subprocess.Popen | None = None) -> None:
    """Stop a process group gracefully: SIGTERM, wait up to `grace`s, SIGKILL stragglers.

    The wait matters: git removes its lock files when it receives SIGTERM, but a SIGKILL
    leaves .git/config.lock (and friends) behind, which then breaks every later
    `git worktree add` / `git config` in the repo. Pass `proc` when the leader is our own
    child so it is reaped (a zombie leader would keep the group looking alive).
    """
    try:
        pgid = os.getpgid(pid)
        os.killpg(pgid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline:
        if proc is not None:
            proc.poll()
        try:
            os.killpg(pgid, 0)
        except (ProcessLookupError, PermissionError):
            return  # whole group exited on SIGTERM — nothing left to leave a lock behind
        time.sleep(0.1)
    log.warning("process group %s ignored SIGTERM for %.0fs; sending SIGKILL", pgid, grace)
    try:
        os.killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def run_group(
    cmd: list[str],
    *,
    cwd: Path | str | None = None,
    env: dict[str, str] | None = None,
    timeout: float | None = None,
    grace: float = 15.0,
) -> subprocess.CompletedProcess:
    """`subprocess.run` (text, captured) that owns a process group and kills all of it.

    On timeout the whole tree is stopped and the result has returncode TIMEOUT_RC (124),
    with whatever output was produced so far, instead of raising and orphaning children.
    """
    proc = subprocess.Popen(
        cmd, cwd=cwd, env=env, text=True, start_new_session=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    try:
        out, err = proc.communicate(timeout=timeout)
        return subprocess.CompletedProcess(cmd, proc.returncode, out, err)
    except subprocess.TimeoutExpired:
        log.warning("timed out after %ss; stopping process group of pid %s", timeout, proc.pid)
        kill_group(proc.pid, grace=grace, proc=proc)
        try:
            out, err = proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            out, err = "", ""
        return subprocess.CompletedProcess(
            cmd, TIMEOUT_RC, out or "", (err or "") + f"\n[timed out after {timeout}s]")
