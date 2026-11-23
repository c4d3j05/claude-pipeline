"""Releases: tickets that build on a release branch instead of main.

claude-planner files a batch of explorer's findings as one release (formats: planner's integration
spec, `openspec/specs/integration/spec.md`). On board 51 a release is:
- a tracking task titled "Release R-n", labelled `release-tracker` (never claimed: no `claude` label),
  whose subtasks are the release's tickets;
- on each ticket a line `Release: R-n · branch release/r-n`.

What the pipeline does with them:
- A ticket with a Release line forks from, and opens its PR against, `release/r-n`. The branch is
  created from main on first use, and main is merged into it before every claim. The existing
  base check then tests the release head once per head commit.
- A merge conflict with main, or a red release head, files a "Repair release R-n" ticket inside the
  release; no other ticket of that release is claimed until it is done. A repair that resolves a
  conflict is merged with a merge commit, so main stays recorded as merged.
- With RELEASE_AUTO_MERGE on, a ticket PR whose base is a release branch is merged by the pipeline
  once its gates pass (after re-testing on the release head if the release moved).
- When every subtask of a release is done and its head is green, the pipeline opens the
  release -> main PR. With RELEASE_MERGE_TO_MAIN on it merges that PR itself, with a merge commit
  (the owner's decision of 2026-11-22: releases are verified on UAT after they merge); otherwise a
  human merges it. Once merged, the tracker moves to Done and the release branch is deleted.
- `auto_merge_allowed` is the only gate for both merges. A ticket PR into main (a DM ticket, a
  hotfix) is never merged by the pipeline, whatever the settings.

Never raises into the tick: every failure is logged and retried on a later pass.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .log import get
from .procenv import clean_env
from .prompt import _strip_html

log = get("releases")

RELEASE_LINE = re.compile(r"^Release:\s*(R-\d+)\s*·\s*branch\s+(release/r-\d+)\s*$")
RELEASE_BRANCH = re.compile(r"^release/r-\d+$")
RELEASE_TITLE = re.compile(r"^Release (R-\d+)\s*$")
REPAIR_TITLE = re.compile(r"^Repair release (R-\d+)\b")
TRACKER_LABEL = "release-tracker"
IDENTITY = {"GIT_AUTHOR_NAME": "claude-pipeline", "GIT_AUTHOR_EMAIL": "claude-pipeline@users.noreply.github.com",
            "GIT_COMMITTER_NAME": "claude-pipeline",
            "GIT_COMMITTER_EMAIL": "claude-pipeline@users.noreply.github.com"}


@dataclass(frozen=True)
class Target:
    release: str          # R-4
    branch: str           # release/r-4


def target_of(description_html: str | None) -> Target | None:
    """The ticket's release, from its Release line; None for a ticket that targets main."""
    found = None
    for line in _strip_html(description_html or "").splitlines():
        m = RELEASE_LINE.match(line.strip())
        if m and m.group(2) == f"release/{m.group(1).lower()}":
            found = Target(m.group(1), m.group(2))
    return found


def is_release_branch(branch: str | None) -> bool:
    return bool(RELEASE_BRANCH.fullmatch(branch or ""))


def auto_merge_allowed(*, tickets: bool, to_main: bool, head: str | None, base: str | None,
                       base_branch: str) -> bool:
    """The only place that decides whether the pipeline may merge a PR itself:
    - a ticket PR into a release branch, when RELEASE_AUTO_MERGE (`tickets`) is on;
    - the release PR (head release/r-n, base BASE_BRANCH), when RELEASE_MERGE_TO_MAIN (`to_main`) is on.
    Anything else never: a ticket PR into main (a DM ticket, a hotfix) is a human's to merge whatever
    the settings, and so is a PR between release branches."""
    if is_release_branch(base):
        return bool(tickets) and not is_release_branch(head)
    return bool(to_main) and bool(base_branch) and base == base_branch and is_release_branch(head)


