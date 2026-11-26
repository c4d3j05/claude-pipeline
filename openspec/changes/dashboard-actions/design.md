# Design

## 1. Settings (dispatcher/config.py, .env.example, README)

`DASHBOARD_ACTIONS` (bool, default true). `DASHBOARD_ACTION_LOG` (path, default `logs/dashboard-actions.jsonl`
relative to the working directory).

## 2. Module `dispatcher/web/actions.py`

```python
@dataclass
class Result:
    ok: bool            # done
    refused: bool       # not done on purpose (busy, unknown target)
    message: str        # one line shown to the user and logged

class Actions:
    def __init__(self, cfg, sources, *, run=subprocess.run, ledger_factory=None, now=None): ...
    def explorer_run(self, lane: str | None) -> Result          # lane: None, "global" or a lane id
    def explorer_answer(self, question_id: str, letter: str) -> Result
    def explorer_batch(self, action: str, batch_id: str) -> Result   # send | hold | unhold
    def pipeline_retry(self, task_id: int) -> Result
    def restart(self, unit: str) -> Result
    def log(self, request_ip: str, action: str, target: str, result: Result) -> None
    def recent(self, n: int = 20) -> list[dict]
```
Explorer commands run as `["uv", "run", "--quiet", "explorer", ...]` with `cwd=cfg.dashboard_explorer_dir`,
`timeout=60`, `capture_output=True, text=True` (same pattern as sources.read_status). Result `ok` when the
return code is 0; message = the last non-empty stdout line, or on failure the last stderr line prefixed `failed: `.
Validation before running anything (else `refused` with the reason): question id `^q-[0-9a-f]{12}$`;
letter in A/B/C; batch id `^B-\d+$`; action in send/hold/unhold; lane `global` or `^[a-z][a-z0-9-]{0,39}$`;
explorer dir configured.
- `explorer_run`: `explorer request-run [--global | --lane <id>] --by dashboard`.
- `explorer_answer`: `explorer answer <qid> <letter>`.
- `explorer_batch`: `explorer batch <action> <bid>`.
- `pipeline_retry`: only for a task whose ledger row state is `blocked` (else refused `task <id> is <state>`);
  calls `dashboard.retry_task(cfg, ledger, task_id)`; message `#<id> re-queued`.
- `restart(unit)`: allowed units = `cfg.dashboard_units` without `.timer` ones, else refused `unknown unit`.
  Busy checks (refused with the reason):
  - `explorer-serve`: explorer status (via `sources.get("explorer")`, fresh, not cached) has a non-null `run`
    → `explorer is running a <reason> run (<id>) since <ago>`;
  - `claude-pipeline-dispatcher`: `ledger.active_count() > 0` → `<n> ticket(s) building: <ids>`;
  - `claude-planner`: planner status has an investigation in provisioning/working/filing →
    `planner is working on <id>`.
  Then `systemctl --user restart <unit>` (timeout 60). For `claude-pipeline-web` itself instead
  `systemd-run --user --on-active=2 systemctl --user restart claude-pipeline-web`, message
  `restarting the dashboard in 2 s; reload the page`.
- `log`: append one JSON line `{"at", "ip", "action", "target", "ok", "refused", "message"}`; never raises.
  `recent` reads the last n lines, newest first, skipping bad lines.

## 3. Routes (dispatcher/web/app.py)

Only when `cfg.dashboard_actions`; otherwise every `/action/...` returns 404.
`POST /action/explorer/run` (form `lane`, optional), `POST /action/explorer/answer` (`question`, `letter`),
`POST /action/explorer/batch` (`action`, `batch`), `POST /action/pipeline/retry` (`task`),
`POST /action/service/restart` (`unit`). Each returns the `_action_result.html` fragment:
`<div class="card note|error">` with ✓ / ✗ and the message, swapped into `#action-result` at the top of the page.
Form data needs `python-multipart`; if it is not already a dependency of the `web` group, add it.

## 4. Buttons (templates)

All `hx-post`, `hx-target="#action-result"`, `hx-confirm` with the exact text below, and the refresh of the
section continues as today. Shown only when `actions` (a template global = `cfg.dashboard_actions`).
- Explorer tab, header: **Run now** (pre-lane: one button, confirm `Ask explorer to run now? It starts within
  a few minutes, after any run in progress.`); with a `lanes` block: one button per lane plus **Global run**
  (confirm `Ask explorer to run lane <id> now? …`).
- Explorer tab, open questions of kind `escalation`: one button per option, label `A`, `B`, `C` with the option
  text as title; confirm `Answer <qid> with <letter>: <option text>?`.
- Explorer tab, batches in state `veto`: **Send** (confirm `Send <bid> to the planner now?`) and **Hold**;
  held batches: **Unhold**.
- Pipeline tab, tickets in state `blocked`: **Retry** (confirm `Retry #<id>? It goes back to Ready and is
  claimed again.`).
- Services tab, each non-timer unit: **Restart** (confirm `Restart <unit>? It is refused if the service is busy.`).
- Overview: card **Recent actions** (time via `ago`, action, target, ✓/✗/refused, message), from `recent(20)`.
- `page.html`: an empty `<div id="action-result"></div>` above `<main>`.

## 5. Tests (tests/test_web_actions.py, new; TestClient, injected `run` and fake ledger)

- `test_actions_off_hides_buttons_and_404s`.
- `test_run_now_calls_request_run` (pre-lane args; lane and global args).
- `test_answer_validates_and_calls_explorer_answer` (bad id / bad letter refused without running anything).
- `test_batch_send_hold_unhold`.
- `test_retry_only_blocked_tickets`.
- `test_restart_refused_while_busy` (explorer run active; dispatcher with active tickets; planner working).
- `test_restart_idle_unit_and_self_restart_is_delayed` (systemctl args; systemd-run args for the web unit).
- `test_timers_and_unknown_units_are_refused`.
- `test_every_action_is_logged_and_shown_on_overview` (incl. refused ones; a bad log line is skipped).
- `test_command_failure_is_reported` (non-zero return code → `failed: <stderr line>`).
