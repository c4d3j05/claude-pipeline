# Proposal: actions on the web dashboard

Status: READY TO BUILD. Pairs with explorer `run-request` (deploy that first; the "Run now" button
calls `explorer request-run`). Other buttons work without it.

## Background

The web dashboard (`claude-pipeline web`, unit `claude-pipeline-web`, code in dispatcher/web/) is
read-only by decision D15 ("controls come later as their own change"). It shows the board, pipeline
tickets, planner investigations, explorer findings/questions/batches and the systemd services
(Services tab, dispatcher/web/services.py). It reads explorer and planner through
`uv run <cli> status --json` in `DASHBOARD_EXPLORER_DIR` / `DASHBOARD_PLANNER_DIR` (dispatcher/web/sources.py).

On 2026-11-25 the owner spent an evening on the VM doing by hand what these actions do: answering
explorer escalation questions, stopping explorer to run it once and starting it again, and restarting
services. The owner decided: these actions, confirmed in the browser, logged, refused when unsafe, and
**no password for now** (the dashboard is on the local network only; a password may come later).

## What Changes

- POST routes under `/action/...` and buttons that call them (htmx, each with a confirmation):
  - explorer: **Run now** (`explorer request-run`, per lane or global in lane mode); **Answer** an escalation
    question (A/B/C with its option text); **Send / Hold / Unhold** a batch.
  - pipeline: **Retry** a blocked ticket (the existing `dashboard.retry_task`).
  - services: **Restart** a unit, refused while it is busy; the web service restarts itself after 2 s.
- Every action is appended to an action log and the Overview tab shows the last 20.
- `DASHBOARD_ACTIONS=true` (default) shows the buttons; `false` keeps the dashboard read-only.

## Out of scope

Passwords/auth; editing settings or findings; planner actions; Vikunja or GitHub writes other than
`retry_task`'s existing ones; restarting timers.

## Impact

pipeline: dispatcher/web/{app.py,actions.py (new),templates/*,sources.py}, dispatcher/config.py,
README, tests/test_web_actions.py (new). explorer: needs `request-run` and `options` on open questions
(explorer `run-request`). planner: none. The integration decision D15 is amended: actions are allowed.
