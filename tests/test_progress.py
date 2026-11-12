"""Stage-by-stage Slack updates: routing, content, and the heartbeat."""

from __future__ import annotations

import threading

import pytest

from dispatcher.notify import Slack

from .test_gaps import _dispatcher


class Http:
    def __init__(self):
        self.calls = []

    def __call__(self, url, **kw):
        self.calls.append(kw)
        return type("R", (), {"json": lambda s: {"ok": True}})()


class Hook:
    def __init__(self):
        self.sent = []

    def __call__(self, url, data=None, **kw):
        self.sent.append(data)


@pytest.fixture
def hook(monkeypatch):
    h = Hook()
    monkeypatch.setattr("dispatcher.notify.requests.post", h)
    return h


def slack(mode, thread, http=None):
    return Slack("https://hooks/x", "xoxb-t", thread_for=lambda t: thread, http_post=http or Http(),
                 progress=mode)


# --- routing -----------------------------------------------------------------------------------
def test_progress_goes_to_the_planner_thread(hook):
    http = Http()
    slack("all", ("D1", "1.2"), http).progress(7, "hello")
    assert len(http.calls) == 1 and http.calls[0]["json"]["thread_ts"] == "1.2" and hook.sent == []


def test_progress_without_a_thread_goes_to_the_webhook_in_all_mode(hook):
    slack("all", None).progress(7, "hello")
    assert len(hook.sent) == 1


def test_thread_mode_skips_tasks_that_have_no_thread(hook):
    http = Http()
    slack("thread", None, http).progress(7, "hello")
    assert hook.sent == [] and http.calls == []
    slack("thread", ("D1", "1.2"), http).progress(7, "hello")
    assert len(http.calls) == 1


def test_off_sends_no_progress_but_keeps_the_main_events(hook):
    http = Http()
    s = slack("off", ("D1", "1.2"), http)
    s.progress(7, "hello")
    assert http.calls == []
    s.pr_opened(7, "https://gh/pr/1")                  # PR opened is not progress: always sent
    assert len(http.calls) == 1


def test_config_reads_the_progress_settings(tmp_path):
    from dispatcher import config as cfgmod

    env = tmp_path / ".env"
    base = ("VIKUNJA_URL=https://v/api/v1\nVIKUNJA_API_TOKEN=t\nVIKUNJA_PROJECT_ID=1\n"
            f"TARGET_REPO_PATH={tmp_path}\n")
    env.write_text(base + "NOTIFY_PROGRESS=Thread\nPROGRESS_INTERVAL_MIN=3\n")
    cfg = cfgmod.load(str(env))
    assert cfg.notify_progress == "thread" and cfg.progress_interval_min == 3
    env.write_text(base + "NOTIFY_PROGRESS=banana\nPROGRESS_INTERVAL_MIN=-5\n")
    cfg = cfgmod.load(str(env))
    assert cfg.notify_progress == "all" and cfg.progress_interval_min == 0
    env.write_text(base)
    cfg = cfgmod.load(str(env))
    assert cfg.notify_progress == "all" and cfg.progress_interval_min == 10


# --- what the dispatcher says ------------------------------------------------------------------
class Said:
    def __init__(self):
        self.lines = []

    def progress(self, tid, text):
        self.lines.append((tid, text))


def test_a_failing_progress_post_never_breaks_the_dispatcher(tmp_path):
    d = _dispatcher(tmp_path)
    d.slack = type("S", (), {"progress": lambda self, t, x: (_ for _ in ()).throw(RuntimeError("x"))})()
    d._say(1, "hello")                                  # must not raise


def test_tests_are_announced_with_the_result(tmp_path, monkeypatch):
    d = _dispatcher(tmp_path)
    d.slack = Said()
    monkeypatch.setattr(d, "_gate", lambda slug: (True, ""))
    assert d._tests(7, "s") == (True, "")
    assert "Running the test suite" in d.slack.lines[0][1] and "Tests passed" in d.slack.lines[1][1]
    d.slack.lines.clear()
    monkeypatch.setattr(d, "_gate", lambda slug: (False, "boom"))
    assert d._tests(7, "s") == (False, "boom")
    assert "Tests failed" in d.slack.lines[1][1]


