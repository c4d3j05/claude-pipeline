# Releases

How claude-pipeline builds a release: a set of tickets that reach `main` together, as one pull
request merged with a merge commit. The contract shared with claude-planner and explorer is
explorer's OpenSpec change `release-batches` (`openspec/changes/release-batches/design.md`, section
"Wire contract"). This file says what this repo does with it.

**Not yet in `spec.md`.** `spec.md` says a human merges every PR and that auto-merge is out of scope.
The owner decided on 2026-11-22:
- the pipeline may merge a ticket's PR into a *release* branch, behind `RELEASE_AUTO_MERGE`;
- releases are verified by explorer on UAT *after* they merge, and the pipeline itself merges the
  release PR into main, with a merge commit, behind `RELEASE_MERGE_TO_MAIN`.

Both are off by default; with them off, a human merges and everything else works the same. A ticket
PR into main (a DM ticket, a hotfix) is never merged by the pipeline. `spec.md` is the owner's to
update.

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
   the only gate for every merge the pipeline makes (see "Who may merge" below).
6. **On merge** the card moves to Done with `merged: <sha> into release/r-n` (explorer reads it).

## Finishing a release

Every cleanup interval (5 min), in the background:
- For a tracker whose subtasks are all done and that has no release PR open: merge main in, run the
  test gate (and `RELEASE_CHECK_CMD`) on the head, once per head commit. Green: open
  `release/r-n -> main`, labelled `claude-release`, titled `Release R-n`. Red or conflicting: file
  the repair ticket.
- Then, with `RELEASE_MERGE_TO_MAIN=true`, the pipeline merges that PR itself, once:
  `gh pr merge --merge --match-head-commit <tested head>`. If GitHub refuses (main's required
  checks have not finished), it turns on GitHub's auto-merge (`--auto`, still a merge commit), so
  GitHub merges it when they pass. If that is refused too (a conflict, a permission, a protection
  rule), the tracker gets a `not merged into main: …` comment and Slack says the release needs the
  owner; the open PR is not tried again. With the setting off, Slack says it must be merged **with a
  merge commit**, as before.
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
| `RELEASE_AUTO_MERGE` | `false` | Merge a release ticket's PR into its release branch once its gates pass. |
| `RELEASE_MERGE_TO_MAIN` | `false` | Merge the release PR into main, with a merge commit, once every ticket is done and the release head is green. |
| `RELEASE_PR_LABEL` | `claude-release` | Label of the release -> main PRs. |
| `RELEASE_CHECK_CMD` | (empty) | Extra check on a release head after the tests, run in its workspace, e.g. `cd backend && poetry run python manage.py makemigrations --check --dry-run`. Django's test run already fails on conflicting migrations. |

## Who may merge

`auto_merge_allowed()` in `releases.py` decides every merge the pipeline makes, and nothing else does:

| PR | Merged by the pipeline when |
|---|---|
| ticket -> `release/r-n` | `RELEASE_AUTO_MERGE=true` |
| `release/r-n` -> `BASE_BRANCH` (the release PR) | `RELEASE_MERGE_TO_MAIN=true` |
| ticket -> `BASE_BRANCH` (a DM ticket, a hotfix) | never |
| anything else | never |

GitHub: the repo must allow merge commits (Zentre's does), and the token in `~/.worker.env` must be
able to merge PRs and to push and delete `release/*` branches. With `RELEASE_MERGE_TO_MAIN=true`,
main's branch protection must also let that token merge the release PR with a merge commit (it must
not require a human review or linear history), and the repo must allow auto-merge if main has
required checks.
