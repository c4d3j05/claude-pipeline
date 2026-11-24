# Proposal

## Why

On 2026-11-24 R-2's first ticket PR (kauby #637) stayed open although `RELEASE_AUTO_MERGE` was on.
The pipeline merges a ticket PR into its release branch with `gh pr merge --squash`, and kauby
allows only merge commits (`allow_squash_merge=false`), so GitHub refused every such merge. The
refusal went only to a Vikunja comment and a Slack "needs attention" line, never to the log, so it
looked like nothing had happened. The owner confirmed squash is not allowed, only merge commits.

repowise findings:
- `context dispatcher/pr.py`: `merge_into_release` is the only ticket-merge path (called from
  `Daemon._auto_merge`); it picks `--merge` only for a repair ticket.
- `why`: release-batches chose squash for tickets and merge commits for repairs and release PRs; the
  release PR body already asks for "a merge commit, not a squash" so explorer can find each merge on UAT.

## What Changes

- New setting `RELEASE_TICKET_MERGE_METHOD` (`merge` | `squash` | `rebase`, default `merge`, an unknown
  value falls back to `merge`). A repair ticket is still always merged with `--merge`.
- A refused merge is logged as a warning with GitHub's message, and a successful one at info.

## Impact

- pipeline: `dispatcher/config.py`, `dispatcher/pr.py`, `.env.example`, `RELEASES.md`, `tests/test_releases.py`.
- planner, explorer: none. A merge commit on the release branch is what explorer already reads.
