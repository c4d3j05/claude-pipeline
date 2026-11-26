Each task ends with `uv run pytest` (whole suite; re-run up to 3 times on the known git-init crash pattern
in tests/test_gaps.py/test_touches.py) and `uv run ruff check dispatcher tests` green.

- [x] 1 Settings and `dispatcher/web/actions.py` with logging (design 1, 2). Tests: all `Actions`-level ones in design 5.
- [x] 2 Routes and the result fragment (design 3). Tests: `test_actions_off_hides_buttons_and_404s` and route-level versions of the others.
- [x] 3 Buttons and the Recent actions card (design 4). Test: `test_every_action_is_logged_and_shown_on_overview`; buttons appear only with actions on.
- [x] 4 README: a "Dashboard actions" section (what each does, the busy rules, no password, `DASHBOARD_ACTIONS=false`).
- [ ] 5 (owner) Deploy explorer `run-request` first, then pull here, `uv sync --group web`, restart `claude-pipeline-web`.
