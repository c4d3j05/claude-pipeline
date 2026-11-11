"""Thin wrapper over the ntree contract.

The spec (Portability note) says to talk to the *contract* — worktree, port pair,
NTREE_WORKSPACE — not to ntree's CLI directly, so a Linux shell equivalent can be
swapped in later. Every method here is one contract operation.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from .log import get
from .procenv import clean_env

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
            env=clean_env(),
        )

    def base_ref(self) -> str:
        """The ref workspaces fork from and PRs are diffed against.

        Fetches first so workers never start from a stale local branch; prefers
        `origin/<base>` when it exists, else falls back to the local base branch.
        """
        try:
            subprocess.run(["git", "fetch", "--quiet", "origin", self.base], cwd=self.repo,
                           capture_output=True, text=True, timeout=120, env=clean_env())
            cp = subprocess.run(
                ["git", "rev-parse", "--verify", "--quiet", f"origin/{self.base}"],
                cwd=self.repo, capture_output=True, text=True, timeout=30)
        except (subprocess.TimeoutExpired, FileNotFoundError) as e:
            log.warning("could not refresh %s from origin: %s", self.base, e)
            return self.base
        return f"origin/{self.base}" if cp.returncode == 0 else self.base

    def new(self, slug: str, base_ref: str | None = None) -> Path:
        """Create the worktree + reserve ports + run NTREE_SETUP_CMD. Idempotent-ish."""
        cp = self._run(["new", slug, "--from", base_ref or self.base], timeout=1200)
        out = (cp.stderr + cp.stdout)
        if cp.returncode != 0 and "already" not in out.lower():
            raise NtreeError(f"ntree new {slug} failed: {cp.stderr.strip() or cp.stdout.strip()}")
        # ntree exits 0 when NTREE_SETUP_CMD fails (it only warns). A workspace with
        # broken dependencies would waste a whole worker run, so treat it as fatal.
        if "setup exited" in out:
            line = next((ln for ln in out.splitlines() if "setup exited" in ln), "setup failed")
            raise NtreeError(f"ntree new {slug}: {line.strip()}")
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

    def remove_branch(self, slug: str) -> None:
        """Delete the git branch left behind by `ntree rm` (it removes only the worktree).

        Without this, a re-created workspace reuses the stale branch instead of
        re-forking from the base, so it never sees new commits on the base branch.
        """
        cp = subprocess.run(
            ["git", "branch", "-D", slug],
            cwd=self.repo,
            capture_output=True,
            text=True,
        )
        if cp.returncode == 0:
            log.debug("deleted stale branch %s", slug)

    def doctor(self) -> subprocess.CompletedProcess:
        return self._run(["doctor"], timeout=300)

    def list_json(self) -> str:
        # `ntree list` is human-oriented; callers parse what they need.
        return self._run(["list"]).stdout
