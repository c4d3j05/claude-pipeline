# claude-pipeline

Turn a Vikunja task marked **ready** into a reviewed pull request without anyone at
a terminal. One Claude Code worker per task, N tasks in parallel on one Mac, each in
its own [ntree](https://github.com/c4d3j05/ntree) git worktree with its own ports and
database namespace.

This repo is the **dispatcher** — the only new code in the design. The rest is Claude
Code, ntree, `gh`, and hooks wired together. See [`spec.md`](spec.md) and
[`claude_worker_pipeline.pdf`](claude_worker_pipeline.pdf) for the full design.

## What it does

- Polls Vikunja for tasks labelled `claude` in the **Ready** bucket, claims one per
  free worker slot, and runs a `claude -p` session per task inside a fresh worktree.
- Refuses to open a PR until the branch's **tests pass** and an independent
  **review** session signs off.
- Writes every state change back to Vikunja (bucket move + one comment) and Slack.
- Caps spend per task (turns + wall-clock) and per day.

It deliberately does **not** merge (a human does), coordinate workers (tasks are
independent), or run in the cloud (everything is local).

## Flow

```
Vikunja (Ready + label:claude)
   → claim (move to In Progress, comment "dispatched: …")
   → ntree new task-<id>-<slug>            # worktree + ports + deps
   → claude -p (--permission-mode acceptEdits, Stop hook runs tests)
   → GATE 1: dispatcher runs tests         # up to 2 red retries
   → working-tree check (commit or discard)
   → GATE 2: adversarial review (cheaper model, read-only)  # up to 1 block retry
   → git push + gh pr create               # move to Review, comment "pr: <url>"
   → human merges  → cleanup → Done / Blocked
```

## Install

```bash
cd claude-pipeline
uv sync
cp .env.example .env && $EDITOR .env      # Vikunja token, project id, target repo, Slack
uv run claude-pipeline selftest           # verify Vikunja, buckets, ntree, gh, claude
```

### Prepare a target repo

A repo is eligible once it has a `CLAUDE.md`, an `.ntreerc` with
`NTREE_WORKSPACE`-aware DB naming, and `.claude/worker-settings.json`:

```bash
TARGET_REPO_PATH=/path/to/repo uv run claude-pipeline prepare-repo
# then edit CLAUDE.md (layout, test command, isolation rules) and .ntreerc
```

The target repo's Vikunja project needs a **Kanban** view with buckets named
`Ready`, `In Progress`, `Review`, `Done`, `Blocked` (names configurable in `.env`).

### Run

```bash
uv run claude-pipeline run                # foreground
./scripts/install.sh                      # or: install launchd agents (daemon + nightly 03:00)
```

## Commands

| Command | What it does |
|---|---|
| `run` | Start the dispatcher daemon (poll → claim → work → gate → PR → write-back). |
| `selftest` | Verify config, Vikunja connectivity, bucket names, and required tools. |
| `cleanup` | Run one PR-close cleanup pass (teardown + Vikunja Done/Blocked) and exit. |
| `nightly` | Run the nightly maintenance pass (`ntree doctor` + report). |
| `prepare-repo` | Drop `worker-settings.json` + `CLAUDE.md` template into `TARGET_REPO_PATH`. |

## Authoring tasks

How to scope and structure tasks so a batch runs in parallel safely (conflicts surface at
*merge*, so scoping is everything) and each task yields a clean PR:
**[`AUTHORING.md`](AUTHORING.md)** — includes a copy-paste ticket template.

## Configuration

All via `.env` (see [`.env.example`](.env.example)). Key knobs: `MAX_WORKERS` (4),
`MAX_TURNS` (60), `WALL_CLOCK_LIMIT_MIN` (45), `MAX_TEST_RETRIES` (2),
`MAX_REVIEW_RETRIES` (1), `DAILY_CAP_USD` (50), `WORKER_MODEL` / `REVIEW_MODEL`
(the review runs the cheaper model).

## Status write-back

| Event | Bucket | Comment |
|---|---|---|
| Claimed | In Progress | `dispatched: session=… pid=… workspace=…` |
| Tests failed (retry n) | In Progress | `gate: tests red, retry n/2` |
| Review blocked | In Progress | `gate: review blocking, retry` |
| PR opened | Review | `pr: <url>` |
| Worker wrote BLOCKED / cap hit | Blocked | `blocked: <reason>` / `cap: …` |
| PR merged / closed | Done / Blocked | `merged: <sha>` / `closed without merge` |

## Layout

```
dispatcher/        the daemon (poll, claim, prompt, worker, gate, review, PR, cleanup)
  config.py        env-driven configuration + validation
  db.py            SQLite ledger (restart recovery + cost)
  vikunja.py       REST client (buckets, moves, comments, labels)
  ntree.py         thin wrapper over the ntree contract (worktree/ports/env)
  prompt.py        slug derivation + worker/review prompt construction
  worker.py        launch `claude -p`, stream-json capture, wall-clock kill
  stream.py        parse stream-json (session id, cost, turns, denials, summary)
  review.py        the gate: independent tests + adversarial review session
  pr.py            git push + `gh pr create` + PR body assembly
  cleanup.py       PR-close teardown + nightly maintenance
  daemon.py        the main loop and per-task pipeline
templates/         worker-settings.json + CLAUDE.md template dropped into target repos
launchd/           dispatcher (KeepAlive) + nightly (03:00) agents
scripts/install.sh install the launchd agents
tests/             unit tests for the pure logic
```

## Development

```bash
uv run ruff check dispatcher tests
uv run pytest
```