def merge_messages(rel: str, url: str, base: str, outcome: str, why: str) -> tuple[str | None, str]:
    """(tracker comment or None, Slack alert) for how the release PR's merge went: `merged`,
    `queued` (GitHub merges it once main's required checks pass), `refused` (it waits for the owner;
    the open PR is not retried) or `off` (RELEASE_MERGE_TO_MAIN off: a human merges it). The
    tracker's `merged: <sha9> into <base> (<url>)` comment comes later, from the merged PR."""
    ready = f"Release {rel}: every ticket is merged and its head is green"
    by_hand = f"Merge it with a MERGE COMMIT (not squash), so explorer can trace each fix on UAT: {url}"
    if outcome == "merged":
        return None, f"{ready}; the pipeline merged it into {base} with a merge commit: {url}"
    if outcome == "queued":
        return (f"auto-merge into {base} is on; GitHub merges it once the required checks pass ({url})",
                f"{ready}; GitHub merges it into {base} (merge commit) once the required checks pass: {url}")
    if outcome == "refused":
        return (f"not merged into {base}: {why}; the owner merges it with a merge commit ({url})",
                f"Release {rel} needs you: the pipeline could not merge it ({why}). {by_hand}")
    return None, f"Release {rel} is ready: every ticket is merged and its head is green. {by_hand}"


def labels_of(task: dict) -> set[str]:
    return {(lb.get("title") or "").strip().lower() for lb in task.get("labels") or []}


def is_tracker(task: dict) -> bool:
    return TRACKER_LABEL in labels_of(task)


def repair_release(task: dict) -> str | None:
    m = REPAIR_TITLE.match((task.get("title") or "").strip())
    return m.group(1) if m else None


def open_repairs(board: list[dict]) -> dict[str, int]:
    """{release: repair task id} for every repair ticket on the board that is not done."""
    out: dict[str, int] = {}
    for b in board:
        for t in b.get("tasks") or []:
            rel = repair_release(t)
            if rel and not t.get("done"):
                out.setdefault(rel, t["id"])
    return out


def trackers(board: list[dict]) -> dict[str, dict]:
    """{release: tracker task} for every release tracker on the board."""
    out: dict[str, dict] = {}
    for b in board:
        for t in b.get("tasks") or []:
            m = RELEASE_TITLE.match((t.get("title") or "").strip())
            if m and is_tracker(t):
                out[m.group(1)] = t
    return out


@dataclass
class Sync:
    ok: bool
    conflict: list[str]           # conflicting files when ok is False because of a conflict
    error: str = ""
    merged: bool = False          # main had to be merged in (the head moved)


