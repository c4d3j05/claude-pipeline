# Proposal (module lanes, D18; ships with explorer phase 1)

## Why

explorer will explore in 7 lanes, each with its own batch in focus, budget share and schedule (D18).
The dashboard's Explorer tab shows one global focus line today. The owner decided (Q36) to add a Lanes
table, read from a `lanes` block that explorer's `status --json` gains in its `module-lanes` change.

repowise findings: `dispatcher/web/sources.py` reads `explorer status --json` through `CLIS` and
caches each answer for `DASHBOARD_REFRESH_SEC`; `views.py` shapes it; `templates/explorer.html` renders
it. Additive fields keep the status JSON schema version.

## What Changes

- The Explorer tab gains a **Lanes** table: lane, focus (batch and settled/total), last run, today's
  spend against the lane's share, open findings, next scheduled run.
- Without a `lanes` block (explorer before phase 1) the table is hidden and the tab is unchanged.

## Impact

- pipeline: `dispatcher/web/views.py`, `dispatcher/web/templates/explorer.html`, `tests/test_web.py`.
- explorer: `module-lanes` adds the `lanes` block (linked change). planner: none.
