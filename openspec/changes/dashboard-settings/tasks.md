Each task ends with `uv run pytest` (whole suite) and `uv run ruff check dispatcher tests` green.

- [x] 1 `dispatcher/web/settings.py`: allow-list (verify defaults against each repo's config.py), read, validate, write (design 1, 2). Tests: the read/validate/write ones in design 4.
- [x] 2 Routes, the Settings tab and its template (design 3). Tests: the route ones in design 4.
- [ ] 3 README: a "Dashboard settings" paragraph (what is editable, backups, restarts, never secrets).
- [ ] 4 (owner) Pull and restart `claude-pipeline-web`.
