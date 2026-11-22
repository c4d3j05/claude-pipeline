## 1. Parse the fallback form

- [x] 1.1 `parse_source` in `dispatcher/slackthread.py` accepts `slack:<channel>/<thread_ts>`
- [x] 1.2 Tests in `tests/test_thread.py`: the fallback form, the form with trailing Source parts, in
  HTML, and rejected variants
- [x] 1.3 `uv run pytest` and `uv run ruff check dispatcher tests` pass

## 2. Before archiving

- [ ] 2.1 Report `repowise risk main..HEAD` and `repowise impacted-tests main..HEAD`
- [ ] 2.2 In planner's integration spec, change "Source fallback form" from [decided, pending] to [code]
