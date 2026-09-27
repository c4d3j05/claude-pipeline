"""Unit tests for the dispatcher's pure logic (no network, no subprocess)."""

from __future__ import annotations

import tempfile
from pathlib import Path

from dispatcher import config as cfgmod
from dispatcher import dashboard, prompt, stream
from dispatcher.cleanup import task_id_from_branch
from dispatcher.db import Ledger


# --- slug / prompt ------------------------------------------------------
def test_slugify_and_task_slug():
    assert prompt.slugify("Add Email/Password Login!") == "add-emailpassword-login"
    assert prompt.task_slug(123, "Session persistence / auto-login") == "task-123-session-persistence-auto-login"
    assert prompt.task_slug(9, "") == "task-9-task"


def test_acceptance_criteria_extraction():
    desc = (
        "<h3>Purpose</h3><p>Do a thing.</p>"
        "<h3>Definition of Done</h3><ul>"
        "<li>Valid login returns 200</li>"
        "<li>Wrong password returns 401</li></ul>"
    )
    crit = prompt.acceptance_criteria(desc)
    assert "Valid login returns 200" in crit
    assert "Wrong password returns 401" in crit


def test_worker_prompt_contains_rules():
    p = prompt.worker_prompt(7, "Do X", "<p>details</p>", "task-7-do-x")
    assert "Do NOT push" in p
    assert "BLOCKED:" in p
    assert "task-7-do-x" in p
    assert "NTREE_WORKSPACE" in p


# --- stream parsing -----------------------------------------------------
def test_stream_parse_result_and_session():
    lines = [
        '{"type":"system","subtype":"init","session_id":"abc-123"}',
        '{"type":"assistant","message":{"content":[{"type":"text","text":"working"},{"type":"tool_use","name":"Edit"}]}}',
        '{"type":"assistant","message":{"content":[{"type":"text","text":"BLOCKED: missing API key"}]}}',
        '{"type":"result","total_cost_usd":0.42,"duration_ms":1000,"num_turns":5,"is_error":false,"result":"done summary"}',
    ]
    r = stream.parse_lines(lines)
    assert r.session_id == "abc-123"
    assert r.tool_uses == 1
    assert r.cost_usd == 0.42
    assert r.num_turns == 5
    assert stream.blocked_reason(r) == "missing API key"


def test_stream_permission_denials():
    lines = [
        '{"type":"user","message":{"content":[{"type":"tool_result","is_error":true,"content":"permission denied for Bash(git push)"}]}}',
    ]
    r = stream.parse_lines(lines)
    assert len(r.permission_denials) == 1


# --- cleanup branch parsing --------------------------------------------
def test_task_id_from_branch():
    assert task_id_from_branch("task-123-foo-bar") == 123
    assert task_id_from_branch("feature/x") is None


# --- ledger cost + state ------------------------------------------------
def test_ledger_cost_and_state():
    with tempfile.TemporaryDirectory() as d:
        led = Ledger(Path(d) / "t.db")
        led.upsert_claim(1, "task-1-x", "task-1-x")
        assert led.active_count() == 1
        led.add_cost(1, 1.25)
        led.add_cost(1, 0.75)
        assert led.get(1)["cost_usd"] == 2.0
        assert led.today_cost() == 2.0
        assert not led.cap_already_notified()
        led.mark_cap_notified()
        assert led.cap_already_notified()
        led.update(1, state="done")
        assert led.active_count() == 0
        led.close()


# --- control: pause + cancel -------------------------------------------
def test_ledger_pause_and_cancel():
    with tempfile.TemporaryDirectory() as d:
        led = Ledger(Path(d) / "t.db")
        assert led.is_paused() is False
        led.set_paused(True)
        assert led.is_paused() is True
        led.upsert_claim(7, "task-7-x", "task-7-x")
        led.update(7, state="working")
        assert led.cancel_requested(7) is False
        led.request_cancel(7)
        assert led.cancel_requested(7) is True
        assert [r["task_id"] for r in led.pending_cancels()] == [7]
        led.delete_task(7)
        assert led.get(7) is None
        led.close()


# --- config hot-reload + .env editing ----------------------------------
def test_config_file_wins_and_set_values():
    with tempfile.TemporaryDirectory() as d:
        envf = Path(d) / ".env"
        envf.write_text("MAX_WORKERS=2\nDAILY_CAP_USD=25\nVIKUNJA_PROJECT_ID=49\n")
        cfg = cfgmod.load(str(envf))
        assert cfg.max_workers == 2 and cfg.daily_cap_usd == 25.0 and cfg.project_id == 49
        # Editing the file changes what the next load() sees (hot-reload path).
        cfgmod.set_values(str(envf), {"MAX_WORKERS": "5"})
        assert cfgmod.load(str(envf)).max_workers == 5
        # set_values preserves the other keys.
        assert cfgmod.load(str(envf)).project_id == 49


# --- dashboard status gathering ----------------------------------------
def test_gather_status_and_snapshot():
    with tempfile.TemporaryDirectory() as d:
        envf = Path(d) / ".env"
        envf.write_text("MAX_WORKERS=2\nDAILY_CAP_USD=25\n")
        cfg = cfgmod.load(str(envf))
        led = Ledger(Path(d) / "t.db")
        led.upsert_claim(650, "task-650-x", "task-650-x")
        led.update(650, state="working")
        led.add_cost(650, 0.5)
        st = dashboard.gather_status(cfg, led, logs_root=Path(d))
        assert st["active"] == 1 and st["max_workers"] == 2
        assert st["tasks"][0]["task_id"] == 650
        text = dashboard.snapshot_text(cfg, led, logs_root=Path(d))
        assert "650" in text and "working" in text
        led.close()
