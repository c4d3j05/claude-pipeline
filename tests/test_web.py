"""The read-only web dashboard (OpenSpec change web-dashboard): sources, alerts and pages."""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
import subprocess

import pytest
from fastapi.testclient import TestClient

from dispatcher import config as cfgmod
from dispatcher.db import Ledger
from dispatcher.web import sources as srcmod
from dispatcher.web import views
from dispatcher.web.app import create_app
from dispatcher.web.sources import Result, Sources, read_board, read_pipeline, read_status

NOW = dt.datetime(2026, 11, 22, 20, 0, tzinfo=dt.UTC)

PLANNER = {
    "schema": 1, "service": "planner", "generated_at": "2026-11-22T19:59:00+00:00",
    "spend_today_usd": 3.5, "daily_cap_usd": 70, "listener": {"running": True, "since": "2026-11-22T15:48:46-05:00"},
    "investigations": [{"id": "63d6fd5b", "state": "provisioning", "flow": None, "source": "batch", "batch_id": "B-1",
                        "spent_usd": 0.4, "cap_usd": 40, "created": "2026-11-22T19:50:00+00:00",
                        "updated": "2026-11-22T19:58:00+00:00",
                        "thread_permalink": "https://ignify.slack.com/archives/C0CKZ58NUAJ/p1795395945652879",
                        "title": "Batch B-1", "future_field": 1}],
    "filed": [{"investigation": "a1b2c3d4", "filed_at": "2026-11-21T10:00:00+00:00", "release": None,
               "tickets": [{"key": "a", "title": "Fix the export", "vikunja_task_id": 531,
                            "url": "https://vk.example/tasks/531"}]}],
}

EXPLORER = {
    "schema": 1, "service": "explorer", "generated_at": "2026-11-22T19:59:30+00:00",
    "uat": {"commit": "62ad7fc0000", "last_tick": "2026-11-22T19:58:00+00:00", "main_head": "99fa4e70000",
            "ahead_since": "2026-11-22T19:30:00+00:00"},
    "spend_today_usd": 21.24, "daily_cap_usd": 50, "paused": None, "active_run": None,
    "runs": [{"id": "9df186db", "commit": "62ad7fc", "day": "2026-11-22", "reason": "resume", "state": "done",
              "spent_usd": 3.48, "findings": 7}],
    "findings": {"counts": {"open": 1, "escalated": 1, "resolved": 1},
                 "items": [
                     {"id": "f-5a9e5f62fc10", "title": "Export button does nothing", "kind": "dead_control",
                      "type": "bug", "state": "open", "route": "contacts", "first_seen": "2026-11-20T00:00:00+00:00",
                      "last_seen": "2026-11-22T18:00:00+00:00", "tasks": [], "batch_id": "B-1", "hotfix_id": None,
                      "failed_fixes": 0, "escalation": None,
                      "permalink": "https://ignify.slack.com/archives/C0CL21U2ZKK/p1"},
                     {"id": "f-b4cd06d67232", "title": "Invoices 500", "kind": "server_error", "type": "bug",
                      "state": "escalated", "route": "billing", "first_seen": "2026-11-19T00:00:00+00:00",
                      "last_seen": "2026-11-22T17:00:00+00:00", "tasks": [41, 42], "batch_id": None,
                      "hotfix_id": "H-b4cd06d67232-2", "failed_fixes": 2,
                      "escalation": {"reason": "fix_attempts", "question": "q-1"},
                      "permalink": "javascript:alert(1)"},
                     {"id": "f-1c728d3a101b", "title": "Old bug", "kind": "dead_control", "type": "bug",
                      "state": "resolved", "route": "home", "first_seen": "2026-11-10T00:00:00+00:00",
                      "last_seen": "2026-11-15T00:00:00+00:00", "tasks": [30], "batch_id": None,
                      "hotfix_id": None, "failed_fixes": 0, "escalation": None, "permalink": None}]},
    "questions": {"open": [{"id": "q-1", "kind": "escalation", "route": "billing",
                            "question": "Finding f-b4cd06d67232 still reproduces after 2 fixes. What now?",
                            "asked": "2026-11-22T18:00:00+00:00", "permalink": None}],
                  "answered_recent": [{"id": "q-0", "question": "Is STAFF allowed?", "answer": "A",
                                       "answered": "2026-11-21T09:00:00+00:00"}]},
    "batches": [{"id": "B-1", "state": "sent", "findings": 3, "release": None, "sent": "2026-11-22T20:54:44+00:00",
                 "resends": 2, "events": ["sent to the planner", "resent"]}],
    "hotfixes": [{"id": "H-b4cd06d67232-2", "finding": "f-b4cd06d67232", "status": "received",
                  "sent": "2026-11-22T12:00:00+00:00"}],
}

