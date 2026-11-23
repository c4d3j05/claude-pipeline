# Design

## Owner decisions (2026-11-22)

| Question | Decision |
|---|---|
| What is it? | A dashboard, not a console: it shows and never acts. Controls may come later as their own change. |
| How is it reached? | From the local network: `DASHBOARD_HOST=0.0.0.0` on the VM. No login. |
| Stack and home | Python + HTMX, in claude-pipeline. |
| What it shows | Each service's history and status: workers and tickets, what was filed, findings, questions, runs, batches, Vikunja tasks. |
| Refactor first or build on top? | Build on top, in new modules. |

## Shape

```
dispatcher/web/
  sources.py   Result, Sources (TTL cache, one lock per source), read_pipeline, read_board, read_status
  views.py     health, spend, alerts, findings filter, ago/money (pure functions)
  app.py       create_app(cfg, sources) -> FastAPI; serve(cfg) -> uvicorn
  templates/   page.html (shell) + one partial per tab + _macros.html
  static/      htmx.min.js (2.0.4, vendored), style.css
```

- **Readers are injectable:** `Sources(cfg, readers=…)`, so tests run without a ledger, Vikunja or
  subprocesses.
- **`create_app` takes the `Sources`.** The CLI builds the real one.

## Data rules

- **Nothing writes.** Only GET routes are defined; a POST gets 405.
  - The ledger is opened per read and closed again. `Ledger()` runs its idempotent `CREATE TABLE IF
    NOT EXISTS`, as the terminal dashboard does.
  - The board is read through `kanban_view_id` and `board`.
- **Status JSON:** `uv run --quiet <cli> status --json` with `cwd` set to the configured checkout and
  a 20 s timeout.
  - Accepted when `schema == 1`; unknown fields are ignored.
  - Anything else becomes a short reason on an error card: a missing directory, uv missing, a
    non-zero exit, output that isn't JSON, another schema, or a timeout.
- **Caching:**
  - Each source is cached for `DASHBOARD_REFRESH_SEC`, and its lock means one read at a time; any
    number of browsers costs one read per source per period.
  - The overview reads all four sources through the same cache.
- **Links:** only `http(s)` URLs become `href`s (the `url` filter). Anything else, e.g. a
  `javascript:` permalink, is shown as text. Jinja autoescaping is on for every template.
- **Times:** a timestamp with no zone is the host's local time, as the ledger writes it. Status JSON
  uses ISO 8601 with an offset.

## Alerts (overview)

| Alert | Level | Source |
|---|---|---|
| A source that cannot be read | bad | any |
| Dispatching paused | warn | pipeline |
| Blocked tasks | warn | pipeline |
| Daily cap reached | bad | pipeline |
| Slack listener not running (`listener.running == false`) | bad | planner |
| main ahead of UAT | warn; bad after 20 min | explorer (`uat.main_head` ≠ `uat.commit` and `uat.ahead_since`) |
| Runs paused | warn | explorer |
| A sent batch with `resends > 0` | warn | explorer |
| Escalated findings waiting for the owner | warn | explorer |

## Not covered

- **Controls:** pause, cancel, retry, resend. Deferred by decision.
- **Authentication.** Out of scope by decision; keep the port on a trusted network.
- **Pipeline history:** shown from the ledger's rows, i.e. each ticket's current state and
  timestamps. The ledger keeps no event log, and the Vikunja comments remain the full trail.
