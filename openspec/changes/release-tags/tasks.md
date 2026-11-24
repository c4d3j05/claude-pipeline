## 1. Release tags

- [x] 1.1 `dispatcher/tagging.py`: plan (fetch, previous tag, first-parent range, statuses), name, notes, tag, push, release, Slack
- [x] 1.2 Settings `RELEASE_TAGS` (hot-reloaded), `TAG_PREFIX`, `TAG_SCAN_COMMITS`, `TAG_POLL_MIN` (config, `.env.example`, README)
- [x] 1.3 Ledger `meta` table: alert once per commit, finish a pending tag first
- [x] 1.4 Daemon: a pass at most every `TAG_POLL_MIN`, in the pool, one at a time
- [x] 1.5 CLI `claude-pipeline tag [--dry-run]`
- [x] 1.6 Tests in `tests/test_tagging.py`; full suite and ruff pass

## 2. Before archiving

- [ ] 2.1 explorer posts `explorer/batch-settled` (explorer change `release-tags`); planner's integration spec has D17
- [ ] 2.2 On the Debian host: the pipeline's GitHub login may push tags; `claude-pipeline tag --dry-run`, then `RELEASE_TAGS=true`
- [ ] 2.3 Report `repowise risk main..HEAD` and `repowise impacted-tests main..HEAD`