PIPELINE = {"paused": False, "today_cost": 4.0, "cap": 50.0, "max_workers": 2, "active": 1,
            "tasks": [{"task_id": 7, "slug": "task-7-export", "state": "working", "cost": 1.2, "retries": "0/0",
                       "pr_url": "", "elapsed": "03:10", "cancel": False, "activity": "Reading files",
                       "started_at": "2026-11-22T14:00:00", "updated_at": "2026-11-22T14:03:00",
                       "url": "https://vk.example/tasks/7"},
                      {"task_id": 6, "slug": "task-6-import", "state": "blocked", "cost": 2.8, "retries": "2/0",
                       "pr_url": "https://github.com/o/r/pull/12", "elapsed": "-", "cancel": False, "activity": "",
                       "started_at": "2026-11-22T10:00:00", "updated_at": "2026-11-22T11:00:00",
                       "url": "https://vk.example/tasks/6"}]}

BOARD = {"buckets": [{"title": "Ready", "tasks": [{"id": 9, "title": "Release R-1", "done": False,
                                                   "labels": ["release-tracker"], "tracker": True, "updated": "",
                                                   "url": "https://vk.example/tasks/9"}]},
                     {"title": "Blocked", "tasks": []}],
         "releases": [{"id": 9, "title": "Release R-1", "done": False, "labels": ["release-tracker"],
                       "tracker": True, "updated": "", "url": "https://vk.example/tasks/9"}]}


@pytest.fixture
def cfg(tmp_path):
    env = tmp_path / ".env"
    env.write_text("VIKUNJA_URL=https://vk.example/api/v1\nVIKUNJA_API_TOKEN=t\nVIKUNJA_PROJECT_ID=51\n"
                   f"PIPELINE_DB={tmp_path / 'p.db'}\nDASHBOARD_REFRESH_SEC=15\n")
    return cfgmod.load(str(env))


def fake_sources(cfg, **override):
    readers = {"pipeline": lambda: PIPELINE, "board": lambda: BOARD,
               "planner": lambda: PLANNER, "explorer": lambda: EXPLORER}
    readers.update(override)
    return Sources(cfg, readers=readers)


def client(cfg, **override) -> TestClient:
    return TestClient(create_app(cfg, fake_sources(cfg, **override)))


# --- settings -----------------------------------------------------------------------------------
def test_dashboard_settings_and_defaults(tmp_path):
    c = cfgmod.load(str(tmp_path / "missing.env"))
    assert (c.dashboard_host, c.dashboard_port, c.dashboard_refresh_sec) == ("127.0.0.1", 8090, 15)
    assert c.dashboard_planner_dir == c.dashboard_explorer_dir == ""
    env = tmp_path / ".env"
    env.write_text("DASHBOARD_HOST=0.0.0.0\nDASHBOARD_PORT=9000\nDASHBOARD_REFRESH_SEC=1\n"
                   "DASHBOARD_PLANNER_DIR=~/repos/claude-planner\n")
    c = cfgmod.load(str(env))
    assert (c.dashboard_host, c.dashboard_port, c.dashboard_refresh_sec) == ("0.0.0.0", 9000, 5)
    assert c.dashboard_planner_dir == "~/repos/claude-planner"


# --- status --json from planner and explorer --------------------------------------------------------
def run_returning(stdout="", rc=0, stderr="", exc=None):
    calls = []

    def run(cmd, **kw):
        calls.append((cmd, kw))
        if exc:
            raise exc
        return subprocess.CompletedProcess(cmd, rc, stdout, stderr)
    run.calls = calls
    return run


def test_read_status_runs_the_cli_in_the_checkout(tmp_path):
    run = run_returning(json.dumps(PLANNER))
    assert read_status("planner", str(tmp_path), run=run)["service"] == "planner"
    cmd, kw = run.calls[0]
    assert cmd == ["uv", "run", "--quiet", "claude-planner", "status", "--json"]
    assert kw["cwd"] == tmp_path and kw["timeout"] == srcmod.STATUS_TIMEOUT_SEC


