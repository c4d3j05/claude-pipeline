## ADDED Requirements

### Requirement: Lanes table
The Explorer tab SHALL show, above the Findings card, one row per entry of the `lanes` list in
`explorer status --json`, in the given order: the lane, its focus as `<batch> · <settled>/<total>` or `—`,
its last run as elapsed time, its next run as time remaining, today's spend as `$spent / $budget`, and its
open findings.

#### Scenario: Two lanes, one in focus
- **WHEN** billing has focus `{batch: "B-4", settled: 2, total: 6}` and accounts-contacts has `focus: null`
- **THEN** the billing row shows `B-4 · 2/6` and the accounts-contacts row shows `—`

#### Scenario: Spend against the share
- **WHEN** a lane has `spent_today: 3.2` and `budget: 10.0`
- **THEN** its row shows `$3.20 / $10.00`

### Requirement: Lanes table tolerates old and bad data
The Lanes table SHALL be absent when `lanes` is missing, is not a list, or is empty, and SHALL skip an
entry without a `lane` string. Lanes SHALL NOT change the Overview tab's alerts, spend or health.

#### Scenario: explorer before lanes
- **WHEN** `status --json` has no `lanes` key
- **THEN** the Explorer tab renders exactly as before, with no Lanes card

#### Scenario: A malformed entry
- **WHEN** `lanes` holds a number and one valid lane
- **THEN** the table shows only the valid lane
