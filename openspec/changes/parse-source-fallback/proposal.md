# Proposal

## Why

When planner cannot get a Slack permalink while filing, it writes the task's Source line as
`Source: slack:<channel>/<thread_ts>` (`planner/filing.py:189`). The pipeline only reads
`https://<ws>.slack.com/archives/…` permalinks (`dispatcher/slackthread.py:24-26`). So such a task's
updates silently go to the webhook channel instead of the owner's thread.

The owner decided (integration spec decision D2, `../planner/openspec/specs/integration/spec.md`)
that the pipeline learns the fallback form rather than planner failing the filing.

repowise findings:
- `get_overview(pipeline)`: Python dispatcher, entry point `dispatcher/__main__.py`.
- `get_context(dispatcher/slackthread.py)`: `parse_source` (line 34) reads the Source line. `post`
  (line 56) needs only a channel and a thread timestamp, so the fallback form carries everything a
  post needs.
- `get_why(dispatcher/slackthread.py)`: no recorded decisions. The module docstring says every step
  is best effort and falls back to the webhook.
- `get_health(dispatcher/slackthread.py)`: 10.0, max CCN 8, has a test file (`tests/test_thread.py`).
  Building on top.

## What Changes

- `parse_source` also accepts `slack:<C|D|G…>/<digits>.<digits>` as the first token after `Source:`.
- The permalink form is unchanged. Anything else still gives no thread, so the post falls back to the
  webhook.

## Impact

`get_risk(dispatcher/slackthread.py)`: 3 direct dependents (`daemon.py:396` calls `parse_source`, and
`notify.py:68` uses `Thread`), not a hotspot, no co-change partners, health 10.0.

Affected repos:
- **pipeline**: this change.
- **planner**: none. It already writes the form; the integration spec records it as decision D2
  ("Source fallback form", **[decided, pending]** until this change is archived).
- **explorer**: none. It links tasks by fingerprint or permalink, not by this form.
