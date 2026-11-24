## 1. Change
- [x] 1.1 `RELEASE_TICKET_MERGE_METHOD` in config (default `merge`, unknown values fall back)
- [x] 1.2 `merge_into_release` uses it; a repair always merges; refusals logged
- [x] 1.3 `.env.example`, `RELEASES.md`
- [x] 1.4 Tests in `tests/test_releases.py`; pytest (the known git-init flake on the Mac aside) and ruff pass

## 2. Deploy
- [ ] 2.1 Pull on Debian and restart the dispatcher (it drains running tasks first)
