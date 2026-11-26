"""Dashboard actions (OpenSpec change dashboard-actions): Actions, routes, buttons, the log."""

from __future__ import annotations

import datetime as dt
import json
import subprocess

import pytest
from fastapi.testclient import TestClient

from dispatcher import config as cfgmod
from dispatcher import dashboard
from dispatcher.web.actions import Actions
from dispatcher.web.app import create_app
from dispatcher.web.sources import Sources

NOW = dt.datetime(2026, 11, 25, 20, 0, tzinfo=dt.UTC)
QID = "q-0123456789ab"
UNITS = "claude-pipeline-dispatcher,claude-pipeline-web,claude-planner,explorer-serve,explorer-nightly.timer"


@pytest.fixture
def cfg(tmp_path):
    expl = tmp_path / "explorer"
    expl.mkdir()
    env = tmp_path / ".env"
    env.write_text("VIKUNJA_URL=https://vk.example/api/v1\nVIKUNJA_API_TOKEN=t\nVIKUNJA_PROJECT_ID=51\n"
                   f"PIPELINE_DB={tmp_path / 'p.db'}\nDASHBOARD_EXPLORER_DIR={expl}\n"
                   f"DASHBOARD_ACTION_LOG={tmp_path / 'actions.jsonl'}\nDASHBOARD_UNITS={UNITS}\n")
    return cfgmod.load(str(env))


class FakeLedger:
    def __init__(self, rows=None):
        self.rows = rows or {}

    def get(self, task_id):
        return self.rows.get(task_id)

    def active(self):
        return [r for r in self.rows.values() if r["state"] in ("claimed", "working")]

    def close(self):
        pass


class Runner:
    def __init__(self, rc=0, stdout="", stderr=""):
        self.calls, self.rc, self.stdout, self.stderr = [], rc, stdout, stderr

    def __call__(self, cmd, **kw):
        self.calls.append((cmd, kw))
        return subprocess.CompletedProcess(cmd, self.rc, self.stdout, self.stderr)


def make(cfg, *, explorer=None, planner=None, rows=None, rc=0, stdout="", stderr=""):
    readers = {"pipeline": lambda: {"tasks": []}, "board": lambda: {"buckets": [], "releases": []},
               "planner": lambda: planner or {"investigations": []},
               "explorer": lambda: explorer or {"run": None}}
    sources = Sources(cfg, readers=readers)
    run = Runner(rc, stdout, stderr)
    ledger = FakeLedger(rows)
    return Actions(cfg, sources, run=run, ledger_factory=lambda: ledger, now=lambda: NOW), run


def row(state):
    return {"task_id": 0, "state": state}


# --- explorer ------------------------------------------------------------------------------------
def test_run_now_calls_request_run(cfg):
    a, run = make(cfg, stdout="run requested\n")
    r = a.explorer_run(None)
    assert (r.ok, r.refused, r.message) == (True, False, "run requested")
    cmd, kw = run.calls[0]
    assert cmd == ["uv", "run", "--quiet", "explorer", "request-run", "--by", "dashboard"]
    assert str(kw["cwd"]) == cfg.dashboard_explorer_dir and kw["timeout"] == 60
    a.explorer_run("global")
    a.explorer_run("billing-2")
    assert run.calls[1][0][4:] == ["request-run", "--global", "--by", "dashboard"]
    assert run.calls[2][0][4:] == ["request-run", "--lane", "billing-2", "--by", "dashboard"]
    n = len(run.calls)
    for bad in ("Bad", "a;ls", "-x", "x" * 41, "1a"):
        assert a.explorer_run(bad).refused
    assert len(run.calls) == n


def test_run_needs_explorer_dir(cfg):
    import dataclasses
    a, run = make(dataclasses.replace(cfg, dashboard_explorer_dir=""))
    assert a.explorer_run(None).refused and not run.calls


def test_answer_validates_and_calls_explorer_answer(cfg):
    a, run = make(cfg, stdout="answered\n")
    assert a.explorer_answer(QID, "B").ok
    assert run.calls[0][0] == ["uv", "run", "--quiet", "explorer", "answer", QID, "B"]
    n = len(run.calls)
    for qid, letter in (("q-1", "A"), (QID + "0", "A"), ("q-0123456789AB", "A"), (QID, "D"),
                        (QID, "a"), (QID, ""), ("q-0123456789ab; rm -rf /", "A")):
        assert a.explorer_answer(qid, letter).refused
    assert len(run.calls) == n


