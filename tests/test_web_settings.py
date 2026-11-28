"""Dashboard settings (OpenSpec change dashboard-settings): allow-list, read, validate, write."""

from __future__ import annotations

import datetime as dt
import os
import stat

import pytest

from dispatcher import config as cfgmod
from dispatcher.web import settings

NOW = dt.datetime(2026, 11, 27, 10, 0, 0, tzinfo=dt.UTC)
SECRET = "xoxb-SECRET-TOKEN-123"
EXPLORER_ENV = (
    "# explorer\n"
    f"SLACK_BOT_TOKEN={SECRET}\n"
    "\n"
    "RECHECK_WALL_CLOCK_MIN=15  # minutes\n"
    "DAILY_CAP_USD='75'\n"
    "DB_PASSWORD=hunter2\n"
    "#RUN_CAP_USD=999\n"
)


@pytest.fixture
def cfg(tmp_path):
    expl, plan = tmp_path / "explorer", tmp_path / "planner"
    expl.mkdir()
    plan.mkdir()
    (expl / ".env").write_text(EXPLORER_ENV)
    (plan / ".env").write_text(f"SLACK_BOT_TOKEN={SECRET}\nTHREAD_CAP_USD=4\n")
    env = tmp_path / ".env"
    env.write_text(f"VIKUNJA_API_TOKEN={SECRET}\nMAX_WORKERS=3\nDASHBOARD_PLANNER_DIR={plan}\n"
                   f"DASHBOARD_EXPLORER_DIR={expl}\nPIPELINE_DB={tmp_path / 'p.db'}\n"
                   f"DASHBOARD_ACTION_LOG={tmp_path / 'actions.jsonl'}\n")
    return cfgmod.load(str(env))


def test_allow_list_defaults_match_the_code_defaults():
    c = cfgmod.load("/nonexistent.env")
    p = {s.key: s for s in settings.ALLOWED["pipeline"]}
    assert float(p["DAILY_CAP_USD"].default) == c.daily_cap_usd
    assert float(p["TASK_CAP_USD"].default) == c.task_cap_usd
    assert int(p["MAX_WORKERS"].default) == c.max_workers
    assert int(p["WALL_CLOCK_LIMIT_MIN"].default) == c.wall_clock_limit_min
    assert int(p["MAX_TURNS"].default) == c.max_turns
    for key in ("RELEASE_TAGS", "RELEASE_AUTO_MERGE", "RELEASE_MERGE_TO_MAIN"):
        assert p[key].default == "false"
    assert p["RELEASE_TAGS"].restart is None and p["RELEASE_AUTO_MERGE"].restart


def test_read_returns_only_allow_listed_keys(cfg):
    got = settings.read(cfg, "explorer")
    assert set(got) == {s.key for s in settings.ALLOWED["explorer"]}
    assert got["RECHECK_WALL_CLOCK_MIN"] == "15" and got["DAILY_CAP_USD"] == "75"
    assert got["RUN_CAP_USD"] is None            # only a comment mentions it
    flat = repr(got) + repr(settings.read(cfg, "planner")) + repr(settings.read(cfg, "pipeline"))
    assert SECRET not in flat and "hunter2" not in flat
    assert settings.read(cfg, "pipeline")["MAX_WORKERS"] == "3"
    assert settings.read(cfg, "nope") == {}


def test_validate_kinds_ranges_and_cross_checks():
    cur = {"DAILY_CAP_USD": "75", "RUN_CAP_USD": "10", "LANE_DAILY_CAP_USD": "10"}
    v = settings.validate
    assert v("explorer", "RECHECK_WALL_CLOCK_MIN", "25", cur) is None
    assert v("explorer", "RECHECK_WALL_CLOCK_MIN", "2", cur) == \
        "RECHECK_WALL_CLOCK_MIN must be between 3 and 60"
    assert "whole number" in v("explorer", "RECHECK_WALL_CLOCK_MIN", "1.5", cur)
    assert "whole number" in v("explorer", "RECHECK_WALL_CLOCK_MIN", "5\nX=1", cur)
    assert v("explorer", "EXPLORE_CAP_USD", "0.05", cur) == "EXPLORE_CAP_USD must be at least 0.1"
    assert v("explorer", "EXPLORE_CAP_USD", "nan", cur)
    assert v("explorer", "EXPLORE_CAP_USD", "1e3", cur)
    assert v("pipeline", "RELEASE_TAGS", "Yes", {}) is None
    assert v("pipeline", "RELEASE_TAGS", "maybe", {}) == "RELEASE_TAGS must be true or false"
    assert settings.normalize("pipeline", "RELEASE_TAGS", "1") == "true"
    assert settings.normalize("pipeline", "RELEASE_TAGS", "no") == "false"
    assert settings.normalize("explorer", "EXPLORE_CAP_USD", "0.600") == "0.6"
    # cross-checks, with unset keys falling back to the code defaults
    assert "LANE_DAILY_CAP_USD must not exceed DAILY_CAP_USD" in \
        v("explorer", "LANE_DAILY_CAP_USD", "80", cur)
    assert "RUN_CAP_USD must not exceed DAILY_CAP_USD" in v("explorer", "RUN_CAP_USD", "76", cur)
    assert "DAILY_CAP_USD must not be below" in v("explorer", "DAILY_CAP_USD", "5", cur)
    assert v("pipeline", "TASK_CAP_USD", "60", {}) is not None      # default daily cap is 50
    assert v("pipeline", "TASK_CAP_USD", "60", {"DAILY_CAP_USD": "100"}) is None
    assert v("planner", "THREAD_CAP_USD", "25", {}) is not None
    # unknown service or key
    assert v("explorer", "SLACK_BOT_TOKEN", "x", cur) is not None
    assert v("nope", "DAILY_CAP_USD", "5", cur) is not None


