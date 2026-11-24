# Proposal

## Why

On 2026-11-24 the owner asked how the Zentre repo gets tagged once everything is done and verified.
Nothing tags it today: kauby has no tags and no GitHub releases. The owner decided (integration
D17):
- a tag is due when a **batch is settled**: explorer has verified on UAT that every finding of the
  batch is resolved or dismissed (one batch may need several releases: B-1 took R-1 and R-2);
- tags are **date-named**: `v2026.11.24`, `.2` for a second one the same day;
- **pipeline** creates them, since it already has write access to the repo; explorer's GitHub token
  stays limited to commit statuses;
- each tag gets a **GitHub Release** with notes and a **Slack line**, and production can deploy from
  `v*` tags later.

repowise findings:
- `get_overview` / `get_context(dispatcher/daemon.py)`: the daemon's tick already runs periodic,
  slow passes in the worker pool behind a running flag (`release_pass`); a tag pass fits the same
  shape. `get_context(dispatcher/releases.py)`: `ReleaseGit._git` runs git with `clean_env` and the
  pipeline's identity; tagging reuses both.
- `get_why(dispatcher/releases.py)`: release merges follow the owner's 2026-11-22 decisions, and
  every write is guarded (`auto_merge_allowed`). No recorded decision covers tags.
- `get_health`: `dispatcher/daemon.py` is 1.0/10 (complex `run_pipeline`, change entropy). Per the
  owner's standing choice ("build on top, new modules") the logic goes in a new module,
  `dispatcher/tagging.py`; daemon.py gains a call line and two small methods.

## What Changes

- **New module `dispatcher/tagging.py`:** reads `explorer/batch-settled` statuses on the base
  branch's first-parent commits since the last `TAG_PREFIX` tag, tags the newest settled one
  (annotated), pushes it, creates a GitHub Release and posts one Slack line. A failure is alerted
  once per commit and retried; a tag created but not yet pushed or released is finished first.
- **Settings:** `RELEASE_TAGS` (default off, hot-reloaded), `TAG_PREFIX` (`v`), `TAG_SCAN_COMMITS`
  (30), `TAG_POLL_MIN` (10).
- **CLI:** `claude-pipeline tag` (one pass now), `--dry-run` (prints what it would tag).
- **Ledger:** a small `meta` table for the alert-once and pending markers.

## Impact

`get_risk`:
- `dispatcher/daemon.py`: hotspot (98%), 3 bug fixes in 6 months, health 1.0. Touched by a call
  line in `tick` and two short methods (`_maybe_tag`, `_safe_tag_pass`).
- `dispatcher/config.py`: 14 dependents, co-changes with `.env.example`. Four new fields with
  defaults; nothing existing changes.
- `dispatcher/db.py`: 13 dependents. One new table (`CREATE TABLE IF NOT EXISTS`) and two methods.

Affected repos:
- **pipeline:** this change.
- **explorer:** posts the `explorer/batch-settled` commit status when a batch settles (its own change,
  `release-tags`); its token stays statuses-only.
- **planner:** owns the contract; the integration spec gains requirement "Release tags" and D17.