def test_batch_send_hold_unhold(cfg):
    a, run = make(cfg)
    for act in ("send", "hold", "unhold"):
        assert a.explorer_batch(act, "B-12").ok
    assert [c[0][4:] for c in run.calls] == [["batch", x, "B-12"] for x in ("send", "hold", "unhold")]
    n = len(run.calls)
    assert a.explorer_batch("delete", "B-1").refused
    assert a.explorer_batch("send", "B-x").refused
    assert a.explorer_batch("send", "b-1").refused
    assert len(run.calls) == n


def test_command_failure_is_reported(cfg):
    a, _ = make(cfg, rc=2, stdout="", stderr="warn\nno such question\n\n")
    r = a.explorer_answer(QID, "A")
    assert (r.ok, r.refused, r.message) == (False, False, "failed: no such question")


# --- pipeline ------------------------------------------------------------------------------------
def test_retry_only_blocked_tickets(cfg, monkeypatch):
    calls = []
    monkeypatch.setattr(dashboard, "retry_task", lambda c, ledger, t: calls.append(t) or "re-queued")
    rows = {1: {"task_id": 1, "state": "blocked"}, 2: {"task_id": 2, "state": "working"}}
    a, _ = make(cfg, rows=rows)
    assert a.pipeline_retry(1).message == "#1 re-queued" and calls == [1]
    r = a.pipeline_retry(2)
    assert r.refused and r.message == "task 2 is working"
    assert a.pipeline_retry(99).refused and a.pipeline_retry(0).refused
    assert calls == [1]


def test_retry_failure_is_reported(cfg, monkeypatch):
    def boom(*a):
        raise RuntimeError("vikunja down")
    monkeypatch.setattr(dashboard, "retry_task", boom)
    a, _ = make(cfg, rows={1: {"task_id": 1, "state": "blocked"}})
    r = a.pipeline_retry(1)
    assert not r.ok and not r.refused and r.message == "failed: vikunja down"


# --- services ------------------------------------------------------------------------------------
def test_restart_refused_while_busy(cfg):
    a, run = make(cfg, explorer={"run": {"id": "r1", "reason": "recheck"}})
    r = a.restart("explorer-serve")
    assert r.refused and r.message.startswith("explorer is running a recheck run (r1) since")
    a, run2 = make(cfg, rows={5: {"task_id": 5, "state": "working"}, 6: {"task_id": 6, "state": "claimed"}})
    r = a.restart("claude-pipeline-dispatcher")
    assert r.refused and r.message == "2 ticket(s) building: #5, #6"
    a, run3 = make(cfg, planner={"investigations": [{"id": "abc", "state": "working"}]})
    r = a.restart("claude-planner")
    assert r.refused and r.message == "planner is working on abc"
    assert not (run.calls or run2.calls or run3.calls)


def test_restart_idle_unit_and_self_restart_is_delayed(cfg):
    a, run = make(cfg, stdout="")
    assert a.restart("explorer-serve").ok
    assert run.calls[0][0] == ["systemctl", "--user", "restart", "explorer-serve"]
    assert a.restart("claude-planner").ok and a.restart("claude-pipeline-dispatcher").ok
    r = a.restart("claude-pipeline-web")
    assert r.ok and r.message == "restarting the dashboard in 2 s; reload the page"
    assert run.calls[-1][0] == ["systemd-run", "--user", "--on-active=2", "systemctl", "--user",
                                "restart", "claude-pipeline-web"]


def test_timers_and_unknown_units_are_refused(cfg):
    a, run = make(cfg)
    for u in ("explorer-nightly.timer", "sshd", "explorer-serve.service", "explorer-serve; ls", ""):
        r = a.restart(u)
        assert r.refused and r.message == "unknown unit"
    assert not run.calls


def test_unreadable_status_blocks_restart(cfg):
    a, run = make(cfg)
    a.sources._readers["explorer"] = lambda: (_ for _ in ()).throw(ValueError("boom"))
    assert a.restart("explorer-serve").refused and not run.calls


# --- the log -------------------------------------------------------------------------------------
def test_log_and_recent_skip_bad_lines(cfg):
    a, _ = make(cfg)
    r = a.explorer_run(None)
    a.log("10.0.0.5", "explorer/run", "global", r)
    with cfg.dashboard_action_log.open("a") as f:
        f.write("not json\n[1]\n")
    a.log("10.0.0.5", "restart", "sshd", a.restart("sshd"))
    rows = a.recent()
    assert [x["action"] for x in rows] == ["restart", "explorer/run"]
    assert rows[0]["refused"] is True and rows[1]["ip"] == "10.0.0.5"
    assert set(rows[0]) == {"at", "ip", "action", "target", "ok", "refused", "message"}
    assert json.loads(cfg.dashboard_action_log.read_text().splitlines()[0])["at"].startswith("2026-11-25")
    assert len(a.recent(1)) == 1


