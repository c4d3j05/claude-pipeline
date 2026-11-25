# Proposal: a Lanes table on the dashboard's Explorer tab

## Background (read this first; nothing outside this change is needed)

pipeline serves a read-only web dashboard (`claude-pipeline web`, code in `dispatcher/web/`). Its
**Explorer** tab shows the output of the sibling service *explorer*, read by running
`uv run explorer status --json` in explorer's checkout (`dispatcher/web/sources.py`, `read_status`, which
requires `"schema": 1` in the JSON and caches the answer for `DASHBOARD_REFRESH_SEC`).

explorer tests the UAT site, records *findings*, and sends them to the planner in **batches** (`B-1`,
`B-2`, …). While a batch is being fixed and verified, explorer is "in focus" on it: it only rechecks that
batch's findings; the batch is *settled* when all are resolved or dismissed.

Integration decision D18 ("module lanes") splits explorer into 7 **lanes**, one per product module:
`accounts-contacts`, `engagements`, `documents-intake`, `billing`, `integrations`, `portal`, `platform`.
Each lane has its own routes, its own daily budget share, its own daily run time and its own batch in
focus. D18 rolls out in four phases; in phase 1 (explorer change `module-lanes`) explorer's
`status --json` gains a top-level **`lanes`** list, described below. This change shows it.

## Why

Today the tab shows one focus line and one spend figure for the whole of explorer. With lanes the owner
needs to see, per lane, whether it is in focus, when it last ran and will next run, and what it spent.

## What Changes

- New view helper `lanes(ex)` in `dispatcher/web/views.py` that turns the `lanes` list into table rows.
- New view helper `until(value)` in the same file, registered as a template filter, for a time in the future.
- The Explorer tab (`dispatcher/web/templates/explorer.html`) gets a **Lanes** card between the three stat
  cards and the **Findings** card.
- The table is absent when explorer sends no usable `lanes` list, so the dashboard works against explorer
  before and after its phase 1.

## Out of scope

- explorer producing the `lanes` list (explorer change `module-lanes`).
- Lanes in the Overview tab: `views.alerts()`, `views.spend()` and `views.health()` do not read lanes.
- Any control (pause a lane, run a lane): the dashboard stays read-only.
- A new status JSON schema version: `lanes` is an additive key and `"schema"` stays `1`.

## Impact

- pipeline: `dispatcher/web/views.py`, `dispatcher/web/app.py` (the `section` route and the filter list),
  `dispatcher/web/templates/explorer.html`, `tests/test_web.py`, `README.md` (the tab table).
- explorer: produces the list (linked change). planner: none.
