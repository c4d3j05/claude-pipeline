"""Thin wrapper over the ntree contract.

The spec (Portability note) says to talk to the *contract* — worktree, port pair,
NTREE_WORKSPACE — not to ntree's CLI directly, so a Linux shell equivalent can be
swapped in later. Every method here is one contract operation.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from .log import get

log = get("ntree")


class NtreeError(RuntimeError):
    pass


class Ntree:
    def __init__(self, repo_path: Path, base_branch: str = "main"):
        self.repo = repo_path
        self.base = base_branch

    def _run(self, args: list[str], timeout: int | None = None) -> subprocess.CompletedProcess:
        log.debug("ntree %s (cwd=%s)", " ".join(args), self.repo)
        return subprocess.run(
            ["ntree", *args],
            cwd=self.repo,
            capture_output=True,
            text=True,
            timeout=timeout,
        )

    def new(self, slug: str) -> Path:
        """Create the worktree + reserve ports + run NTREE_SETUP_CMD. Idempotent-ish."""
        cp = self._run(["new", slug, "--from", self.base], timeout=1200)
        if cp.returncode != 0 and "already" not in (cp.stderr + cp.stdout).lower():
            raise NtreeError(f"ntree new {slug} failed: {cp.stderr.strip() or cp.stdout.strip()}")
        return self.path(slug)

    def path(self, slug: str) -> Path:
        cp = self._run(["path", slug])
        if cp.returncode != 0:
            raise NtreeError(f"ntree path {slug} failed: {cp.stderr.strip()}")
        return Path(cp.stdout.strip())

    def run(
        self, slug: str, cmd: list[str], timeout: int | None = None
    ) -> subprocess.CompletedProcess:
        """Foreground `ntree run <slug> -- <cmd>`; sources .env.workspace (PORT, NTREE_WORKSPACE)."""
        return self._run(["run", slug, "--", *cmd], timeout=timeout)

    def rm(self, slug: str) -> None:
        cp = self._run(["rm", slug, "--force"], timeout=300)
        if cp.returncode != 0:
            log.warning("ntree rm %s: %s", slug, cp.stderr.strip() or cp.stdout.strip())

    def doctor(self) -> subprocess.CompletedProcess:
        return self._run(["doctor"], timeout=300)

    def list_json(self) -> str:
        # `ntree list` is human-oriented; callers parse what they need.
        return self._run(["list"]).stdout
