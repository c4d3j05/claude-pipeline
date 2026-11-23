## ADDED Requirements

### Requirement: Services tab
The dashboard SHALL show, for each unit in `DASHBOARD_UNITS`, what `systemctl --user status` shows:
- its state and since when, main PID, memory, CPU and restarts
- a timer's last and next run
- the processes running in the unit's cgroup

It SHALL only read: `systemctl --user show` and files under `/sys/fs/cgroup` and `/proc`. On a host
without systemd it SHALL say so instead of failing.

#### Scenario: Release tests running
- **WHEN** the dispatcher is running a release's tests
- **THEN** the Services tab lists `ntree run release-r-1 -- scripts/worker-test.sh` under `claude-pipeline-dispatcher`

#### Scenario: Service down
- **WHEN** `explorer-serve` is enabled but has failed
- **THEN** the Overview shows an alert that it has failed, with systemd's result

#### Scenario: No systemd
- **WHEN** the dashboard runs on macOS
- **THEN** the Services tab says there is no systemd on this host
