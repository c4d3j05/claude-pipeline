# Proposal

## Why

On 2026-11-23 the owner could not tell why the R-1 release PR was late without a shell on the VM.
`systemctl --user status claude-pipeline-dispatcher` answered it at once: the dispatcher was running
`ntree run release-r-1 -- scripts/worker-test.sh`, the release tests. The owner asked for that view,
for every service, on the web dashboard (integration decision D15: read-only, local network).

repowise findings:
- `get_context(dispatcher/web/sources.py)`: the sources sit behind one TTL cache (`Sources`), and a
  failing source becomes an error card. A new reader plugs in without touching the other four.
- `get_health`: `dispatcher/web/*` was added in `web-dashboard` and is healthy. The new code goes in a
  new module, `dispatcher/web/services.py`; `views.py` and `sources.py` gain call lines only.

## What Changes

- **New source `services`:** `systemctl --user show` for each unit in `DASHBOARD_UNITS`. It covers:
  - state and since when, main PID, memory, CPU and restarts
  - each timer's last and next run
  - the processes in each running unit's cgroup, read from `/sys/fs/cgroup` and `/proc`
- **New Services tab**, and an overview line: "N of M units active".
- **Overview alerts** for a unit that is not installed, has failed, or is enabled but not active.
- **Without systemd** (macOS), the tab says so instead of showing an error.

It is read-only: the only command is `systemctl --user show`. Nothing starts, stops or reloads a unit.

## Impact

`get_risk`: new module; `sources.py`, `views.py` and `app.py` change by a few lines each.

Affected repos:
- **pipeline:** this change.
- **planner and explorer:** none. Their units (`claude-planner`, `explorer-serve`,
  `explorer-nightly.timer`) are only read, by name.

The service runs as the same user, so `systemctl --user` reaches the other units with no new
permission.