def test_log_never_raises(cfg, tmp_path):
    import dataclasses
    blocked = tmp_path / "file"
    blocked.write_text("x")
    c = dataclasses.replace(cfg, dashboard_action_log=blocked / "sub" / "a.jsonl")
    a, _ = make(c)
    a.log("ip", "x", "y", a.restart("sshd"))
    assert a.recent() == []


# --- routes --------------------------------------------------------------------------------------
def web(cfg, **kw):
    a, run = make(cfg, **kw)
    return TestClient(create_app(cfg, a.sources, a)), a, run


def test_actions_off_hides_buttons_and_404s(cfg):
    import dataclasses
    off = dataclasses.replace(cfg, dashboard_actions=False)
    a, run = make(off)
    c = TestClient(create_app(off, a.sources, a))
    for path, data in (("/action/explorer/run", {}), ("/action/explorer/answer", {"question": QID, "letter": "A"}),
                       ("/action/explorer/batch", {"action": "send", "batch": "B-1"}),
                       ("/action/pipeline/retry", {"task": "1"}), ("/action/service/restart", {"unit": "explorer-serve"})):
        assert c.post(path, data=data).status_code == 404
    assert not run.calls and not off.dashboard_action_log.exists()


def test_actions_default_on_and_get_is_not_allowed(cfg):
    assert cfg.dashboard_actions is True
    c, _, _ = web(cfg)
    assert c.get("/action/explorer/run").status_code in (404, 405)


def test_routes_run_and_answer_and_batch(cfg):
    c, _a, run = web(cfg, stdout="ok line\n")
    r = c.post("/action/explorer/run", data={"lane": "global"})
    assert r.status_code == 200 and "✓" in r.text and "ok line" in r.text and 'class="card note"' in r.text
    assert run.calls[-1][0][4:] == ["request-run", "--global", "--by", "dashboard"]
    c.post("/action/explorer/run")
    assert run.calls[-1][0][4:] == ["request-run", "--by", "dashboard"]
    c.post("/action/explorer/answer", data={"question": QID, "letter": "C"})
    assert run.calls[-1][0][4:] == ["answer", QID, "C"]
    c.post("/action/explorer/batch", data={"action": "hold", "batch": "B-4"})
    assert run.calls[-1][0][4:] == ["batch", "hold", "B-4"]
    n = len(run.calls)
    r = c.post("/action/explorer/answer", data={"question": "q-1", "letter": "A"})
    assert "✗" in r.text and 'class="card error"' in r.text
    c.post("/action/explorer/answer")
    c.post("/action/explorer/batch", data={"action": "rm", "batch": "B-4"})
    assert len(run.calls) == n


def test_route_html_is_escaped_and_failures_reported(cfg):
    c, _, _ = web(cfg, rc=1, stderr="<b>bad</b>")
    r = c.post("/action/explorer/run")
    assert "&lt;b&gt;bad&lt;/b&gt;" in r.text and "✗" in r.text


def test_route_retry_and_restart(cfg, monkeypatch):
    monkeypatch.setattr(dashboard, "retry_task", lambda *a: "re-queued")
    c, _a, run = web(cfg, rows={3: {"task_id": 3, "state": "blocked"}, 4: {"task_id": 4, "state": "working"}})
    assert "#3 re-queued" in c.post("/action/pipeline/retry", data={"task": "3"}).text
    assert "task 4 is working" in c.post("/action/pipeline/retry", data={"task": "4"}).text
    for bad in ("x", "3; ls", "-1", "", "٣"):
        assert "not a task id" in c.post("/action/pipeline/retry", data={"task": bad}).text
    assert "restarting the dashboard" in c.post("/action/service/restart", data={"unit": "claude-pipeline-web"}).text
    assert "unknown unit" in c.post("/action/service/restart", data={"unit": "explorer-nightly.timer"}).text
    assert run.calls[-1][0][0] == "systemd-run"


def test_route_restart_refused_while_busy(cfg):
    c, _a, run = web(cfg, explorer={"run": {"id": "r9", "reason": "recheck"}})
    r = c.post("/action/service/restart", data={"unit": "explorer-serve"})
    assert "✗" in r.text and "explorer is running a recheck run (r9)" in r.text and not run.calls


def test_routes_log_every_action_with_the_origin(cfg):
    c, a, _ = web(cfg)
    c.post("/action/explorer/run")
    c.post("/action/service/restart", data={"unit": "sshd"})
    rows = a.recent()
    assert [x["action"] for x in rows] == ["service/restart", "explorer/run"]
    assert rows[0]["refused"] and rows[0]["target"] == "sshd" and rows[0]["ip"]