@pytest.mark.parametrize(("run", "message"), [
    (run_returning("", rc=2, stderr="boom\nlast line"), "exited 2: last line"),
    (run_returning("not json"), "did not print JSON"),
    (run_returning(json.dumps({"schema": 2})), "unexpected status schema 2"),
    (run_returning(exc=subprocess.TimeoutExpired("uv", 20)), "took longer than 20s"),
    (run_returning(exc=FileNotFoundError("uv")), "could not run uv"),
])
def test_read_status_failures_are_short_reasons(tmp_path, run, message):
    with pytest.raises(ValueError, match=message):
        read_status("explorer", str(tmp_path), run=run)


def test_read_status_needs_the_checkout(tmp_path):
    with pytest.raises(ValueError, match="is not a directory"):
        read_status("planner", str(tmp_path / "nope"), run=run_returning("{}"))


# --- the cache and broken sources -------------------------------------------------------------------
def test_each_source_is_read_once_per_period(cfg):
    now = [0.0]
    calls = []
    s = Sources(cfg, clock=lambda: now[0], readers={"pipeline": lambda: calls.append(1) or PIPELINE,
                                                     "board": None, "planner": None, "explorer": None})
    for _ in range(5):
        assert s.get("pipeline").ok
    assert len(calls) == 1
    now[0] = 16.0
    s.get("pipeline")
    assert len(calls) == 2


def test_a_failing_or_unconfigured_source_is_a_result_not_an_exception(cfg):
    def boom():
        raise RuntimeError("database is locked")
    s = Sources(cfg, readers={"pipeline": boom, "board": None, "planner": None, "explorer": None})
    r = s.get("pipeline")
    assert not r.ok and r.configured and "database is locked" in r.error
    assert s.get("planner").configured is False and not s.get("planner").ok


def test_default_readers_follow_the_settings(cfg):
    s = Sources(cfg)
    assert s._readers["planner"] is None and s._readers["explorer"] is None
    assert s._readers["board"] is not None
    s2 = Sources(dataclasses.replace(cfg, dashboard_planner_dir="/x", vikunja_url=""))
    assert s2._readers["planner"] is not None and s2._readers["board"] is None


# --- pipeline and board readers ----------------------------------------------------------------------
def test_read_pipeline_adds_times_and_task_links(cfg):
    ledger = Ledger(cfg.db_path)
    ledger.upsert_claim(7, "task-7-export", "task-7-export")
    ledger.upsert_claim(8, "task-8-other", "task-8-other")
    ledger.update(8, state="done")
    st = read_pipeline(cfg, ledger, logs_root=cfg.db_path.parent / "logs")
    ledger.close()
    assert {t["task_id"] for t in st["tasks"]} == {7, 8}
    assert all(t["updated_at"] and t["url"] == f"https://vk.example/tasks/{t['task_id']}" for t in st["tasks"])


class FakeVikunja:
    def kanban_view_id(self, project_id):
        assert project_id == 51
        return 3

    def board(self, project_id, view):
        return [{"title": "Done", "tasks": [{"id": 1, "title": "old", "done": True, "labels": [],
                                             "updated": "2026-11-20T00:00:00Z"}]},
                {"title": "In Progress", "tasks": []},
                {"title": "Ready", "tasks": [{"id": 9, "title": "Release R-1", "labels": [{"title": "release-tracker"}]},
                                             {"id": 10, "title": "Fix", "labels": [{"title": "claude"}]}]}]


def test_read_board_orders_buckets_like_the_pipeline_and_finds_releases(cfg):
    b = read_board(cfg, FakeVikunja())
    assert [x["title"] for x in b["buckets"]] == ["Ready", "In Progress", "Done"]
    assert [t["id"] for t in b["releases"]] == [9]
    assert b["buckets"][0]["tasks"][0]["url"].startswith("https://vk.example/tasks/")


# --- alerts and overview -----------------------------------------------------------------------------
def results(**override) -> dict[str, Result]:
    base = {"pipeline": Result("pipeline", data=PIPELINE), "board": Result("board", data=BOARD),
            "planner": Result("planner", data=PLANNER), "explorer": Result("explorer", data=EXPLORER)}
    base.update(override)
    return base