def test_write_replaces_one_line_keeps_the_rest_and_backs_up(cfg, tmp_path):
    path = tmp_path / "explorer" / ".env"
    os.chmod(path, 0o600)
    backup = settings.write(cfg, "explorer", "RECHECK_WALL_CLOCK_MIN", "25", NOW)
    assert backup.name == ".env.bak-20261127100000" and backup.read_text() == EXPLORER_ENV
    assert path.read_text() == EXPLORER_ENV.replace("RECHECK_WALL_CLOCK_MIN=15  # minutes",
                                                    "RECHECK_WALL_CLOCK_MIN=25")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert not [p for p in path.parent.iterdir() if p.suffix == ".tmp"]
    # keep the newest 10 backups
    for i in range(1, 12):
        settings.write(cfg, "explorer", "RECHECK_WALL_CLOCK_MIN", str(20 + i),
                       NOW + dt.timedelta(seconds=i))
    baks = sorted(p.name for p in path.parent.glob(".env.bak-*"))
    assert len(baks) == 10 and baks[0] == ".env.bak-20261127100002" \
        and baks[-1] == ".env.bak-20261127100011"


def test_write_appends_an_absent_key(cfg, tmp_path):
    path = tmp_path / "planner" / ".env"
    path.write_text("THREAD_CAP_USD=4")          # no trailing newline
    settings.write(cfg, "planner", "IDLE_EXPIRE_HOURS", "48", NOW)
    assert path.read_text() == ("THREAD_CAP_USD=4\n# set from the dashboard 2026-11-27\n"
                                "IDLE_EXPIRE_HOURS=48\n")
    assert settings.read(cfg, "planner")["IDLE_EXPIRE_HOURS"] == "48"


def test_write_refuses_before_touching_any_file(cfg, tmp_path):
    path = tmp_path / "explorer" / ".env"
    for service, key, value in [("explorer", "SLACK_BOT_TOKEN", "x"), ("explorer", "PATH", "x"),
                                ("shell", "DAILY_CAP_USD", "5"), ("explorer", "RECHECK_WALL_CLOCK_MIN", "99"),
                                ("explorer", "RECHECK_WALL_CLOCK_MIN", "5\nSLACK_BOT_TOKEN=evil")]:
        with pytest.raises(ValueError):
            settings.write(cfg, service, key, value, NOW)
    assert path.read_text() == EXPLORER_ENV
    assert not list(path.parent.glob(".env.bak-*"))


# --- routes and page ----------------------------------------------------------------------------
import json

from fastapi.testclient import TestClient

from dispatcher.web.actions import Actions
from dispatcher.web.app import create_app
from dispatcher.web.sources import Sources


def make_client(cfg, log_path=None):
    readers = {"pipeline": lambda: {"tasks": []}, "board": lambda: {"buckets": [], "releases": []},
               "planner": lambda: {"investigations": []}, "explorer": lambda: {"run": None}}
    sources = Sources(cfg, readers=readers)
    acts = Actions(cfg, sources, now=lambda: NOW)
    return TestClient(create_app(cfg, sources, acts))


def with_cfg(cfg, **kw):
    import dataclasses
    return dataclasses.replace(cfg, **kw)


def log_lines(cfg):
    return [json.loads(x) for x in cfg.dashboard_action_log.read_text().splitlines()]


