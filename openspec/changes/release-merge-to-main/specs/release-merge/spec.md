## ADDED Requirements

### Requirement: Single merge gate
The pipeline SHALL decide every merge it makes in `releases.auto_merge_allowed`. A ticket PR into a
release branch SHALL be merged only with `RELEASE_AUTO_MERGE` on. The release PR (head `release/r-n`,
base `BASE_BRANCH`) SHALL be merged only with `RELEASE_MERGE_TO_MAIN` on. A ticket PR into
`BASE_BRANCH` SHALL never be merged by the pipeline, whatever the settings.

#### Scenario: Hotfix into main
- **WHEN** both settings are on and a hotfix ticket's PR targets main
- **THEN** the pipeline does not merge it and makes no gh merge call

#### Scenario: Release PR with the setting off
- **WHEN** `RELEASE_MERGE_TO_MAIN` is off and a release PR is opened
- **THEN** the pipeline does not merge it, and Slack says to merge it with a merge commit

### Requirement: Release PR merged with a merge commit
With `RELEASE_MERGE_TO_MAIN` on, once every ticket of a release is done and its head is green, the
pipeline SHALL merge the release PR with a merge commit at the tested head, never a squash or rebase.
When GitHub refuses a direct merge, it SHALL turn on GitHub's auto-merge, still with a merge commit.

#### Scenario: Release merged
- **WHEN** every ticket of R-4 is done, the head of `release/r-4` is green and GitHub accepts the merge
- **THEN** the release PR is merged with a merge commit, and on the next pass the tracker moves to
  Done with `merged: <sha9> into main (<url>)`

#### Scenario: Required checks pending
- **WHEN** GitHub refuses the direct merge because main's required checks have not finished
- **THEN** the pipeline turns on auto-merge and GitHub merges the PR once they pass

### Requirement: Refused release merge waits for the owner
When GitHub refuses both the merge and auto-merge, the pipeline SHALL leave the release PR open,
comment on the tracker, alert in Slack that the owner is needed, and SHALL NOT try to merge that open
PR again.

#### Scenario: Branch protection refuses
- **WHEN** main's protection refuses the pipeline's merge of `release/r-4`
- **THEN** the tracker gets `not merged into main: …`, Slack says the release needs the owner, and
  later passes do not try again
