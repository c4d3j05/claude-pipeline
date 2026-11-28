## ADDED Requirements

### Requirement: Settings tab
With `DASHBOARD_ACTIONS` on, the dashboard SHALL let the owner change an allow-list of operational settings in
the pipeline's, the planner's and explorer's `.env`, each validated, backed up, logged, and labelled with whether
it applies now or after a restart. No other line of those files SHALL appear in a page, a response or a log.

#### Scenario: A longer recheck limit
- **WHEN** the owner sets explorer's RECHECK_WALL_CLOCK_MIN to 25
- **THEN** explorer's .env has `RECHECK_WALL_CLOCK_MIN=25`, a backup exists, and the page offers Restart explorer-serve

#### Scenario: A secret in the file
- **WHEN** explorer's .env holds SLACK_BOT_TOKEN
- **THEN** the Settings tab never shows it and it cannot be saved
