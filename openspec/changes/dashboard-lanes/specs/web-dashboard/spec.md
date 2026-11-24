## ADDED Requirements

### Requirement: Lanes table
The Explorer tab SHALL show one row per explorer lane from the `lanes` block of `explorer status
--json`: its focus (batch and settled of total), last run, today's spend against its share, open
findings and next scheduled run. Without a `lanes` block the table SHALL be hidden.

#### Scenario: Two lanes, one in focus
- **WHEN** billing is in focus on B-4 (2 of 6 settled) and accounts-contacts is not
- **THEN** the billing row shows "B-4 · 2/6" and the accounts-contacts row shows no focus

#### Scenario: explorer before lanes
- **WHEN** `status --json` has no `lanes` block
- **THEN** the Explorer tab looks as it does today
