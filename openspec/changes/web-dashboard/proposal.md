# Proposal

## Why

To see what the factory is doing, the owner has to log into the Debian VM and run a separate
command for each of the three services. That means `claude-pipeline dashboard`, `claude-planner
status`, and `explorer status` and `explorer batch status`, plus Vikunja's own UI for the board.

The owner decided on 2026-11-22:
- a **read-only web dashboard**, reached from the local network
- built with **Python + HTMX** and living **in claude-pipeline**
- showing the history and status of each service: workers and tickets, what planner filed,
  explorer's findings, questions, runs and batches, and the Vikunja tasks
- planner and explorer feed it through a `status --json` command each (linked changes); their
  databases stay private
- **build on top, in new modules**

repowise findings:
- `get_overview(pipeline)`: Python dispatcher. The only existing UI is the terminal dashboard
  (`dashboard.py` with `tui.py`, Textual).
- `get_context(dispatcher/dashboard.py)`: "Terminal control panel for the pipeline". `gather_status`
  reads the ledger and each worker's log, and is reused here unchanged. The rest of the module
  (`toggle_pause`, `retry_task`, `request_cancel`, `set_config`) is controls, and the web dashboard
  does not import those.
- `get_why(dispatcher/dashboard.py)`: no recorded decisions.
- `get_health`:

  | File | Health | Notes |
  |---|---|---|
  | `dashboard.py` | 6.99 | `_last_message` nests 4 levels |
  | `config.py` | 7.71 | |
  | `__main__.py` | 5.34 | 30% line coverage |
  | `vikunja.py` | 7.49 | |
  | `db.py` | 8.85 | |

  The new code goes in a new package, `dispatcher/web/`. Existing files get only a settings block,
  a CLI command and docs.

## What Changes

- A new command, `claude-pipeline web`, serves a FastAPI + Jinja2 + HTMX app with uvicorn. It
  depends on a new optional dependency group, `web`; htmx is vendored and the CSS is plain, so there
  is no CDN and no build step.
- **Tabs:** Overview, Board, Pipeline, Planner and Explorer. Each tab is a shell, and HTMX polls its
  section every `DASHBOARD_REFRESH_SEC`.
- **Extra routes:** `/api/<source>.json` (passthrough) and `/healthz`.
- **Sources:**
  - pipeline: the ledger, via `gather_status`
  - board: the Vikunja board, read with GETs only
  - planner and explorer: `uv run <cli> status --json` in their checkouts, schema 1

  Each answer is cached for `DASHBOARD_REFRESH_SEC`. A failing source becomes an error card and never
  breaks the page.
- **Settings:** `DASHBOARD_HOST` (127.0.0.1), `DASHBOARD_PORT` (8090), `DASHBOARD_PLANNER_DIR`,
  `DASHBOARD_EXPLORER_DIR`, and `DASHBOARD_REFRESH_SEC` (15, minimum 5).
- **Deployment:** `systemd/claude-pipeline-web.service`. `scripts/install.sh` installs it but doesn't
  enable it.
- **Docs:** README.

## Impact

`get_risk`:

| File | Dependents | Hotspot | Fixes | What changes |
|---|---|---|---|---|
| `dispatcher/config.py` | 11 | yes | 2 | 5 new fields with defaults, appended only |
| `dispatcher/dashboard.py` | 4 | no | | not changed |
| `dispatcher/__main__.py` | 1 | no | 1 | one command |

`daemon.py` is not touched, and the dispatcher's behaviour does not change.

Affected repos:
- **pipeline**: this change.
- **planner**: linked change adding `claude-planner status --json` (schema 1).
- **explorer**: linked change adding `explorer status --json` (schema 1).
- **Integration spec** (planner): gains a "Status JSON" section with both shapes. A shape changes
  only with a linked change here.

There is no login, so the data on the page (tasks, findings, costs, Slack and Vikunja links) is
visible to anyone who can reach the port. The default bind is 127.0.0.1, and the owner sets 0.0.0.0
on the LAN-only VM.
