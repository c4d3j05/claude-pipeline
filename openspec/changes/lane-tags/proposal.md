# Proposal (phase 4 of module lanes, D18)

## Why

explorer will explore in 7 lanes, each with its own batch in focus (integration D18). Today pipeline
tags the newest `main` commit carrying any successful `explorer/batch-settled` status
(`dispatcher/tagging.py:39`, only the latest status per context counts at `:66-74`, batch ids parsed
from `^(B-\d+) settled` at `:40`). With lanes, two lanes on one commit would overwrite each other's
status, and one lane settling would tag a commit that also carries another lane's merged but
unverified release. The owner decided (Q11): tag a commit only when every release merged into `main`
up to it has its batch settled and its release check passed.

repowise findings:
- `context dispatcher/tagging.py`: built 2026-11-24 (release-tags), one pass per `TAG_POLL_MIN`,
  pending tag kept in the ledger `meta` table.
- `why`: release-tags and release-check (D17 amended) govern it; nothing assumes more than one context.

## What Changes

- Read every status context `explorer/batch-settled/<lane>` (and the legacy `explorer/batch-settled`).
- Know which releases were merged into `main` up to a commit (the release trackers' `merged: <sha>`
  comments and the release PRs' merge commits) and which batch/lane each release came from (the
  tracker's `Batch: B-n` line).
- Tag commit C only when, for every such release, the batch's lane has a `success` status on C or on
  an earlier first-parent commit after that release's merge. Notes list every settled batch and lane.
- `claude-pipeline tag --dry-run` names any release still blocking the tag.

## Impact

- pipeline: `dispatcher/tagging.py`, tests. Risk: moderate, the module is one day old.
- explorer: `lane-tags` posts the per-lane context (linked change).
- planner: integration spec "Release tags across lanes" (D18), already recorded.
- Deploy: `RELEASE_TAGS` stays off from explorer's phase 1 until this and explorer `lane-tags` are live.
