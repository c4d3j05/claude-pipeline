## ADDED Requirements

### Requirement: Tag on a settled batch
With `RELEASE_TAGS` on, the pipeline SHALL, at most every `TAG_POLL_MIN`, read the commit statuses of
the base branch's first-parent commits since the newest `TAG_PREFIX` tag and tag the newest commit
whose latest `explorer/batch-settled` status is `success`. It SHALL never tag a commit outside the
first-parent history of the base branch, and never move or delete a tag. With `RELEASE_TAGS` off it
SHALL tag nothing.

#### Scenario: B-1 settles
- **WHEN** explorer marks main's commit `8a1f…` `explorer/batch-settled` success, "B-1 settled"
- **THEN** the next pass creates an annotated tag on `8a1f…`

#### Scenario: Nothing settled
- **WHEN** no commit since the last tag has a settled status
- **THEN** the pass creates nothing and posts nothing

### Requirement: Tag name
A tag SHALL be named `<TAG_PREFIX>YYYY.MM.DD` after the host's local date, with `.2`, `.3`… for
further tags the same day.

#### Scenario: Second tag in a day
- **WHEN** `v2026.11.24` already exists on another commit
- **THEN** the new tag is `v2026.11.24.2`

### Requirement: Release notes and announcement
Each tag SHALL get a GitHub Release whose notes name the settled batches and the tagged commit, then
list every first-parent commit since the previous tag (PR number and title for a PR merge), and one
Slack line with the tag, the short SHA, the batches and the release link.

#### Scenario: Two releases in one tag
- **WHEN** B-1 settles after R-1 (#635) and R-2 (#640) were merged since the last tag
- **THEN** the release notes list `#640` and `#635` with their titles

### Requirement: Tag failure
A failed fetch, status read, tag, push or release SHALL be logged, alerted in Slack once per commit,
and retried on the next pass. A tag that was created but not yet pushed or released SHALL be
finished on the next pass before anything else is scanned.

#### Scenario: Release creation fails
- **WHEN** the tag is pushed but `gh release create` fails
- **THEN** the owner gets one alert, and the next pass creates the release for the same tag

### Requirement: Tag command
`claude-pipeline tag` SHALL run one pass now; `--dry-run` SHALL print the tag name, commit, batches
and notes it would create and change nothing.

#### Scenario: Dry run
- **WHEN** the owner runs `claude-pipeline tag --dry-run`
- **THEN** it prints "would tag v2026.11.24 on <sha>" with the notes, and no tag exists afterwards
