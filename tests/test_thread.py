"""Posting task events into the planner's Slack thread, and the selftest helpers."""

from __future__ import annotations

import pytest
import requests

from dispatcher import procenv, slackthread
from dispatcher.db import Ledger
from dispatcher.notify import Slack
from dispatcher.slackthread import Thread, parse_source
from dispatcher.vikunja import stuck_dependents

LINK = "https://ignify.slack.com/archives/D0CGS2L6KLK/p1699999999000100"


# --- reading the Source line -------------------------------------------------------------------
def test_source_permalink_gives_channel_and_thread_ts():
    d = f"### Definition of Done\n- [ ] a\n\nSource: {LINK} · investigation ab12 · ticket a"
    assert parse_source(d) == Thread("D0CGS2L6KLK", "1699999999.000100")


def test_thread_ts_query_parameter_wins():
    d = f"Source: {LINK}?thread_ts=1699990000.000200&cid=D0CGS2L6KLK"
    assert parse_source(d) == Thread("D0CGS2L6KLK", "1699990000.000200")


def test_source_in_html_description():
    d = f"<h3>Definition of Done</h3><ul><li>a</li></ul><p>Source: {LINK} · investigation ab12</p>"
    assert parse_source(d) == Thread("D0CGS2L6KLK", "1699999999.000100")


@pytest.mark.parametrize("desc", [
    "", None, "no source here", "Source: not a link", "Source: https://example.com/archives/D1/p1699999999000100",
    "Source: https://ignify.slack.com/messages/D1", "Source: https://ignify.slack.com/archives/x1/p1699999999000100",
    "mentions Source: later in a sentence",
])
def test_no_valid_source_means_no_thread(desc):
    assert parse_source(desc) is None


# --- posting -----------------------------------------------------------------------------------
class Http:
    def __init__(self, reply=None, exc=None):
        self.calls, self.reply, self.exc = [], reply or {"ok": True}, exc

    def __call__(self, url, **kw):
        self.calls.append((url, kw))
        if self.exc:
            raise self.exc
        return type("R", (), {"json": lambda s: self.reply})()


def test_post_uses_the_bot_token_channel_and_thread():
    http = Http()
    assert slackthread.post("xoxb-t", Thread("D1", "1.2"), "hi", http_post=http)
    url, kw = http.calls[0]
    assert url.endswith("chat.postMessage") and kw["headers"]["Authorization"] == "Bearer xoxb-t"
    assert kw["json"] == {"channel": "D1", "thread_ts": "1.2", "text": "hi", "unfurl_links": False}


@pytest.mark.parametrize("http", [Http({"ok": False, "error": "channel_not_found"}),
                                  Http(exc=requests.ConnectionError("down"))])
def test_a_refused_or_failed_post_returns_false(http):
    assert slackthread.post("t", Thread("D1", "1.2"), "hi", http_post=http) is False


# --- routing in the notifier -------------------------------------------------------------------
class Hook:
    def __init__(self):
        self.sent = []

    def __call__(self, url, data=None, **kw):
        self.sent.append((url, data))


@pytest.fixture
def hook(monkeypatch):
    h = Hook()
    monkeypatch.setattr("dispatcher.notify.requests.post", h)
    return h


def slack(http, thread, token="xoxb-t"):
    return Slack("https://hooks/x", token, thread_for=lambda tid: thread, http_post=http)


def test_task_events_go_to_the_thread_not_the_webhook(hook):
    http = Http()
    s = slack(http, ("D1", "1.2"))
    s.pr_opened(7, "https://gh/pr/1")
    s.task_blocked(7, "tests red", "https://v/7")
    s.task_merged(7, "https://gh/pr/1", 1.5)
    s.pr_needs_attention(7, "merge conflict", "https://gh/pr/1")
    assert len(http.calls) == 4 and hook.sent == []
    texts = [kw["json"]["text"] for _, kw in http.calls]
    assert "PR opened" in texts[0] and "blocked" in texts[1] and "merged" in texts[2] and "conflict" in texts[3]


def test_a_task_without_a_thread_uses_the_webhook(hook):
    http = Http()
    slack(http, None).pr_opened(7, "https://gh/pr/1")
    assert http.calls == [] and len(hook.sent) == 1


def test_no_bot_token_uses_the_webhook_even_with_a_thread(hook):
    http = Http()
    slack(http, ("D1", "1.2"), token="").pr_opened(7, "u")
    assert http.calls == [] and len(hook.sent) == 1


@pytest.mark.parametrize("http", [Http({"ok": False, "error": "invalid_auth"}),
                                  Http(exc=requests.Timeout("slow"))])
def test_a_failed_thread_post_falls_back_to_the_webhook(hook, http):
    slack(http, ("D1", "1.2")).task_blocked(7, "tests red", "https://v/7")
    assert len(http.calls) == 1 and len(hook.sent) == 1