def test_page_lists_settings_and_never_a_secret(cfg, tmp_path):
    c = make_client(cfg)
    assert "settings" in cfg_tabs()
    bodies = [c.get("/settings").text, c.get("/section/settings").text]
    html = bodies[1]
    for body in bodies:
        assert SECRET not in body and "hunter2" not in body and "SLACK_BOT_TOKEN" not in body
    assert "RECHECK_WALL_CLOCK_MIN" in html and "default (10)" in html      # RUN_CAP_USD unset
    assert "restart explorer-serve" in html and "applies now" in html
    assert "Save" in html and 'name="value"' in html


def cfg_tabs():
    from dispatcher.web.app import TABS
    return TABS


def test_save_route_logs_and_reports_restart(cfg, tmp_path):
    c = make_client(cfg)
    r = c.post("/action/settings/save", data={"service": "explorer",
                                              "key": "RECHECK_WALL_CLOCK_MIN", "value": "25"})
    assert r.status_code == 200
    assert "RECHECK_WALL_CLOCK_MIN=25 saved; restart explorer-serve to apply" in r.text
    assert "Restart explorer-serve" in r.text and "/action/service/restart" in r.text
    env = (tmp_path / "explorer" / ".env").read_text()
    assert "RECHECK_WALL_CLOCK_MIN=25\n" in env and SECRET in env
    assert list((tmp_path / "explorer").glob(".env.bak-*"))
    (entry,) = log_lines(cfg)
    assert (entry["action"], entry["target"], entry["ok"]) == \
        ("settings/save", "explorer:RECHECK_WALL_CLOCK_MIN", True)
    assert entry["message"] == "RECHECK_WALL_CLOCK_MIN=25 saved; restart explorer-serve to apply"
    assert SECRET not in cfg.dashboard_action_log.read_text() and SECRET not in r.text
    # a refused value says why and is logged as refused
    r = c.post("/action/settings/save", data={"service": "explorer",
                                              "key": "RECHECK_WALL_CLOCK_MIN", "value": "99"})
    assert "RECHECK_WALL_CLOCK_MIN must be between 3 and 60" in r.text and "Restart" not in r.text
    assert log_lines(cfg)[-1]["refused"] is True


def test_save_route_hot_setting_applies_now(cfg, tmp_path):
    c = make_client(cfg)
    r = c.post("/action/settings/save", data={"service": "pipeline", "key": "MAX_WORKERS",
                                              "value": "4"})
    assert "MAX_WORKERS=4 saved; applies now" in r.text and "Restart" not in r.text
    assert "MAX_WORKERS=4\n" in (tmp_path / ".env").read_text()
    assert cfgmod.load(cfg.env_file).max_workers == 4
    assert log_lines(cfg)[-1]["message"] == "MAX_WORKERS=4 saved; applies now"


def test_unknown_key_or_service_is_refused_without_touching_the_file(cfg, tmp_path):
    c = make_client(cfg)
    before = {p: p.read_bytes() for p in tmp_path.rglob(".env")}
    for service, key in [("explorer", "SLACK_BOT_TOKEN"), ("pipeline", "VIKUNJA_API_TOKEN"),
                         ("planner", "PATH"), ("root", "MAX_WORKERS"), ("", "")]:
        r = c.post("/action/settings/save", data={"service": service, "key": key, "value": "1"})
        assert r.status_code == 200 and "not a setting" in r.text
    assert {p: p.read_bytes() for p in tmp_path.rglob(".env")} == before
    assert not list(tmp_path.rglob(".env.bak-*"))
    assert all(e["target"] == "unknown" and e["refused"] for e in log_lines(cfg))
    assert SECRET not in cfg.dashboard_action_log.read_text()


def test_actions_off_settings_are_read_only(cfg, tmp_path):
    off = with_cfg(cfg, dashboard_actions=False)
    c = make_client(off)
    html = c.get("/section/settings").text
    assert "RECHECK_WALL_CLOCK_MIN" in html and "Save" not in html and "<form" not in html
    assert SECRET not in html
    before = (tmp_path / "explorer" / ".env").read_text()
    r = c.post("/action/settings/save", data={"service": "explorer",
                                              "key": "RECHECK_WALL_CLOCK_MIN", "value": "25"})
    assert r.status_code == 404
    assert (tmp_path / "explorer" / ".env").read_text() == before


def test_unconfigured_service_shows_not_configured(cfg, tmp_path):
    bare = with_cfg(cfg, dashboard_planner_dir="", dashboard_explorer_dir=str(tmp_path / "gone"))
    c = make_client(bare)
    html = c.get("/section/settings").text
    assert html.count("not configured") == 2
    assert "MAX_INVESTIGATIONS" not in html and "MAX_WORKERS" in html
    r = c.post("/action/settings/save", data={"service": "planner", "key": "MAX_INVESTIGATIONS",
                                              "value": "2"})
    assert "planner is not configured" in r.text
