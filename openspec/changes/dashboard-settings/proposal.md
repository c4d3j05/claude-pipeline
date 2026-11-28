# Proposal: a Settings tab on the dashboard (allow-listed, validated, logged)

Status: READY TO BUILD. Builds on `dashboard-actions` (its action log, its `DASHBOARD_ACTIONS` switch and
its Restart action).

## Background

Operational settings of the three services live in each checkout's `.env`: this repo's, the planner's
(`DASHBOARD_PLANNER_DIR/.env`) and explorer's (`DASHBOARD_EXPLORER_DIR/.env`). On 2026-11-25 the owner had to
edit `RECHECK_WALL_CLOCK_MIN` by hand on the VM in the middle of a release. The same files also hold secrets
(tokens, passwords, webhooks), so the dashboard must never show or change anything but an allow-list.

## What Changes

- A **Settings** tab listing an allow-list of operational settings per service with their current value (or
  "default"), a help line and whether a change needs a restart.
- Saving one setting validates it, backs the file up, rewrites only that line, logs the action, and says
  what to restart (with the existing Restart button). Pipeline's hot-reloaded settings apply without restart.
- No other line of any `.env` is read into a page, a response or a log.

## Out of scope

Secrets; adding new keys outside the allow-list; editing several keys at once; planner or explorer code.

## Impact

pipeline only: dispatcher/web/{settings.py (new), app.py, templates/settings.html (new), page tabs},
README, tests/test_web_settings.py (new). planner, explorer: their `.env` files are edited on the same host by
the owner's action; no code change there.
