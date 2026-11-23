## ADDED Requirements

### Requirement: Read-only dashboard
`claude-pipeline web` SHALL serve a dashboard on `DASHBOARD_HOST:DASHBOARD_PORT` (default
127.0.0.1:8090) that only reads: GET routes only, no change to the ledger, Vikunja, claude-planner or
explorer.

#### Scenario: A write is refused
- **WHEN** a client sends POST to any dashboard route
- **THEN** the server answers 405 and nothing changes

### Requirement: Factory-wide tabs
The dashboard SHALL show five tabs, each refreshed by HTMX every `DASHBOARD_REFRESH_SEC`:
- Overview: health, spend against caps, and alerts
- Board: Vikunja tasks by bucket
- Pipeline: workers and tickets
- Planner: investigations and filed tickets
- Explorer: UAT commit, runs, findings by state, questions, batches and hotfixes

#### Scenario: Escalated findings
- **WHEN** explorer reports a finding in state `escalated`
- **THEN** the overview shows an alert, and the Explorer tab can be filtered to escalated findings

### Requirement: Status JSON sources
The dashboard SHALL read claude-planner and explorer by running `uv run <cli> status --json` in
`DASHBOARD_PLANNER_DIR` and `DASHBOARD_EXPLORER_DIR`. It SHALL accept only `schema` 1, SHALL ignore
fields it does not know, and SHALL reuse each source's answer for `DASHBOARD_REFRESH_SEC`.

#### Scenario: Many browsers
- **WHEN** five browsers refresh within one period
- **THEN** each source is read once

### Requirement: A broken source does not break the page
A source that is not configured, cannot be run, times out, exits non-zero, prints no JSON or reports
another schema SHALL be shown as a card with the reason. The rest of the dashboard SHALL keep working.

#### Scenario: explorer unreachable
- **WHEN** `explorer status --json` times out
- **THEN** the Explorer tab shows "could not be read" with the reason, and the overview raises an alert