def test_a_ledger_lookup_error_still_notifies(hook):
    def boom(tid):
        raise RuntimeError("db locked")

    Slack("https://hooks/x", "xoxb-t", thread_for=boom, http_post=Http()).pr_opened(7, "u")
    assert len(hook.sent) == 1


def test_other_messages_stay_on_the_webhook(hook):
    http = Http()
    s = slack(http, ("D1", "1.2"))
    s.daily_cap(5, 10)
    s.alert("poll failed")
    s.nightly("summary")
    assert http.calls == [] and len(hook.sent) == 3


# --- the ledger remembers the thread -----------------------------------------------------------
def test_ledger_stores_and_clears_the_thread_and_migrates_old_databases(tmp_path):
    import sqlite3

    old = tmp_path / "old.db"
    c = sqlite3.connect(old)
    c.execute("CREATE TABLE tasks (task_id INTEGER PRIMARY KEY, slug TEXT NOT NULL, branch TEXT NOT NULL,"
              " session_id TEXT, workspace TEXT, pid INTEGER, state TEXT NOT NULL, test_retries INTEGER"
              " NOT NULL DEFAULT 0, review_retries INTEGER NOT NULL DEFAULT 0, cost_usd REAL NOT NULL"
              " DEFAULT 0, pr_url TEXT, started_at TEXT NOT NULL, updated_at TEXT NOT NULL)")
    c.commit()
    c.close()
    led = Ledger(old)                                  # an existing database gets the new columns
    led.upsert_claim(7, "task-7-x", "task-7-x")
    assert led.thread(7) is None and led.thread(99) is None
    led.update(7, slack_channel="D1", slack_thread="1.2")
    assert led.thread(7) == ("D1", "1.2")
    led.upsert_claim(7, "task-7-x", "task-7-x")        # a re-queue keeps it until the claim re-reads
    assert led.thread(7) == ("D1", "1.2")
    led.update(7, slack_channel=None, slack_thread=None)
    assert led.thread(7) is None


# --- claiming reads the Source line ------------------------------------------------------------
def test_claim_stores_the_thread_from_the_description(tmp_path):
    from .test_gaps import _dispatcher

    d = _dispatcher(tmp_path)
    d.ledger.upsert_claim(1, "s", "s")
    d._remember_thread({"id": 1, "description": f"x\n\nSource: {LINK} · investigation ab12"})
    assert d.ledger.thread(1) == ("D0CGS2L6KLK", "1699999999.000100")
    d._remember_thread({"id": 1, "description": "a task with no source"})
    assert d.ledger.thread(1) is None                  # a re-claim without a Source line clears it


def test_claim_fetches_the_description_when_the_board_omits_it(tmp_path):
    from .test_gaps import _dispatcher

    d = _dispatcher(tmp_path)
    d.ledger.upsert_claim(1, "s", "s")
    d.vk = type("VK", (), {"task": lambda self, tid: {"description": f"Source: {LINK}"}})()
    d._remember_thread({"id": 1})
    assert d.ledger.thread(1) == ("D0CGS2L6KLK", "1699999999.000100")


def test_a_failure_reading_the_source_never_fails_the_claim(tmp_path):
    from .test_gaps import _dispatcher

    d = _dispatcher(tmp_path)
    d.vk = type("VK", (), {"task": lambda self, tid: (_ for _ in ()).throw(RuntimeError("boom"))})()
    d._remember_thread({"id": 1})                       # must not raise


# --- credentials -------------------------------------------------------------------------------
@pytest.mark.parametrize("scope", ["plain", "claude", "github"])
def test_the_bot_token_never_reaches_a_subprocess(monkeypatch, scope):
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-secret")
    monkeypatch.setenv("SLACK_APP_TOKEN", "xapp-secret")
    env = procenv.clean_env(scope)
    assert "SLACK_BOT_TOKEN" not in env and "SLACK_APP_TOKEN" not in env


# --- selftest: dependents that can never start --------------------------------------------------
def _t(tid, *blockers, labels=("claude",), done=False):
    rel = {"blocked": [{"id": b, "done": d} for b, d in blockers]} if blockers else {}
    return {"id": tid, "done": done, "labels": [{"title": x} for x in labels], "related_tasks": rel}


def test_stuck_dependents_reports_ready_tasks_whose_blocker_is_in_blocked():
    board = [{"title": "To-Do", "tasks": [_t(2, (1, False)), _t(3, (4, False)), _t(5, (1, True))]},
             {"title": "Blocked", "tasks": [_t(1)]},
             {"title": "Doing", "tasks": [_t(4)]}]
    assert stuck_dependents(board, "to-do", "blocked", "claude") == [(2, 1)]


def test_stuck_dependents_ignores_unlabelled_and_done_tasks():
    board = [{"title": "To-Do", "tasks": [_t(2, (1, False), labels=()), _t(6, (1, False), done=True)]},
             {"title": "Blocked", "tasks": [_t(1)]}]
    assert stuck_dependents(board, "To-Do", "Blocked", "claude") == []