def test_alerts_cover_what_needs_the_owner():
    texts = [a.text for a in views.alerts(results(), now=NOW)]
    assert any("1 task(s) blocked: #6" in t for t in texts)
    assert any("main (99fa4e7) is ahead of UAT (62ad7fc) for 30 min" in t for t in texts)
    assert any("B-1 sent but not picked up" in t and "resent 2" in t for t in texts)
    assert any("1 finding(s) wait for your answer" in t for t in texts)


def test_alerts_for_broken_or_stopped_services():
    stopped = {**PLANNER, "listener": {"running": False}}
    out = views.alerts(results(explorer=Result("explorer", error="timed out"),
                               planner=Result("planner", data=stopped)), now=NOW)
    assert [a.service for a in out if a.level == "bad"].count("planner") == 1
    assert any(a.service == "explorer" and "timed out" in a.text for a in out)
    assert not views.alerts(results(pipeline=Result("pipeline", data={**PIPELINE, "tasks": []}),
                                    explorer=Result("explorer", configured=False),
                                    planner=Result("planner", configured=False)), now=NOW)


def test_spend_and_health():
    rows = {r["service"]: r for r in views.spend(results())}
    assert rows["explorer"]["pct"] == 42 and rows["pipeline"]["pct"] == 8 and rows["planner"]["cap"] == 70
    h = {x["service"]: x for x in views.health(results(explorer=Result("explorer", configured=False)))}
    assert h["pipeline"]["state"] == "up" and h["explorer"]["state"] == "off"
    assert "1 open investigation" in h["planner"]["detail"]


def test_findings_filter():
    rows, counts = views.findings(EXPLORER, "open")
    assert [f["id"] for f in rows] == ["f-5a9e5f62fc10", "f-b4cd06d67232"] and counts["escalated"] == 1
    assert [f["id"] for f in views.findings(EXPLORER, "escalated")[0]] == ["f-b4cd06d67232"]
    assert len(views.findings(EXPLORER, "all")[0]) == 3


def test_ago_reads_local_and_aware_times():
    assert views.ago("2026-11-22T19:30:00+00:00", now=NOW) == "30 min ago"
    assert views.ago(None) == "-" and views.ago("nonsense") == "-"


def test_until_reads_future_times():
    now = dt.datetime(2026, 11, 25, 12, 0, tzinfo=dt.UTC)
    assert views.until("2026-11-25T15:00:00+00:00", now=now) == "in 3 h"
    assert views.until("2026-11-25T12:12:00+00:00", now=now) == "in 12 min"
    assert views.until("2026-11-27T12:00:00+00:00", now=now) == "in 2 d"
    assert views.until("2026-11-25T11:00:00+00:00", now=now) == "due"
    assert views.until(None) == "-" and views.until("nope") == "-"


LANES = [
    {"lane": "accounts-contacts", "focus": None, "last_run": "2026-11-25T06:02:11+00:00",
     "next_run": "2026-11-26T06:00:00+00:00", "spent_today": 3.2, "budget": 10.0, "open_findings": 4},
    {"lane": "billing", "focus": {"batch": "B-4", "settled": 2, "total": 6}, "last_run": None,
     "next_run": None, "spent_today": 0, "budget": 10.0, "open_findings": 9},
]


def test_lanes_rows():
    rows = views.lanes({"lanes": LANES})
    assert [r["lane"] for r in rows] == ["accounts-contacts", "billing"]
    assert rows[0]["focus"] == "\u2014" and rows[0]["spend"] == "$3.20 / $10.00"
    assert rows[0]["over"] is False and rows[0]["open_findings"] == 4
    assert rows[1]["focus"] == "B-4 \u00b7 2/6" and rows[1]["spend"] == "$0.00 / $10.00"


def test_lanes_tolerates_bad_data():
    assert views.lanes({}) == [] and views.lanes({"lanes": "x"}) == [] and views.lanes({"lanes": []}) == []
    rows = views.lanes({"lanes": [7, {"focus": None},
                                  {"lane": "portal", "budget": 5, "spent_today": 5}]})
    assert len(rows) == 1 and rows[0]["lane"] == "portal"
    assert rows[0]["over"] is True and rows[0]["focus"] == "\u2014" and rows[0]["open_findings"] == 0


