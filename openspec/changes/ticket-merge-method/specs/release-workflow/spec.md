## ADDED Requirements

### Requirement: Ticket merge method
The pipeline SHALL merge a ticket PR into its release branch with the method `RELEASE_TICKET_MERGE_METHOD`
(`merge`, `squash` or `rebase`; default `merge`), and a repair ticket's PR always with a merge commit.
A merge GitHub refuses SHALL be logged with GitHub's message, besides the "needs attention" notice.

#### Scenario: A repository that allows only merge commits
- **WHEN** the setting is left at its default and a ticket's gates pass
- **THEN** its PR is merged into `release/r-n` with a merge commit

#### Scenario: A refused merge
- **WHEN** GitHub refuses the merge
- **THEN** the log has a warning with GitHub's message and the PR stays open for a human
