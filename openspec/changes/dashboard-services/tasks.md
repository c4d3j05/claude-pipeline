## 1. Services source and tab

- [x] 1.1 `dispatcher/web/services.py`: `read_services`, `processes`, `needs_attention`
- [x] 1.2 `DASHBOARD_UNITS` setting (config, `.env.example`, README)
- [x] 1.3 Source, overview health line and alerts, Services tab and template
- [x] 1.4 Tests in `tests/test_web_services.py`; web tests and ruff pass

## 2. Before archiving

- [ ] 2.1 Check the tab on the Debian host against `systemctl --user status`
- [ ] 2.2 Report `repowise risk main..HEAD` and `repowise impacted-tests main..HEAD`