# --- buttons and the Recent actions card ---------------------------------------------------------
EXPL = {
    "schema": 1, "service": "explorer", "uat": {}, "spend_today_usd": 1.0, "daily_cap_usd": 50, "run": None, "active_run": None, "findings": {"items": []},
    "questions": {"open": [
        {"id": QID, "kind": "escalation", "question": "What now?", "route": "billing",
         "asked": "2026-11-25T18:00:00+00:00", "options": ["Fix it", "Leave it", "Drop it"]},
        {"id": "q-aaaaaaaaaaaa", "kind": "info", "question": "FYI", "options": ["x"]}]},
    "batches": [{"id": "B-7", "state": "veto", "findings": 2}, {"id": "B-8", "state": "held", "findings": 1},
                {"id": "B-9", "state": "sent", "findings": 1}],
}
PIPE = {"paused": False, "today_cost": 1.0, "cap": 50.0, "max_workers": 2, "active": 0,
        "tasks": [{"task_id": 6, "slug": "t-6", "state": "blocked", "cost": 1, "retries": "0/0", "pr_url": "",
                   "elapsed": "-", "activity": "", "started_at": None, "updated_at": None, "url": ""},
                  {"task_id": 7, "slug": "t-7", "state": "working", "cost": 1, "retries": "0/0", "pr_url": "",
                   "elapsed": "-", "activity": "", "started_at": None, "updated_at": None, "url": ""}]}
SERVICES = {"available": True, "units": [
    {"unit": u, "description": "", "installed": True, "enabled": "enabled", "active": "active", "sub": "running",
     "result": "", "since": None, "main_pid": 1, "memory_mb": 1.0, "cpu_sec": 1, "restarts": 0,
     "is_timer": u.endswith(".timer"), "last_run": None, "next_run": None, "processes": []}
    for u in UNITS.split(",")]}


def page_client(cfg, explorer=None, enabled=True):
    import dataclasses
    c = dataclasses.replace(cfg, dashboard_actions=enabled)
    readers = {"pipeline": lambda: PIPE, "board": lambda: {"buckets": [], "releases": []},
               "planner": lambda: {"investigations": []}, "explorer": lambda: explorer or EXPL,
               "services": lambda: SERVICES}
    a = Actions(c, Sources(c, readers=readers), run=Runner(), ledger_factory=lambda: FakeLedger(), now=lambda: NOW)
    return TestClient(create_app(c, a.sources, a)), a


def test_buttons_appear_only_with_actions_on(cfg):
    on, _ = page_client(cfg)
    off, _ = page_client(cfg, enabled=False)
    for tab in ("explorer", "pipeline", "services", "overview"):
        assert "hx-post" in on.get(f"/section/{tab}").text or tab == "overview"
        assert "hx-post" not in off.get(f"/section/{tab}").text
    assert 'id="action-result"' in on.get("/").text and "read-only" not in on.get("/").text
    assert "Recent actions" not in off.get("/section/overview").text
    assert "Recent actions" in on.get("/section/overview").text


def test_explorer_buttons(cfg):
    t = page_client(cfg)[0].get("/section/explorer").text
    assert "Ask explorer to run now? It starts within a few minutes, after any run in progress." in t
    assert t.count("/action/explorer/answer") == 3        # the info question gets none
    assert "Answer q-0123456789ab with B: Leave it?" in t and 'title="Drop it"' in t
    assert "Send B-7 to the planner now?" in t and ">Hold<" in t and ">Unhold<" in t
    assert t.count(">Send<") == 1 and t.count(">Unhold<") == 1


def test_explorer_lane_buttons(cfg):
    ex = {**EXPL, "lanes": [{"lane": "billing", "spent_today": 1, "budget": 5}]}
    t = page_client(cfg, explorer=ex)[0].get("/section/explorer").text
    assert "Ask explorer to run lane billing now?" in t and "Global run" in t and "&#34;lane&#34;" not in t


def test_pipeline_and_services_buttons(cfg):
    c, _ = page_client(cfg)
    t = c.get("/section/pipeline").text
    assert t.count(">Retry<") == 1 and "Retry #6? It goes back to Ready and is claimed again." in t
    s = c.get("/section/services").text
    assert s.count(">Restart<") == 4 and "Restart explorer-serve? It is refused if the service is busy." in s
    assert "Restart explorer-nightly.timer" not in s


def test_every_action_is_logged_and_shown_on_overview(cfg):
    c, _ = page_client(cfg)
    c.post("/action/explorer/run")
    c.post("/action/service/restart", data={"unit": "sshd"})
    with cfg.dashboard_action_log.open("a") as f:
        f.write("{broken\n")
    t = c.get("/section/overview").text
    assert "Recent actions" in t and "explorer/run" in t and "service/restart" in t
    assert "refused" in t and "unknown unit" in t and "✓" in t
    assert t.index("service/restart") < t.index("explorer/run")       # newest first