class ReleaseGit:
    """Git on release branches, in a scratch worktree of the target repo: the repo's own checkout
    and the ticket workspaces are never moved."""

    def __init__(self, repo: Path, scratch: Path, *, run=subprocess.run, base: str = "main"):
        self.repo, self.scratch, self._run, self.base = Path(repo), Path(scratch), run, base

    def _git(self, *args: str, cwd: Path | None = None, creds: bool = False, timeout: int = 300):
        env = {**clean_env("github" if creds else "plain"), **IDENTITY}
        try:
            return self._run(["git", *args], cwd=cwd or self.repo, capture_output=True, text=True,
                             timeout=timeout, env=env)
        except (subprocess.TimeoutExpired, FileNotFoundError) as e:
            return subprocess.CompletedProcess(["git", *args], 1, "", str(e))

    def fetch(self, *branches: str) -> bool:
        refs = [f"+refs/heads/{b}:refs/remotes/origin/{b}" for b in branches]
        return self._git("fetch", "--quiet", "origin", *refs, creds=True).returncode == 0

    def exists(self, branch: str) -> bool:
        return self._git("rev-parse", "--verify", "--quiet", f"origin/{branch}").returncode == 0

    def sha(self, ref: str) -> str:
        cp = self._git("rev-parse", "--verify", "--quiet", ref)
        return cp.stdout.strip() if cp.returncode == 0 else ""

    def is_ancestor(self, a: str, b: str, cwd: Path | None = None) -> bool:
        return self._git("merge-base", "--is-ancestor", a, b, cwd=cwd).returncode == 0

    def ensure(self, branch: str) -> bool:
        """The release branch on origin, created from origin/<base> when missing."""
        self.fetch(self.base)
        if not self.exists(branch):
            self._git("fetch", "--quiet", "origin", f"+refs/heads/{branch}:refs/remotes/origin/{branch}",
                      creds=True)
        if self.exists(branch):
            return True
        cp = self._git("push", "--quiet", "origin", f"origin/{self.base}:refs/heads/{branch}", creds=True)
        if cp.returncode != 0:
            log.warning("could not create %s: %s", branch, cp.stderr.strip()[-300:])
            return False
        log.info("created %s from origin/%s", branch, self.base)
        return self.fetch(branch)

    def sync(self, branch: str) -> Sync:
        """Merge origin/<base> into the release branch and push. Never squashes."""
        if not self.ensure(branch) or not self.fetch(self.base, branch):
            return Sync(False, [], "could not fetch or create the release branch")
        if self.is_ancestor(f"origin/{self.base}", f"origin/{branch}"):
            return Sync(True, [])
        wt = self.scratch / branch.replace("/", "-")
        shutil.rmtree(wt, ignore_errors=True)
        self._git("worktree", "prune")
        add = self._git("worktree", "add", "--detach", str(wt), f"origin/{branch}")
        if add.returncode != 0:
            return Sync(False, [], f"worktree add failed: {add.stderr.strip()[-200:]}")
        try:
            m = self._git("merge", "--no-ff", "--no-edit", "-m", f"Merge {self.base} into {branch}",
                          f"origin/{self.base}", cwd=wt)
            if m.returncode != 0:
                files = self._git("diff", "--name-only", "--diff-filter=U", cwd=wt).stdout.split()
                self._git("merge", "--abort", cwd=wt)
                return Sync(False, files or ["(see the merge output)"], m.stdout.strip()[-300:])
            p = self._git("push", "--quiet", "origin", f"HEAD:refs/heads/{branch}", cwd=wt, creds=True)
            if p.returncode != 0:
                return Sync(False, [], f"push failed: {p.stderr.strip()[-200:]}")
            self.fetch(branch)
            log.info("merged %s into %s", self.base, branch)
            return Sync(True, [], merged=True)
        finally:
            self._git("worktree", "remove", "--force", str(wt))

    def update_ticket(self, worktree: Path, branch: str) -> Sync:
        """Bring a ticket's branch up to the release head before it is merged into it."""
        if not self.fetch(branch):
            return Sync(False, [], "could not fetch the release branch")
        if self.is_ancestor(f"origin/{branch}", "HEAD", cwd=worktree):
            return Sync(True, [])
        m = self._git("merge", "--no-edit", "-m", f"Merge {branch} into the ticket", f"origin/{branch}",
                      cwd=worktree)
        if m.returncode != 0:
            files = self._git("diff", "--name-only", "--diff-filter=U", cwd=worktree).stdout.split()
            self._git("merge", "--abort", cwd=worktree)
            return Sync(False, files or ["(see the merge output)"], m.stdout.strip()[-300:])
        return Sync(True, [], merged=True)

    def delete(self, branch: str) -> bool:
        return self._git("push", "--quiet", "origin", "--delete", branch, creds=True).returncode == 0


def repair_description(target: Target, *, conflict: list[str] | None = None, tail: str = "") -> str:
    """A repair ticket's text: the Release line (so it builds on the release branch) and what to do."""
    if conflict:
        what = (f"<p>Merging <code>main</code> into <code>{target.branch}</code> conflicts in: "
                f"{', '.join(conflict[:20])}.</p>"
                f"<p>Run <code>git merge origin/main</code> in this workspace, resolve the conflicts so both "
                "sides keep working, and commit the merge (a merge commit with both parents; never squash or "
                "rebase).</p>")
        done = ["origin/main is merged into this branch with a merge commit, conflicts resolved",
                "The test suite passes"]
    else:
        what = (f"<p>The test suite fails on the head of <code>{target.branch}</code>, with every ticket "
                "of the release merged. Find which changes conflict and fix the branch.</p>"
                f"<pre>{tail[-1500:]}</pre>")
        done = ["The test suite passes on the release branch", "No ticket's acceptance criteria are undone"]
    return (f"<p>Release: {target.release} · branch {target.branch}</p>{what}"
            "<h3>Definition of Done</h3><ul>" + "".join(f"<li>{d}</li>" for d in done) + "</ul>")
