## 1. Gate and setting

- [x] 1.1 `RELEASE_MERGE_TO_MAIN` (default false) in `dispatcher/config.py` and `.env.example`
- [x] 1.2 `releases.auto_merge_allowed(*, tickets, to_main, head, base, base_branch)`, the single gate;
  both call sites updated
- [x] 1.3 Test every combination, including a ticket PR into main with both settings on

## 2. Merging the release PR

- [x] 2.1 `pr.merge_release_into_main`: `--merge --match-head-commit`, then `--auto`; never squash or
  rebase; refuses without a gh call when the gate says no or the head is unknown
- [x] 2.2 `releases.merge_messages` for the tracker comment and the Slack alert
- [x] 2.3 `release_pass` merges right after opening the PR (call lines only); an open release PR is
  never retried
- [x] 2.4 Tests: merged, queued, refused, off, not retried

## 3. Docs and checks

- [x] 3.1 README.md and RELEASES.md: the setting, the owner's decision, branch protection
- [x] 3.2 `uv run pytest` and `uv run ruff check dispatcher tests`: no new failures (the existing
  `git init` SIGSEGV failures in this sandbox are unchanged)
- [ ] 3.3 Host: set `RELEASE_AUTO_MERGE=true` and `RELEASE_MERGE_TO_MAIN=true`; check main's branch
  protection and the repo's auto-merge setting
- [ ] 3.4 Before archiving: report `repowise risk main..HEAD` and `repowise impacted-tests main..HEAD`
