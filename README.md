# claude-pipeline

Turn a Vikunja task marked **ready** into a reviewed pull request without anyone at
a terminal. One Claude Code worker per task, N tasks in parallel on one machine (macOS or Linux), each in
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
./scripts/install.sh                      # or: install as a service (daemon + nightly 03:00)
                                          #   macOS → launchd agents, Linux → systemd --user units
```

### Linux (Debian/Ubuntu)

Needs `git`, `gh`, `jq`, `lsof`, `netcat-openbsd`, `bubblewrap`, `socat` (Claude Code's
sandbox on Linux; the worker settings set `failIfUnavailable`), `uv`, the `claude` CLI, and a Linux-capable
[ntree](https://github.com/c4d3j05/ntree) (branch `feature/linux-support` or later).
`scripts/install.sh` installs `systemd --user` units from `systemd/`. User services stop at
logout unless you run `sudo loginctl enable-linger $USER`. Logs: `journalctl --user -u
claude-pipeline-dispatcher -f` or `logs/dispatcher.err.log`.

## Limits and recovery

- **Budget:** `TASK_CAP_USD` (default 10) covers the worker, retries *and* the review; the
  daily cap counts everything. Cost history survives a retry (`lifetime_cost`).
- **Capacity:** test gates are CPU/DB heavy, so at most `MAX_GATES` (default 1) run at once
  and each is cut off after `GATE_TIMEOUT_MIN` (default 30) with its **whole process tree**
  stopped. Size `MAX_WORKERS` to the machine (default 2). The worker's own Stop-hook gate runs
  inside `claude` and is not queued; keep its `timeout` above your gate's duration.
- **Baseline check:** before a worker is launched, the gate runs once on the untouched base
  commit (result cached per SHA, `BASELINE_CHECK=false` to disable). A red base blocks the
  task for free instead of a worker "fixing" an environment problem.
- **Credentials are scoped** per subprocess, read fresh from `~/.worker.env` for every task
  (`WORKER_ENV_FILE` to override): the Claude token reaches only `claude`; `GH_TOKEN` reaches
  only `gh` and git operations against the remote; the test gate, `ntree` and local git get
  none (the gate runs worker-written code). The template also denies workers `Read` access to
  `~/.worker.env`, `~/.ssh`, `~/.aws`, `~/.config/gh` and similar.
- **Failures:** a transient API/network error is retried with backoff (`TRANSIENT_RETRIES`);
  a credentials problem (not logged in, bad key) is not retried and **pauses dispatching**
  with an alert. Any other errored session blocks with the real reason. Permission denials
  come from the result event, so a shell printing "Permission denied" does not count.
- **Cancel** is honoured at every phase: before the first worker launch, between gate and
  review, and while a worker runs. (A cancel during the minutes-long `ntree new` setup takes
  effect as soon as setup returns.)
- **Gates re-verify:** tests and a clean working tree are re-checked after every worker turn;
  a truncated review diff is flagged. Workers fork from a freshly fetched `origin/<base>`, and
  a failing `NTREE_SETUP_CMD` fails the task.
- **Git locks:** workers are stopped with SIGTERM + a 15 s grace (git removes its own locks on
  SIGTERM); stale locks are swept at startup/nightly; `ntree new` retries on a lock error.
- **Restart:** interrupted tasks go to *Blocked*; drag the card back to **Ready** to retry with
  a fresh budget. One dispatcher per `PIPELINE_DB`. Stopping drains running tasks; a second
  SIGTERM/Ctrl-C stops the workers and exits.
- **Open PRs are watched:** merge conflicts, failing CI and requested changes are posted to the
  card (and Slack) once per change. Closing or merging a PR tears down its workspace; only the
  *current* attempt's PR can do that.
- **Alerts:** 3 failed polls in a row (expired Vikunja token, API down) alert via Slack.
- **Housekeeping (nightly):** blocked workspaces after `BLOCKED_RETENTION_DAYS`, task logs after
  `LOG_RETENTION_DAYS`, stale git locks.
- `prepare-repo` renders the worker's Stop hook from `TEST_CMD`; **commit** the generated
  files to the base branch (`selftest` checks this).

Not covered: automatically *fixing* a PR after CI/review feedback (the human is notified, the
pipeline does not act), rebasing a PR when the base moves, and sandboxing the dispatcher's own
test run beyond removing credentials from it.

## Commands

| Command | What it does |
|---|---|
| `run` | Start the dispatcher daemon (poll → claim → work → gate → PR → write-back). |
| `selftest` | Verify config, Vikunja connectivity, bucket names, and required tools. |
| `cleanup` | Run one PR-close cleanup pass (teardown + Vikunja Done/Blocked) and exit. |
| `nightly` | Run the nightly maintenance pass (`ntree doctor` + report). |
| `prepare-repo` | Drop `worker-settings.json` + `CLAUDE.md` template into `TARGET_REPO_PATH`. |
| `dashboard` | Terminal control panel: live worker status + edit config + pause/retry/cancel (`--once` for a one-shot text snapshot). |
| `web` | Read-only web dashboard for the whole factory: pipeline, the Vikunja board, claude-planner and explorer. |
| `tag` | One release-tag pass now (`RELEASE_TAGS`); `--dry-run` prints the tag, commit, batches and notes it would create. |

## Dashboard

A Textual TUI control panel (`uv sync --group dashboard` once to install it):

```bash
uv run claude-pipeline dashboard          # interactive TUI
uv run claude-pipeline dashboard --once   # one-shot text status (no TUI)
```

It reads the same SQLite ledger the daemon writes, so status is always accurate.
Keys: `p` pause/resume dispatching · `r` retry a blocked task · `c` cancel an in-flight
task · `o` open the selected PR · `e` edit config · `q` quit. Config edits (`MAX_WORKERS`,
`DAILY_CAP_USD`, `POLL_INTERVAL`, models, permission mode) are written to `.env` and the
daemon **hot-reloads them each tick** — no restart, no interrupted workers. Controls go
through a `control` row / `cancel_requested` flag in the ledger that the daemon honors.

## Web dashboard

A read-only page for the whole factory, so you can see what is going on without logging into the
host (OpenSpec change `web-dashboard`). One tab per area, each refreshed by HTMX every
`DASHBOARD_REFRESH_SEC`:

| Tab | Shows |
|---|---|
| Overview | Each service's health, today's spend against its cap, and alerts: blocked tasks, a paused dispatcher, main ahead of UAT, a batch the planner has not picked up, findings waiting for your answer, a service that cannot be read |
| Board | The Vikunja board by bucket, release trackers first, each task linked |
| Pipeline | Busy workers (stage, elapsed, spend, last activity) and every ticket's history |
| Planner | Open investigations and what was filed, with links to the Slack thread and the Vikunja tasks |
| Explorer | UAT's commit, runs, findings filtered by state (including escalated), open and answered questions, batches and hotfixes |
| Services | What `systemctl --user status` shows for every unit in `DASHBOARD_UNITS`: state, since when, PID, memory, CPU, restarts, timers' last and next run, and the processes running inside each one (a release test, a worker session). Read-only |

```bash
uv sync --group web                 # once
uv run claude-pipeline web          # http://DASHBOARD_HOST:DASHBOARD_PORT
```

It never writes: the ledger and Vikunja are only read, and claude-planner and explorer are asked for
`status --json` (schema 1, in planner's integration spec) by running `uv run <cli> status --json` in
`DASHBOARD_PLANNER_DIR` and `DASHBOARD_EXPLORER_DIR` on the same host. Each answer is reused for
`DASHBOARD_REFRESH_SEC`, so open browsers do not multiply the work. A service that cannot be read
shows an error card; the rest of the page keeps working. `/api/<source>.json` returns the raw data
and `/healthz` answers for a monitor.

**There is no login.** Anyone who can reach the port sees tasks, findings and costs. On a VM that
only your local network reaches, set `DASHBOARD_HOST=0.0.0.0`; otherwise keep `127.0.0.1` and use an
SSH tunnel. On Linux, `scripts/install.sh` installs `claude-pipeline-web.service` but leaves it off:

```bash
systemctl --user enable --now claude-pipeline-web.service
```

## Authoring tasks

How to scope and structure tasks so a batch runs in parallel safely (conflicts surface at
*merge*, so scoping is everything) and each task yields a clean PR:
**[`AUTHORING.md`](AUTHORING.md)** — includes a copy-paste ticket template.

## Configuration

All via `.env` (see [`.env.example`](.env.example)). Key knobs: `MAX_WORKERS` (4),
`MAX_TURNS` (60), `WALL_CLOCK_LIMIT_MIN` (45), `MAX_TEST_RETRIES` (2),
`MAX_REVIEW_RETRIES` (1), `DAILY_CAP_USD` (50), `WORKER_MODEL` / `REVIEW_MODEL`
(the review runs the cheaper model).

## Releases

A ticket whose description has `Release: R-n · branch release/r-n` builds on that release branch,
not on main. claude-planner files these for a batch of explorer findings: a "Release R-n" tracking
task (label `release-tracker`, never claimed) with every ticket as its subtask. The full contract is
planner's integration spec (`openspec/specs/integration/spec.md`); [`RELEASES.md`](RELEASES.md)
describes what this repo does with it and which settings turn it on.

Who merges what (the owner's decisions of 2026-11-22): ticket PRs into `release/r-n` are merged by
the pipeline with `RELEASE_AUTO_MERGE=true`; the release PR into `main` is merged by the pipeline,
with a merge commit, with `RELEASE_MERGE_TO_MAIN=true`. A release is then verified by explorer on
UAT after it merges. A ticket PR into `main` (a DM ticket, a hotfix) is always a human's to merge.

## Release tags

With `RELEASE_TAGS=true` the target repo is tagged once a batch is settled: when explorer has
verified on UAT that every finding of a batch is resolved or dismissed, it marks that commit with
the commit status `explorer/batch-settled`. At most every `TAG_POLL_MIN` the dispatcher reads the
statuses of the base branch's first-parent commits since the last `TAG_PREFIX` tag, and on the
newest settled one creates an annotated tag named after the local date (`v2026.11.24`, `.2` for a
second the same day), pushes it, creates a GitHub Release (the settled batches, then every PR merged
since the previous tag) and posts one line in Slack. A failure is alerted once and retried.

A tag is never moved or deleted, and only commits on the first-parent history of the base branch
are tagged. So `v*` tags exist only on verified, settled commits, and a production deploy can
follow tags later. `claude-pipeline tag --dry-run` shows what the next pass would do. The contract
is D17 in planner's integration spec.

## Status write-back

| Event | Bucket | Comment |
|---|---|---|
| Claimed | In Progress | `dispatched: session=… pid=… workspace=…` |
| Tests failed (retry n) | In Progress | `gate: tests red, retry n/2` |
| Review blocked | In Progress | `gate: review blocking, retry` |
| PR opened | Review | `pr: <url>` |
| Worker wrote BLOCKED / cap hit | Blocked | `blocked: <reason>` / `blocked: cap: …` |
| PR merged / closed | Done / Blocked | `merged: <sha>` (`merged: <sha> into release/r-n` for a release ticket) / `closed without merge` |
| Release branch conflicts with main | Ready | `waiting: release R-n: …; repair task #N` |

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
  pr.py            git push + `gh pr create` + PR body assembly + release merges and PRs
  releases.py      release branches: Release lines, sync with main, repairs, the auto-merge guard
  tagging.py       release tags on commits explorer marks `explorer/batch-settled` (D17)
  cleanup.py       PR-close teardown + nightly maintenance
  daemon.py        the main loop and per-task pipeline
  dashboard.py     terminal dashboard data + controls (gather_status)
  web/             read-only web dashboard: sources (cache), views (alerts), app (FastAPI + HTMX)
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
