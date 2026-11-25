## ADDED Requirements

### Requirement: Settled status contexts
pipeline SHALL read a batch-settled commit status from the context `explorer/batch-settled` and from any
context `explorer/batch-settled/<lane>` where `<lane>` matches `^[a-z][a-z0-9-]{0,39}$`. The description
SHALL start `B-<n> settled`; state `success` means settled and checked. On one commit only the latest
status of each context counts.

#### Scenario: Two lanes on one commit
- **WHEN** a commit has a success `B-4 settled…` under `explorer/batch-settled/billing` and a success `B-5 settled…` under `explorer/batch-settled/accounts-contacts`
- **THEN** pipeline reads both batches, B-4 with lane billing and B-5 with lane accounts-contacts

#### Scenario: A newer failure in the same context
- **WHEN** the latest `explorer/batch-settled/billing` status on a commit is a failure and an older one is a success
- **THEN** that commit counts as not settled for billing

### Requirement: Release PR names its batch
The release PR body SHALL carry the line `Batch: B-<n>` copied from the release tracker's description
when the tracker has one. pipeline SHALL read a release's batch only from that line.

#### Scenario: Tracker with a batch
- **WHEN** the tracker of R-6 starts with `Batch: B-6`
- **THEN** the release PR body has the line `Batch: B-6` after its `Tracker:` line

### Requirement: Tags across lanes
pipeline SHALL tag the newest commit that carries a successful settled status and at which every batch
release merged at or before it, inside the scanned range, has a successful settled status for its own
batch id on a first-parent commit from its merge up to that commit. Lanes are not compared.

#### Scenario: One lane is not done
- **WHEN** B-5 is settled on `abc123` but R-6 (batch B-6), merged before `abc123`, has no success for B-6
- **THEN** `abc123` is not tagged

#### Scenario: Every merged release is covered
- **WHEN** `def456`, after both merges, has a success for B-5 and for B-6
- **THEN** pipeline tags `def456` and the notes begin `B-5 (accounts-contacts), B-6 (billing) settled`

#### Scenario: An older commit qualifies
- **WHEN** the newest settled commit is blocked by R-6 but an older settled commit predates R-6's merge and is covered
- **THEN** the older commit is tagged

### Requirement: What never blocks a tag
A merged release PR without a `Batch:` line, a release merged before the scanned range, and any merge that
is not a release PR (a DM ticket, a hotfix) SHALL NOT block a tag.

#### Scenario: A release from before this rule
- **WHEN** R-2's PR body has no `Batch:` line and a later commit has a success for B-9
- **THEN** that commit is tagged

### Requirement: Blocked tags are explained
`claude-pipeline tag --dry-run` SHALL print one line per release blocking the newest settled commit:
`blocked by R-<n> (B-<n>, PR #<number>, merged <sha9>): no successful explorer/batch-settled status for B-<n> since its merge`.
A real pass SHALL log the same lines and post nothing to Slack for them.

#### Scenario: Dry run while billing is unverified
- **WHEN** R-6 blocks the tag and the owner runs `claude-pipeline tag --dry-run`
- **THEN** the output names R-6, B-6, its PR number and merge commit, and the exit code is 0
