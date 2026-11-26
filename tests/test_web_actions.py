"""Dashboard actions (OpenSpec change dashboard-actions): Actions, routes, buttons, the log."""

from __future__ import annotations

import datetime as dt
import json
import subprocess

import pytest

from dispatcher import config as cfgmod
from dispatcher import dashboard
from dispatcher.web.actions import Actions
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
