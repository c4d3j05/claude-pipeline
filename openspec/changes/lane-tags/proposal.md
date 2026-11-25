# Proposal: release tags across explorer lanes

## Background (read this first; nothing outside this change is needed)

Three services cooperate on the target repo (Zentre, `ignify-ou/kauby`):

- **explorer** tests the UAT site, records *findings*, groups them into a **batch** (`B-1`, `B-2`, …,
  one global sequence) and sends the batch to planner.
- **planner** turns a batch into tickets in Vikunja plus one *release tracker* task titled
  `Release R-<n>` (one global sequence). The tracker's description starts with the line `Batch: B-<n>`.
- **pipeline** (this repo) builds each ticket, merges the ticket PRs into the branch `release/r-<n>`,
  then opens the *release PR* `release/r-<n>` → `main` (labelled `RELEASE_PR_LABEL`, default
  `claude-release`) and merges it with a merge commit. UAT deploys `main`.

After a release reaches UAT, explorer rechecks the batch's findings. When every finding is resolved or
dismissed the batch is **settled**; explorer then runs a *release check* (its regression scripts and
probes) on that UAT commit and posts a **GitHub commit status** on it:

- state `success` when the check passed, `failure` when it did not;
- description starting `B-<n> settled` (for example `B-4 settled; check passed: 5/5 scripts, probe 0 hard, state 12/12`);
- context `explorer/batch-settled` today.

`dispatcher/tagging.py` (built 2026-11-24, change `release-tags`) tags the newest first-parent commit of
`main` since the last `v*` tag whose latest `explorer/batch-settled` status is a success, as
`vYYYY.MM.DD`, creates a GitHub Release and posts one Slack line.

**What changes around it (integration decision D18, "module lanes").** explorer is being split into 7
**lanes**, one per product module: `accounts-contacts`, `engagements`, `documents-intake`, `billing`,
`integrations`, `portal`, `platform`. Each lane explores its own routes and has its own batch in flight,
so several batches and several releases can be open at once. The rollout has four phases: (1) lanes inside
explorer, (2) remote runners, (3) a UAT firm and personas per lane, (4) the tag rule in this change.
In phase 4 explorer posts the settled status under a context per lane, `explorer/batch-settled/<lane>`,
with the same description and states.

## Why

With several releases open, today's rule is wrong twice over:

1. One lane settling (say `B-5`, accounts-contacts) would tag a commit of `main` that also contains
   another lane's release (`R-6` for `B-6`, billing) that nobody has verified yet.
2. `settled_batches()` reads only the exact context `explorer/batch-settled`
   (`dispatcher/tagging.py:39,66-73`), so it would not see the per-lane contexts at all.

The owner's rule: **tag a commit only when every batch release merged into `main` up to that commit has
been settled and checked.**

## What Changes

1. `_release_body()` in `dispatcher/daemon.py` writes a line `Batch: B-<n>` into the release PR body,
   copied from the tracker description, so the batch of a release can be read from GitHub alone.
2. `dispatcher/tagging.py` reads every context `explorer/batch-settled/<lane>` and the legacy
   `explorer/batch-settled`.
3. It lists the merged release PRs and, for each one that names a batch and was merged inside the scanned
   range, requires a success status for **that batch id** at or after the merge.
4. It tags the newest commit that itself carries a success status and at which every such release is
   covered. The notes, the tag message and the Slack line name each batch with its lane, `B-4 (billing)`.
5. `claude-pipeline tag --dry-run` prints one `blocked by …` line per release that prevents a tag.

## Out of scope

- No Vikunja access from `tagging.py` (releases are read from GitHub only).
- No change to tag names, to the pending-tag retry, or to the alert-once behaviour.
- explorer posting the per-lane context (explorer change `lane-tags`) and planner writing `Lane:` on the
  tracker (planner change `batch-lane`); pipeline needs neither to be deployed first, because the legacy
  context keeps working and the lane is display only.
- Turning `RELEASE_TAGS` back on (a deploy step, see tasks 3.x).

## Impact

- pipeline: `dispatcher/tagging.py`, `dispatcher/daemon.py` (`_release_body`, its one call site in
  `release_pass`), `dispatcher/releases.py` (one helper), `tests/test_tagging.py`, `tests/test_releases.py`,
  `README.md`.
- explorer: posts `explorer/batch-settled/<lane>` (its own change). planner: none for this change.
- Risk: `tagging.py` is new code with a full scripted test double (`FakeRepo` in `tests/test_tagging.py`);
  every existing test there must keep passing unchanged except where task 2.1 says the double is extended.
