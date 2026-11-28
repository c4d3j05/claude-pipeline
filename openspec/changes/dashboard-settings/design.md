# Design

## 1. The allow-list (`dispatcher/web/settings.py`)

```python
@dataclass(frozen=True)
class Setting:
    key: str
    kind: str            # "int" | "float" | "bool"
    default: str         # the code default, shown when the key is unset
    min: float | None
    max: float | None
    restart: str | None  # unit to restart for it to apply; None = hot-reloaded
    help: str

ALLOWED: dict[str, tuple[Setting, ...]]   # "pipeline" | "planner" | "explorer"
```
pipeline (file: this repo's `.env`; restart `claude-pipeline-dispatcher` unless hot):
`MAX_WORKERS` int 1..8 default 2 hot; `DAILY_CAP_USD` float >=1 default 80 hot; `TASK_CAP_USD` float >=1 hot;
`WALL_CLOCK_LIMIT_MIN` int 10..240 default 45 hot; `MAX_TURNS` int 10..300 default 60 hot;
`RELEASE_TAGS` bool hot; `RELEASE_AUTO_MERGE` bool restart; `RELEASE_MERGE_TO_MAIN` bool restart.
planner (file: `DASHBOARD_PLANNER_DIR/.env`; restart `claude-planner`): `MAX_INVESTIGATIONS` int 1..6 default 3;
`THREAD_CAP_USD` float >=1 default 5; `DAILY_CAP_USD` float >=1 default 20; `IDLE_EXPIRE_HOURS` int 1..720 default 72.
explorer (file: `DASHBOARD_EXPLORER_DIR/.env`; restart `explorer-serve`): `POLL_INTERVAL_MIN` int 1..60 default 10;
`RUN_CAP_USD` float >=0.5 default 10; `DAILY_CAP_USD` float >=1 default 75; `LANE_DAILY_CAP_USD` float >=0.5 default 10;
`EXPLORE_CAP_USD` float >=0.1 default 0.60; `EXPLORE_WALL_CLOCK_MIN` int 3..60 default 8;
`RECHECK_WALL_CLOCK_MIN` int 3..60 default 15; `LANE_RUN_MAX_MIN` int 5..240 default 45;
`LANE_PRACTITIONER_PER_DAY` int 0..5 default 1; `FINDINGS_PER_RUN` int 0..50 default 10;
`QUESTIONS_PER_RUN` int 0..50 default 5; `BATCH_MAX_FINDINGS` int 1..60 default 25; `BATCH_VETO_HOURS` float 0..72 default 12.
Defaults are the code defaults of each repo as of 2026-11-27 (check each in its config.py and correct the table
if any differs; note the correction in the commit message). Help lines are one sentence each.

## 2. Reading and writing a `.env`

```python
def path_of(cfg, service) -> Path | None            # None when that service's dir is not configured/exists
def read(cfg, service) -> dict[str, str | None]      # only allow-listed keys; None = unset
def validate(service, key, value, current: dict) -> str | None   # error text or None
def write(cfg, service, key, value, now) -> Path     # returns the backup path
```
`read` parses lines `KEY=VALUE` (ignoring blank lines and `#` comments; strips one pair of surrounding quotes and
an inline ` # comment`) and returns ONLY allow-listed keys. Nothing else from the file is kept in memory beyond
the parse.
`validate`: the key must be allow-listed for that service; value parsed by kind (bool accepts true/false/1/0/yes/no,
written back as `true`/`false`); within min/max; cross-checks with the other current values of that file:
explorer `LANE_DAILY_CAP_USD <= DAILY_CAP_USD` and `RUN_CAP_USD <= DAILY_CAP_USD`; pipeline `TASK_CAP_USD <= DAILY_CAP_USD`;
planner `THREAD_CAP_USD <= DAILY_CAP_USD`. Error texts like `RECHECK_WALL_CLOCK_MIN must be between 3 and 60`.
`write`: copy the file to `.env.bak-YYYYMMDDHHMMSS` next to it (keep the newest 10 backups, delete older ones);
replace the first non-comment line starting `KEY=` with `KEY=value`, or append `KEY=value` with a preceding
comment `# set from the dashboard <date>` when absent; write to a temp file in the same dir, copy the original's
permission bits, `os.replace`. Never touch any other line.

## 3. Routes and page

`GET /settings` (a tab, added to TABS after `services`) and `GET /section/settings`: three cards (pipeline,
planner, explorer); a service whose dir is not configured shows "not configured". Each row: key, current value
(or `default (<code default>)`), an input of the right type (number with min/max/step, or a select true/false),
the help line, and a badge "applies now" (hot) or "restart <unit>".
`POST /action/settings/save` (form `service`, `key`, `value`): validate → write → log (action `settings/save`,
target `<service>:<key>`, message `KEY=value saved; restart <unit> to apply` or `KEY=value saved; applies now`)
→ result fragment, which for a restart-needing setting includes the existing Restart button for that unit.
Confirm text: `Set KEY=value in <service>'s .env? <applies now | it applies after restarting <unit>>`.
All of this only when `DASHBOARD_ACTIONS` is on; otherwise the tab shows values read-only and the POST is 404.

## 4. Tests (`tests/test_web_settings.py`)

- `test_read_returns_only_allow_listed_keys` (a file with SLACK_BOT_TOKEN etc.: never returned; never in the page HTML).
- `test_validate_kinds_ranges_and_cross_checks`.
- `test_write_replaces_one_line_keeps_the_rest_and_backs_up` (comments, order, other keys and file mode kept; backup created; 11th backup prunes the oldest).
- `test_write_appends_an_absent_key`.
- `test_save_route_logs_and_reports_restart` and `test_save_route_hot_setting_applies_now`.
- `test_unknown_key_or_service_is_refused_without_touching_the_file`.
- `test_actions_off_settings_are_read_only`.
- `test_unconfigured_service_shows_not_configured`.
