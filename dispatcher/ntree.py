"""Thin wrapper over the ntree contract.

The spec (Portability note) says to talk to the *contract* — worktree, port pair,
NTREE_WORKSPACE — not to ntree's CLI directly, so a Linux shell equivalent can be
swapped in later. Every method here is one contract operation.
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

from .log import get
from .proc import TIMEOUT_RC, run_group
from .procenv import clean_env, with_git_config

log = get("ntree")


# Lock files (relative to the common git dir) that a killed git can leave behind.
_LOCK_GLOBS = (
    "config.lock", "packed-refs.lock", "shallow.lock", "HEAD.lock", "refs/**/*.lock",
    "worktrees/*/index.lock", "worktrees/*/HEAD.lock",
)
LOCK_RETRIES = 4          # attempts for `ntree new` when git reports a lock error
LOCK_RETRY_SLEEP = 15.0   # seconds between attempts (lets a fresh lock age past the sweep)


class NtreeError(RuntimeError):
    pass


class Ntree:
    def __init__(self, repo_path: Path, base_branch: str = "main"):
        self.repo = repo_path
        self.base = base_branch

    def _run(self, args: list[str], timeout: int | None = None) -> subprocess.CompletedProcess:
        log.debug("ntree %s (cwd=%s)", " ".join(args), self.repo)
        # run_group: on timeout the whole tree (pytest, servers, package managers) is stopped,
        # not just `ntree` itself. Credentials: none — ntree runs the repo's own setup and
        # test commands, including worker-written code.
        return run_group(
            ["ntree", *args],
            cwd=self.repo,
            timeout=timeout,
            # Forking from a remote ref (origin/main) makes git write upstream-tracking
            # config into the shared .git/config. We never want that (the PR push sets its
            # own upstream) and it contends for config.lock with every other git process.
            env=with_git_config(clean_env("plain"), branch__autoSetupMerge="false"),
        )

    def _git_common_dir(self) -> Path | None:
        cp = subprocess.run(["git", "rev-parse", "--git-common-dir"], cwd=self.repo,
                            capture_output=True, text=True, timeout=30)
        return (self.repo / cp.stdout.strip()) if cp.returncode == 0 else None

    def clear_stale_git_locks(self, min_age_s: float = 30.0) -> list[Path]:
        """Remove git lock files left behind by a killed git process.

        A lock is removed only if it is older than `min_age_s` AND no process has it open,
        so a live git (another worker, you in a terminal) is never disturbed.
        """
        common = self._git_common_dir()
        if common is None:
            return []
        removed: list[Path] = []
        for pattern in _LOCK_GLOBS:
            for lock in common.glob(pattern):
                try:
                    age = time.time() - lock.stat().st_mtime
                except OSError:
                    continue
                if age < min_age_s:
                    continue
                try:
                    held = subprocess.run(["lsof", "-t", str(lock)], capture_output=True,
                                          text=True, timeout=10).stdout.strip()
                except (OSError, subprocess.SubprocessError):
                    held = ""
                if held:
                    continue
                lock.unlink(missing_ok=True)
                log.warning("removed stale git lock %s (%.0fs old, no holder)", lock, age)
                removed.append(lock)
        return removed

    def rev(self, ref: str) -> str:
        """Resolve a ref to a commit SHA ('' if it can't be resolved)."""
        cp = subprocess.run(["git", "rev-parse", "--verify", "--quiet", ref], cwd=self.repo,
                            capture_output=True, text=True, timeout=30, env=clean_env("plain"))
        return cp.stdout.strip() if cp.returncode == 0 else ""

    def base_ref(self) -> str:
        """The ref workspaces fork from and PRs are diffed against.

        Fetches first so workers never start from a stale local branch; prefers
        `origin/<base>` when it exists, else falls back to the local base branch.
        """
        try:
            subprocess.run(["git", "fetch", "--quiet", "origin", self.base], cwd=self.repo,
                           capture_output=True, text=True, timeout=120, env=clean_env("github"))
            cp = subprocess.run(
                ["git", "rev-parse", "--verify", "--quiet", f"origin/{self.base}"],
                cwd=self.repo, capture_output=True, text=True, timeout=30)
        except (subprocess.TimeoutExpired, FileNotFoundError) as e:
            log.warning("could not refresh %s from origin: %s", self.base, e)
            return self.base
        return f"origin/{self.base}" if cp.returncode == 0 else self.base

    def new(self, slug: str, base_ref: str | None = None) -> Path:
        """Create the worktree + reserve ports + run NTREE_SETUP_CMD. Idempotent-ish."""
        args = ["new", slug, "--from", base_ref or self.base]
        cp = self._run(args, timeout=1200)
        out = (cp.stderr + cp.stdout)
        if cp.returncode == TIMEOUT_RC:
            raise NtreeError(f"ntree new {slug} timed out")
        for _ in range(LOCK_RETRIES - 1):
            if cp.returncode == 0 or "could not lock" not in out:
                break
            # A git lock is in the way: sweep stale ones, else wait for a live holder.
            if not self.clear_stale_git_locks():
                time.sleep(LOCK_RETRY_SLEEP)
            self.rm(slug)  # drop the half-made worktree before retrying
            cp = self._run(args, timeout=1200)
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
