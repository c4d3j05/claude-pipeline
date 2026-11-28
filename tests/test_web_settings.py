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
                   f"DASHBOARD_EXPLORER_DIR={expl}\nPIPELINE_DB={tmp_path / 'p.db'}\n")
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