# --- pages -------------------------------------------------------------------------------------------
@pytest.mark.parametrize("path", ["/", "/board", "/pipeline", "/planner", "/explorer"])
def test_every_tab_is_a_shell_that_htmx_refreshes(cfg, path):
    r = client(cfg).get(path)
    tab = path.strip("/") or "overview"
    assert r.status_code == 200
    assert f'hx-get="/section/{tab}"' in r.text and 'hx-trigger="load, every 15s [!document.querySelector(\'#section details[open]\')]"' in r.text
    assert "/static/htmx.min.js" in r.text


def test_sections_render_the_sources(cfg):
    c = client(cfg)
    assert "Nothing needs you" not in c.get("/section/overview").text
    assert "Release R-1" in c.get("/section/board").text
    pipe = c.get("/section/pipeline").text
    assert "Reading files" in pipe and "#12" in pipe
    plan = c.get("/section/planner").text
    assert "63d6fd5b" in plan and "Fix the export" in plan and "https://vk.example/tasks/531" in plan
    ex = c.get("/section/explorer").text
    assert "Export button does nothing" in ex and "waits for you" in ex and "H-b4cd06d67232-2" in ex


def test_the_lanes_table_shows_each_lane(cfg):
    ex = client(cfg, explorer=lambda: {**EXPLORER, "lanes": LANES}).get("/section/explorer").text
    assert "<h2>Lanes" in ex and "accounts-contacts" in ex
    assert "B-4 \u00b7 2/6" in ex and "$3.20 / $10.00" in ex
    assert ex.index("Lanes") < ex.index("<h2>Findings")


def test_no_lanes_block_leaves_the_tab_as_it_was(cfg):
    ex = client(cfg).get("/section/explorer").text
    assert "<h2>Lanes" not in ex and "Export button does nothing" in ex


def test_explorer_filter_and_unsafe_links(cfg):
    ex = client(cfg).get("/section/explorer?state=escalated").text
    assert "Invoices 500" in ex and "Export button does nothing" not in ex
    assert "javascript:" not in ex                        # a non-http link is shown as text
    assert client(cfg).get("/section/explorer?state=bogus").status_code == 200


def test_a_broken_source_shows_an_error_card_and_the_page_still_works(cfg):
    def boom():
        raise RuntimeError("uv: command not found")
    c = client(cfg, explorer=boom, planner=None)
    ex = c.get("/section/explorer")
    assert ex.status_code == 200 and "could not be read" in ex.text and "uv: command not found" in ex.text
    assert "DASHBOARD_PLANNER_DIR" in c.get("/section/planner").text
    assert c.get("/section/overview").status_code == 200


def test_json_passthrough_health_and_read_only(cfg):
    c = client(cfg)
    body = c.get("/api/planner.json").json()
    assert body["ok"] and body["data"]["investigations"][0]["id"] == "63d6fd5b"
    assert c.get("/api/nope.json").status_code == 404
    assert c.get("/healthz").json() == {"ok": True}
    assert c.get("/section/nope").status_code == 404 and c.get("/nope").status_code == 404
    assert c.post("/section/pipeline").status_code == 405       # nothing on this server writes
    assert c.get("/static/style.css").status_code == 200


def test_cli_has_a_web_command(monkeypatch, tmp_path):
    from dispatcher import __main__ as cli
    from dispatcher.web import app as appmod

    seen = {}
    monkeypatch.setattr(appmod, "serve", lambda c: seen.setdefault("port", c.dashboard_port) and 0)
    env = tmp_path / ".env"
    env.write_text("DASHBOARD_PORT=8123\n")
    assert cli.main(["--env", str(env), "web"]) == 0
    assert seen["port"] == 8123


def test_long_activity_is_shown_whole_in_a_fold_and_the_terminal_cuts_it(cfg):
    from dispatcher import dashboard
    long = "The earlier gate run was killed by my own 590-second `timeout`, not a test failure; " + "x" * 200
    task = {**PIPELINE["tasks"][0], "activity": long}
    r = client(cfg, pipeline=lambda: {**PIPELINE, "tasks": [task]}).get("/section/pipeline")
    assert '<details class="more"><summary' in r.text and "x" * 200 in r.text
    assert dashboard._short(long).endswith("…") and len(dashboard._short(long)) == dashboard.SHORT
