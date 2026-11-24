## ADDED Requirements

### Requirement: Tags across lanes
pipeline SHALL tag a commit of `main` only when every release merged into `main` up to that commit
since the previous tag has a successful `explorer/batch-settled/<lane>` status for its batch's lane on a
first-parent commit between its merge and that commit. A legacy `explorer/batch-settled` status counts
for any lane.

#### Scenario: One lane is not done
- **WHEN** R-5 (accounts) is settled on `abc123` but R-6 (billing), merged before `abc123`, is not
- **THEN** `abc123` is not tagged, and `tag --dry-run` names R-6 as blocking

#### Scenario: Every merged release is covered
- **WHEN** both lanes post success on `def456`, after both merges
- **THEN** pipeline tags `def456` and the notes list both batches with their lanes
