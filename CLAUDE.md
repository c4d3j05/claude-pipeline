# claude-pipeline — worker context

The dispatcher that turns Vikunja tasks into reviewed PRs. Design lives in `spec.md`
and `claude_worker_pipeline.pdf`. This is the *pipeline itself*, not a target repo.

## Layout
- `dispatcher/` — the daemon. `daemon.py` is the main loop + per-task pipeline; the
  other modules are single-responsibility (see the table in `README.md`).
- `db.py` — SQLite ledger (restart recovery + cost). Vikunja is the queue's source of truth.
- `templates/` — files copied into *target* repos (`worker-settings.json`, `CLAUDE.md`).
- `launchd/`, `scripts/` — ops.
- `tests/` — unit tests for the pure logic (no network/subprocess).

## Run tests
```bash
uv run pytest           # whole suite
uv run pytest tests/test_pipeline.py::test_slugify_and_task_slug
```

## Conventions
- Python ≥3.11, `uv` for env + running. Only runtime dep is `requests`.
- Formatter/linter: `ruff` (`uv run ruff check dispatcher tests`). Line length 100.
- Type hints throughout; `from __future__ import annotations` at the top of each module.
- Workers must never raise on subprocess failure — inspect `returncode` (ruff PLW1510
  is intentionally ignored).

## Isolation
This repo does not bind ports or a DB itself. When operating a target repo, the
dispatcher relies on `ntree` to provide `NTREE_WORKSPACE` and `$PORT`.

## Do not touch
- `spec.md`, `claude_worker_pipeline.pdf` — the source of truth for the design.
- `.env` — secrets (Vikunja token, Slack webhook).

## Definition of done
`uv run ruff check` clean, `uv run pytest` green, changes committed, nothing pushed.

## Blocked protocol
If a requirement is missing or ambiguous, write `BLOCKED: <reason>` and stop.
