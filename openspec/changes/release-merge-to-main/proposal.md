# Proposal

## Why

The owner decided on 2026-11-22:
- Releases are verified on UAT **after** they merge; there is no pre-merge environment.
- Ticket PRs into `release/r-n` are auto-merged (`RELEASE_AUTO_MERGE` on).
- The pipeline itself merges the release PR into main.

Today the pipeline opens the release PR and a human must merge it, so a finished release waits on
the owner even though its tickets passed tests and review and its head is green.

repowise findings:
- `get_health`:
  - `dispatcher/daemon.py` is 1.0 (`run_pipeline` CCN 48, a hotspot with 3 recent fixes).
  - `dispatcher/pr.py` is 4.33 (a hotspot).
  - `dispatcher/releases.py` is 9.65.
  - The owner chose **build on top, new modules**: the merge decision and the messages live in
    `releases.py`, the gh call in `pr.py`, and `daemon.py` gets only call lines.
- `get_why(dispatcher/releases.py)`: no recorded decisions. The module docstring held the invariant
  "a PR into main is never merged by the pipeline", which this change narrows deliberately.

## What Changes

- New setting `RELEASE_MERGE_TO_MAIN` (default `false`; set `true` on the host).
- `releases.auto_merge_allowed()` stays the single decision point for every merge:
  - ticket PR → `release/r-n`: `RELEASE_AUTO_MERGE`;
  - release PR (`release/r-n` → `BASE_BRANCH`): `RELEASE_MERGE_TO_MAIN`;
  - ticket PR → main (DM ticket, hotfix): **never**, whatever the settings;
  - anything else: never.
- When the release PR is opened and the setting is on, the pipeline merges it once, with a merge
  commit at the tested head. If GitHub refuses (required checks pending), it turns on GitHub's
  auto-merge. If that is refused too, the tracker gets a comment and Slack says the owner is needed.
  The open PR is not retried.
- The existing close path is unchanged: the tracker goes to Done with `merged: <sha9> into main
  (<url>)`, and the branch is deleted.

## Impact

`get_risk`:

| File | Dependents | Hotspot | Fixes in 6 months |
|---|---|---|---|
| `dispatcher/daemon.py` | 4 | yes | 3 |
| `dispatcher/pr.py` | 5 | yes | 3 |
| `dispatcher/releases.py` | 4 | no | 0 |

Affected repos:
- **pipeline**: this change.
- **explorer**: no code change. It depends on the release being merged with a **merge commit**, and
  on the tracker comment `merged: <sha9> into main (<url>)`; both are kept. Its verdict follows on
  UAT after the merge, and per its own linked change it posts a commit status on that merge commit.
- **planner**: none. Its integration spec's "Release merge commit" and "Release tasks" requirements
  now name the pipeline as the one who merges.

Deployment: set `RELEASE_AUTO_MERGE=true` and `RELEASE_MERGE_TO_MAIN=true` in the host's `.env`.
Main's branch protection must let the pipeline's GitHub login merge with a merge commit. If main
has required checks, the repo must allow auto-merge.