def test_baseline_check_announces_the_slow_first_run_and_the_cached_result(tmp_path, monkeypatch):
    d = _dispatcher(tmp_path)
    d.slack = Said()
    monkeypatch.setattr(d.ntree, "rev", lambda ref: "abc123", raising=False)
    monkeypatch.setattr(d, "_gate", lambda slug: (True, ""))
    assert d._baseline(7, tmp_path, "s", "origin/main") is None
    texts = [t for _, t in d.slack.lines]
    assert any("Checking that the base branch passes" in t for t in texts)
    assert any("Base branch is green" in t for t in texts)
    d.slack.lines.clear()
    assert d._baseline(8, tmp_path, "s", "origin/main") is None      # cached for this commit
    assert [t for _, t in d.slack.lines] == [":white_check_mark: Base branch already verified green for this commit."]


def test_review_is_announced_with_the_verdict(tmp_path, monkeypatch):
    import dispatcher.daemon as dmod

    d = _dispatcher(tmp_path)
    d.slack = Said()
    d.ledger.upsert_claim(7, "s", "s")
    for verdict, findings, needle in (("pass", [], "Review passed"),
                                      ("block", [{"a": 1}, {"b": 2}], "2 blocking issues")):
        monkeypatch.setattr(dmod, "adversarial_review",
                            lambda *a, v=verdict, f=findings: {"verdict": v, "findings": f, "cost_usd": 0})
        d.slack.lines.clear()
        d._review(7, tmp_path, "s", "spec", "origin/main")
        assert "Independent review started" in d.slack.lines[0][1] and needle in d.slack.lines[1][1]


# --- heartbeat ---------------------------------------------------------------------------------
def test_the_heartbeat_reports_the_stage_and_stops_when_the_task_ends(tmp_path):
    import dataclasses

    d = _dispatcher(tmp_path)
    d.slack = Said()
    d.cfg = dataclasses.replace(d.cfg, progress_interval_min=0)      # we drive the interval below
    d.ledger.upsert_claim(7, "s", "s")
    d.ledger.update(7, state="testing", cost_usd=1.5)

    class FastEvent(threading.Event):
        """wait() returns False (the interval elapsed) a few times, then True (task finished)."""

        def __init__(self, ticks):
            super().__init__()
            self.ticks = ticks

        def wait(self, timeout=None):
            self.ticks -= 1
            return self.ticks < 0

    d.cfg = dataclasses.replace(d.cfg, progress_interval_min=1)
    d._heartbeat(7, FastEvent(2))
    assert len(d.slack.lines) == 2
    assert "running tests" in d.slack.lines[0][1] and "$1.50" in d.slack.lines[0][1]
    d.slack.lines.clear()
    d.ledger.update(7, state="pr_open")
    d._heartbeat(7, FastEvent(5))
    assert d.slack.lines == []                                       # finished tasks stop the beat


def test_a_zero_interval_disables_the_heartbeat(tmp_path):
    import dataclasses

    d = _dispatcher(tmp_path)
    d.slack = Said()
    d.cfg = dataclasses.replace(d.cfg, progress_interval_min=0)
    d.ledger.upsert_claim(7, "s", "s")
    seen = []

    class OneCheck(threading.Event):
        def wait(self, timeout=None):
            seen.append(timeout)
            return True

    d._heartbeat(7, OneCheck())
    assert d.slack.lines == [] and seen == [60]


def test_claiming_announces_the_pickup(tmp_path):
    d = _dispatcher(tmp_path)
    d.slack = Said()
    d.move = lambda *a: None
    d.comment = lambda *a: None
    d.pool = type("P", (), {"submit": lambda self, *a, **k: None})()
    d.vk = type("VK", (), {"task": lambda self, tid: {"description": ""}})()
    d.claim_and_dispatch({"id": 7, "title": "Add the thing", "description": ""})
    assert d.slack.lines and "Picked up task 7" in d.slack.lines[0][1] and "Add the thing" in d.slack.lines[0][1]
