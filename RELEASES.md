# Releases

How claude-pipeline builds a release: a set of tickets that reach `main` together, as one pull
request merged with a merge commit. The contract shared with claude-planner and explorer is
explorer's OpenSpec change `release-batches` (`openspec/changes/release-batches/design.md`, section
"Wire contract"). This file says what this repo does with it.

**Not yet in `spec.md`.** `spec.md` says a human merges every PR and that auto-merge is out of scope.
The owner decided on 2026-11-22 that the pipeline may merge a ticket's PR into a *release* branch
(never into main). That is implemented behind `RELEASE_AUTO_MERGE`, off by default; with it off, a
human merges ticket PRs into the release branch and everything else works the same. `spec.md` is
the owner's to update.

## On the board

- A tracking task titled `Release R-n`, labelled `release-tracker`. It has no `claude` label, and the
  dispatcher skips the tracker label explicitly as well: it is never claimed.
- Every ticket of the release is a subtask of the tracker and carries the line
  `Release: R-n · branch release/r-n`.
- A ticket without that line targets `BASE_BRANCH` exactly as before.

## What happens to a release ticket

1. **Before the claim** the dispatcher creates `release/r-n` from main if it does not exist, and
   merges main into it (a merge commit, in a scratch worktree; the repo's own checkout is never
   moved). The ticket's workspace forks from `origin/release/r-n`.
2. **The base check** (the test gate on the untouched base, once per commit) now runs on the release
   head. So the release is tested as a whole after every sync, once per head commit.
3. **A conflict with main, or a red release head**, files a `Repair release R-n` ticket: in Ready,
   labelled `claude` and `release:R-n`, a subtask of the tracker, with the Release line. The ticket
   that found it goes back to Ready with a `waiting:` comment. No other ticket of that release is
   claimed until the repair is done. Only one repair per release is open at a time.
4. **The PR** targets `release/r-n`.
5. **With `RELEASE_AUTO_MERGE=true`**, once the tests and review pass: if the release moved since the
   ticket branched, the release head is merged into the ticket's branch and the tests run again; then
   `gh pr merge --squash --match-head-commit <tested sha>`. A repair is merged with `--merge`, so main
   stays recorded as merged and the next sync has nothing to redo. A conflict, a red gate or a refused
   merge leaves the PR open and posts "needs attention". `auto_merge_allowed()` in `releases.py` is
   the only gate, and it refuses any base that is not exactly `release/r-<n>`.
6. **On merge** the card moves to Done with `merged: <sha> into release/r-n` (explorer reads it).

## Finishing a release

Every cleanup interval (5 min), in the background:
- For a tracker whose subtasks are all done and that has no release PR open: merge main in, run the
  test gate (and `RELEASE_CHECK_CMD`) on the head, once per head commit. Green: open
  `release/r-n -> main`, labelled `claude-release`, titled `Release R-n`, and post to Slack that it
  must be merged **with a merge commit**. Red or conflicting: file the repair ticket.
- For a merged release PR: the tracker moves to Done (`merged: <sha> into main`) and the branch is
  deleted.
- A ticket with the Release line but no subtask relation, or the reverse, is reported once in Slack:
  it is half removed from the release.

A release with a blocked ticket waits; nothing ships without a ticket automatically. The nightly
stuck-dependent warning names the release.

Work for main is never held back by a release ticket's files: that overlap is settled when main is
next merged into the release branch. A release ticket still waits for main work on the same files.

## Settings

| Setting | Default | Meaning |
|---|---|---|
| `RELEASE_AUTO_MERGE` | `false` | Merge a release ticket's PR into its release branch once its gates pass. Never applies to main. |
| `RELEASE_PR_LABEL` | `claude-release` | Label of the release -> main PRs. |
| `RELEASE_CHECK_CMD` | (empty) | Extra check on a release head after the tests, run in its workspace, e.g. `cd backend && poetry run python manage.py makemigrations --check --dry-run`. Django's test run already fails on conflicting migrations. |

GitHub: the repo must allow merge commits (Zentre's does), and the token in `~/.worker.env` must be
able to merge PRs and to push and delete `release/*` branches.
