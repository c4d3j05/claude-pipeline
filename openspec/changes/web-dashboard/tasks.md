## 1. Dashboard

- [x] 1.1 `dispatcher/web/sources.py`: pipeline, board, planner and explorer sources behind a TTL cache
- [x] 1.2 `dispatcher/web/views.py`: health, spend, alerts, findings filter
- [x] 1.3 `dispatcher/web/app.py` + templates + vendored htmx + CSS; GET routes only
- [x] 1.4 `claude-pipeline web`, the `DASHBOARD_*` settings, `.env.example`, the optional `web` group
- [x] 1.5 `systemd/claude-pipeline-web.service`, installed (not enabled) by `scripts/install.sh`; README
- [x] 1.6 Tests in `tests/test_web.py`; `uv run pytest` and `ruff` pass

## 2. Linked changes and deploy

- [ ] 2.1 planner: `claude-planner status --json` (schema 1)
- [ ] 2.2 explorer: `explorer status --json` (schema 1)
- [ ] 2.3 Integration spec: "Status JSON" with both shapes
- [ ] 2.4 VM: set `DASHBOARD_HOST=0.0.0.0` and the two checkout dirs, `uv sync --group web`, enable the unit
- [ ] 2.5 Before archiving: report `repowise risk main..HEAD` and `repowise impacted-tests main..HEAD`
