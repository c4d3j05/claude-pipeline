## ADDED Requirements

### Requirement: Source line names the task's thread
The pipeline SHALL read the Slack thread of a task from the first token after `Source:` in its
description. It SHALL accept a Slack permalink (`https://<ws>.slack.com/archives/<channel>/p<digits>`,
with an optional `thread_ts` query parameter) and planner's fallback form
`slack:<channel>/<thread_ts>`. For any other token it SHALL use no thread, and post to the webhook.

#### Scenario: Permalink
- **WHEN** a description has `Source: https://ignify.slack.com/archives/D0CGS2L6KLK/p1699999999000100`
- **THEN** the thread is channel `D0CGS2L6KLK`, ts `1699999999.000100`

#### Scenario: Fallback form
- **WHEN** a description has `Source: slack:D0CGS2L6KLK/1699999999.000100 · investigation ab12`
- **THEN** the thread is channel `D0CGS2L6KLK`, ts `1699999999.000100`

#### Scenario: Unknown form
- **WHEN** a description has `Source: slack:x1/1699999999.000100`
- **THEN** there is no thread, and updates go to the webhook
