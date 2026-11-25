"""Releases (explorer's release-batches change): release branches, repairs, the guarded merge and the
release PR. Git runs for real against a local bare origin; Vikunja, gh and Slack are fakes."""

from __future__ import annotations

import dataclasses
import os
import subprocess
from pathlib import Path

import pytest

from dispatcher import daemon as dmod
from dispatcher import pr as prmod
from dispatcher import releases as rl
from dispatcher.db import Ledger

from .test_gaps import _cfg, _dispatcher, _pr_open_row, _run_cleanup

ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}


def git(cwd, *args) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True,
                          env={**os.environ, **ENV}).stdout.strip()


@pytest.fixture
def origin(tmp_path):
    """A bare origin with main, and the target repo cloned from it."""
    bare = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(bare)], check=True)
    seed = tmp_path / "seed"
    seed.mkdir()
    git(seed, "init", "-q", "-b", "main")
    (seed / "app.py").write_text("one\ntwo\nthree\n")
    git(seed, "add", ".")
    git(seed, "commit", "-qm", "base")
    git(seed, "remote", "add", "origin", str(bare))
    git(seed, "push", "-q", "origin", "main")
    repo = tmp_path / "repo"
    subprocess.run(["git", "clone", "-q", str(bare), str(repo)], check=True)
    return bare, seed, repo


def _push_main(seed, name, text):
    (seed / name).write_text(text)
    git(seed, "add", ".")
    git(seed, "commit", "-qm", f"main: {name}")
    git(seed, "push", "-q", "origin", "main")


DESC = "<p>Fix it</p><p>Release: R-4 · branch release/r-4</p><p>Source: x · explorer 0a1b2c3d4e5f</p>"


# --- the wire contract ------------------------------------------------------------------------
def test_release_line_parsing():
    assert rl.target_of(DESC) == rl.Target("R-4", "release/r-4")
    assert rl.target_of("Release: R-12 · branch release/r-12") == rl.Target("R-12", "release/r-12")
    assert rl.target_of("Release: R-4 · branch release/r-5") is None          # branch must match
    assert rl.target_of("<p>no release</p>") is None and rl.target_of(None) is None
    assert rl.repair_release({"title": "Repair release R-4"}) == "R-4"
    assert rl.is_tracker({"labels": [{"title": "release-tracker"}]})


@pytest.mark.parametrize("tickets,to_main,head,base,ok", [
    # ticket PRs into a release branch: RELEASE_AUTO_MERGE alone decides
    (True, False, "task-7-fix", "release/r-4", True), (True, True, "task-7-fix", "release/r-4", True),
    (False, True, "task-7-fix", "release/r-4", False), (False, False, "task-7-fix", "release/r-4", False),
    (True, False, None, "release/r-4", True),
    # the release PR into main: RELEASE_MERGE_TO_MAIN alone decides
    (False, True, "release/r-4", "main", True), (True, True, "release/r-4", "main", True),
    (True, False, "release/r-4", "main", False), (False, False, "release/r-4", "main", False),
    # a ticket PR into main (a DM ticket, a hotfix) never, whatever the settings
    (True, True, "task-7-fix", "main", False), (True, True, None, "main", False),
    (True, True, "release/r-4-evil", "main", False), (True, True, "feature/release/r-4", "main", False),
    # anything else never
    (True, True, "release/r-4", "release/r-5", False), (True, True, "release/r-4", "master", False),
    (True, True, "task-7", "release/r-4-evil", False), (True, True, "task-7", "feature/release/r-4", False),
    (True, True, "release/r-4", "", False), (True, True, "release/r-4", None, False)])
def test_auto_merge_decides_every_case_in_one_place(tickets, to_main, head, base, ok):
    assert rl.auto_merge_allowed(tickets=tickets, to_main=to_main, head=head, base=base, base_branch="main") is ok


def test_an_empty_base_branch_never_allows_a_merge_to_main():
    assert not rl.auto_merge_allowed(tickets=True, to_main=True, head="release/r-4", base="", base_branch="")


def test_merge_into_release_never_calls_gh_for_main(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(prmod.subprocess, "run", lambda *a, **k: calls.append(a[0]) or subprocess.CompletedProcess(a[0], 0, "", ""))
    cfg = _cfg(tmp_path, RELEASE_AUTO_MERGE="true")
    ok, msg = prmod.merge_into_release(cfg, "https://gh/pr/1", "main", "abc", merge_commit=False)
    assert not ok and "only for release branches" in msg and calls == []
    ok, _ = prmod.merge_into_release(cfg, "https://gh/pr/1", "release/r-4", "abc", merge_commit=False)
    assert ok and calls[-1][:5] == ["gh", "pr", "merge", "https://gh/pr/1", "--merge"]   # the default
    sq = _cfg(tmp_path, RELEASE_AUTO_MERGE="true", RELEASE_TICKET_MERGE_METHOD="squash")
    prmod.merge_into_release(sq, "https://gh/pr/1", "release/r-4", "abc", merge_commit=False)
    assert calls[-1][4] == "--squash"
    bad = _cfg(tmp_path, RELEASE_AUTO_MERGE="true", RELEASE_TICKET_MERGE_METHOD="fast-forward")
    assert bad.release_ticket_merge_method == "merge"
    prmod.merge_into_release(sq, "https://gh/pr/2", "release/r-4", "abc", merge_commit=True)
    assert calls[-1][4] == "--merge"                                   # a repair: always a merge commit
    prmod.merge_into_release(cfg, "https://gh/pr/2", "release/r-4", "abc", merge_commit=True)
    assert "--merge" in calls[-1] and "--match-head-commit" in calls[-1]
    off = _cfg(tmp_path, RELEASE_AUTO_MERGE="false")
    assert not prmod.merge_into_release(off, "https://gh/pr/3", "release/r-4", "abc", merge_commit=False)[0]
    both = _cfg(tmp_path, RELEASE_AUTO_MERGE="true", RELEASE_MERGE_TO_MAIN="true")
    n = len(calls)
    assert not prmod.merge_into_release(both, "https://gh/pr/4", "main", "abc", merge_commit=True)[0]
    assert len(calls) == n                                   # a ticket PR into main: no gh call at all


def _gh(monkeypatch, *results):
    """Fake `subprocess.run` for gh: each call takes the next return code from `results`."""
    calls, codes = [], list(results)

    def run(args, **kw):
        calls.append(args)
        rc = codes.pop(0)
        return subprocess.CompletedProcess(args, rc, "", "" if rc == 0 else f"refused #{len(calls)}")
    monkeypatch.setattr(prmod.subprocess, "run", run)
    return calls


def test_the_release_pr_is_merged_with_a_merge_commit_at_the_tested_head(tmp_path, monkeypatch):
    calls = _gh(monkeypatch, 0)
    cfg = _cfg(tmp_path, RELEASE_MERGE_TO_MAIN="true")
    assert prmod.merge_release_into_main(cfg, "https://gh/pr/9", "release/r-4", "abc123") == ("merged", "")
    assert calls == [["gh", "pr", "merge", "https://gh/pr/9", "--merge", "--match-head-commit", "abc123"]]


def test_pending_checks_hand_the_release_pr_to_githubs_auto_merge(tmp_path, monkeypatch):
    calls = _gh(monkeypatch, 1, 0)
    cfg = _cfg(tmp_path, RELEASE_MERGE_TO_MAIN="true")
    assert prmod.merge_release_into_main(cfg, "https://gh/pr/9", "release/r-4", "abc123") == ("queued", "")
    assert calls[1] == ["gh", "pr", "merge", "https://gh/pr/9", "--merge", "--auto", "--match-head-commit", "abc123"]


def test_a_refused_release_merge_is_tried_once_and_never_squashed(tmp_path, monkeypatch):
    calls = _gh(monkeypatch, 1, 1)
    cfg = _cfg(tmp_path, RELEASE_MERGE_TO_MAIN="true")
    outcome, why = prmod.merge_release_into_main(cfg, "https://gh/pr/9", "release/r-4", "abc123")
    assert outcome == "refused" and why == "refused #2" and len(calls) == 2
    assert all("--squash" not in c and "--rebase" not in c and "--merge" in c for c in calls)


@pytest.mark.parametrize("env,branch,sha", [
    ({"RELEASE_MERGE_TO_MAIN": "false", "RELEASE_AUTO_MERGE": "true"}, "release/r-4", "abc"),
    ({"RELEASE_MERGE_TO_MAIN": "true"}, "task-7-fix", "abc"),
    ({"RELEASE_MERGE_TO_MAIN": "true"}, "release/r-4", "")])
def test_the_release_merge_refuses_without_calling_gh(tmp_path, monkeypatch, env, branch, sha):
    calls = _gh(monkeypatch)
    outcome, _ = prmod.merge_release_into_main(_cfg(tmp_path, **env), "https://gh/pr/9", branch, sha)
    assert outcome == "refused" and calls == []


# --- release branches in git --------------------------------------------------------------------
def test_the_branch_is_created_from_main_and_main_is_merged_in(origin, tmp_path):
    bare, seed, repo = origin
    g = rl.ReleaseGit(repo, tmp_path / "scratch")
    assert g.sync("release/r-4").ok
    assert git(bare, "rev-parse", "release/r-4") == git(bare, "rev-parse", "main")
    _push_main(seed, "hotfix.py", "fix\n")
    s = g.sync("release/r-4")
    assert s.ok and s.merged
    head = git(bare, "rev-parse", "release/r-4")
    assert len(git(bare, "rev-list", "--parents", "-n", "1", head).split()) == 3     # a true merge
    assert subprocess.run(["git", "merge-base", "--is-ancestor", "main", "release/r-4"], cwd=bare).returncode == 0
    assert g.sync("release/r-4") == rl.Sync(True, [])                                   # nothing to do


def test_a_conflict_is_reported_and_the_branch_is_left_alone(origin, tmp_path):
    bare, seed, repo = origin
    g = rl.ReleaseGit(repo, tmp_path / "scratch")
    g.sync("release/r-4")
    work = tmp_path / "work"
    subprocess.run(["git", "clone", "-q", "-b", "release/r-4", str(bare), str(work)], check=True)
    (work / "app.py").write_text("one\nRELEASE\nthree\n")
    git(work, "commit", "-qam", "release change")
    git(work, "push", "-q", "origin", "release/r-4")
    before = git(bare, "rev-parse", "release/r-4")
    _push_main(seed, "app.py", "one\nMAIN\nthree\n")
    s = g.sync("release/r-4")
    assert not s.ok and s.conflict == ["app.py"]
    assert git(bare, "rev-parse", "release/r-4") == before
    assert not list((tmp_path / "scratch").glob("*"))                                  # scratch cleaned


def test_a_ticket_branch_takes_the_moved_release_head(origin, tmp_path):
    _bare, seed, repo = origin
    g = rl.ReleaseGit(repo, tmp_path / "scratch")
    g.sync("release/r-4")
    wt = tmp_path / "ticket"                        # ticket workspaces are worktrees of the repo
    git(repo, "worktree", "add", "-q", "-b", "task-1-x", str(wt), "origin/release/r-4")
    (wt / "t.py").write_text("ticket\n")
    git(wt, "add", ".")
    git(wt, "commit", "-qm", "ticket")
    assert g.update_ticket(wt, "release/r-4") == rl.Sync(True, [])
    _push_main(seed, "other.py", "x\n")
    g.sync("release/r-4")
    up = g.update_ticket(wt, "release/r-4")
    assert up.ok and up.merged and (wt / "other.py").exists()


# --- claiming ------------------------------------------------------------------------------------
class _Board:
    def __init__(self, ready, others=()):
        self.ready, self.others = ready, list(others)

    def board(self, *_):
        return [{"title": "Ready", "tasks": self.ready}, {"title": "In Progress", "tasks": self.others}]

    def tasks_in_bucket(self, b):
        return b["tasks"]

    def task_labels(self, t):
        return {x["title"].lower() for x in t.get("labels") or []}

    def has_comment_prefix(self, *_):
        return False

    def task(self, tid):
        return next(t for t in self.ready + self.others if t["id"] == tid)


def _t(tid, title="t", desc="", labels=("claude",), done=False):
    return {"id": tid, "title": title, "description": desc, "done": done, "labels": [{"title": x} for x in labels],
            "related_tasks": {}}


def test_batch_of_reads_the_tracker_line():
    assert rl.batch_of("Batch: B-6\n\nRelease R-6: \u2026") == "B-6"
    assert rl.batch_of("<p>Batch: B-12</p>") == "B-12"
    assert rl.batch_of("no line") == "" and rl.batch_of(None) == ""


def test_the_release_pr_body_names_the_batch():
    tracker, subs = {"id": 861}, [{"id": 862, "title": "Fix the invoice total"}]
    link = lambda tid: f"https://vk.example/tasks/{tid}"
    lines = dmod._release_body("R-6", tracker, subs, link, batch="B-6").splitlines()
    i = next(n for n, ln in enumerate(lines) if ln.startswith("Tracker: #"))
    assert lines[i + 1] == "Batch: B-6"
    plain = dmod._release_body("R-6", tracker, subs, link, batch="")
    assert "Batch:" not in plain and plain == dmod._release_body("R-6", tracker, subs, link)
    assert "Tracker: #861 (https://vk.example/tasks/861)\n\nTickets:" in plain


def test_release_pass_puts_the_trackers_batch_in_the_pr_body(tmp_path, monkeypatch):
    d, ev = _release_dispatcher(tmp_path, monkeypatch, [{"id": 1, "title": "Fix A", "done": True}],
                                [_t(1, "Fix A", DESC)])
    d.release_pass()
    assert "Batch: B-1" in ev["created"][0][2].splitlines()


def test_a_tracker_is_never_claimed_and_a_release_under_repair_waits(tmp_path):
    d = _dispatcher(tmp_path)
    tracker = _t(1, "Release R-4", "Batch: B-1", labels=("release-tracker", "claude"))
    a = _t(2, "Fix A", DESC)
    repair = _t(3, "Repair release R-4", DESC)
    other = _t(4, "Fix B", DESC.replace("R-4", "R-5").replace("r-4", "r-5"))
    main = _t(5, "Hotfix")
    d.vk = _Board([tracker, a, repair, other, main])
    assert [t["id"] for t in d.ready_tasks(limit=9)] == [3, 4, 5]
    repair["done"] = True
    assert [t["id"] for t in d.ready_tasks(limit=9)] == [2, 4, 5]


def test_main_work_never_waits_on_release_files_but_release_work_waits_on_main(tmp_path):
    d = _dispatcher(tmp_path)
    d.ledger.upsert_claim(10, "s10", "s10")
    d.ledger.update(10, state="working", touches='["app/views.py"]', target="release/r-4")
    d.ledger.upsert_claim(11, "s11", "s11")
    d.ledger.update(11, state="working", touches='["app/models.py"]', target=None)
    hot = _t(5, "Hotfix", "<p>Touches: app/views.py</p>")
    rel = _t(6, "Fix", DESC + "<p>Touches: app/models.py</p>")
    d.vk = _Board([hot, rel])
    assert [t["id"] for t in d.ready_tasks(limit=9)] == [5]


# --- the pipeline for a release ticket ------------------------------------------------------------
class _VKW:
    def __init__(self, board=None):
        self.created, self.labels, self.relations, self.moves = [], [], [], []
        self._board = board or [{"title": "Ready", "tasks": [_t(900, "Release R-4", "Batch: B-1", labels=("release-tracker",))]}]

    def board(self, *_):
        return self._board

    def create_task(self, pid, title, desc):
        self.created.append((title, desc))
        return {"id": 77}

    def apply_label(self, tid, lid):
        self.labels.append((tid, lid))

    def ensure_label(self, title):
        return {"claude": 1}.get(title, 9)

    def relate(self, a, b, kind):
        self.relations.append((a, b, kind))


@pytest.fixture
def release_pipeline(tmp_path, monkeypatch):
    from .test_gaps import _change, _git
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "a.txt").write_text("a\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "base")
    _git(repo, "branch", "release/r-4")
    _git(repo, "update-ref", "refs/remotes/origin/release/r-4", "release/r-4")   # as after a fetch
    _git(repo, "checkout", "-qb", "task-1-x", "release/r-4")
    _change(repo)
    d = _dispatcher(tmp_path, TARGET_REPO_PATH=str(repo), TASK_CAP_USD="10", RELEASE_AUTO_MERGE="true")
    d.logs_root = tmp_path / "logs"
    ev = {"moved": [], "comments": [], "prs": [], "merges": [], "alerts": []}
    d.move = lambda tid, key: ev["moved"].append((tid, key))
    d.comment = lambda tid, text: ev["comments"].append(text)
    d.slack.task_blocked = lambda *a, **k: None
    d.slack.pr_opened = lambda *a: None
    d.slack.pr_needs_attention = lambda *a: ev["alerts"].append(a)
    d.slack.alert = lambda text: ev["alerts"].append(text)
    d.vk = _VKW()
    monkeypatch.setattr(d.ntree, "base_ref", lambda: "main")
    monkeypatch.setattr(d.ntree, "rm", lambda s: None)
    monkeypatch.setattr(d.ntree, "remove_branch", lambda s: None)
    monkeypatch.setattr(d.ntree, "new", lambda s, b=None: ev.setdefault("forked_from", b) and repo or repo)
    monkeypatch.setattr(dmod, "run_tests", lambda *a: (True, ""))
    monkeypatch.setattr(dmod.prmod, "push_and_create",
                        lambda **kw: ev["prs"].append((kw["branch"], kw.get("base"))) or "https://gh/pr/1")
    monkeypatch.setattr(dmod.prmod, "merge_into_release",
                        lambda cfg, url, base, sha, merge_commit: ev["merges"].append((base, merge_commit)) or (True, ""))
    monkeypatch.setattr(d.rgit, "sync", lambda b: rl.Sync(True, []))
    monkeypatch.setattr(d.rgit, "ensure", lambda b: True)
    monkeypatch.setattr(d.rgit, "update_ticket", lambda wt, b: rl.Sync(True, []))
    monkeypatch.setenv("FAKE_TEXT", '{"verdict":"pass","findings":[]}')
    d.ledger.upsert_claim(1, "task-1-x", "task-1-x")
    return d, ev


def test_a_release_ticket_forks_from_and_merges_into_its_release(release_pipeline, fake_bin):
    d, ev = release_pipeline
    d.run_pipeline({"id": 1, "title": "x", "description": DESC}, "task-1-x")
    assert ev["forked_from"] == "origin/release/r-4"
    assert ev["prs"] == [("task-1-x", "release/r-4")] and ev["merges"] == [("release/r-4", False)]
    assert d.ledger.get(1)["state"] == "pr_open"
    assert any(c.startswith("merged into release/r-4 by the pipeline") for c in ev["comments"])


def test_auto_merge_off_leaves_the_pr_for_a_human(release_pipeline, fake_bin):
    d, ev = release_pipeline
    d.cfg = dataclasses.replace(d.cfg, release_auto_merge=False)
    d.run_pipeline({"id": 1, "title": "x", "description": DESC}, "task-1-x")
    assert ev["prs"] == [("task-1-x", "release/r-4")] and ev["merges"] == []


def test_a_main_ticket_is_never_merged_by_the_pipeline(release_pipeline, fake_bin):
    d, ev = release_pipeline
    d.run_pipeline({"id": 1, "title": "x", "description": "<p>a hotfix</p>"}, "task-1-x")
    assert ev["prs"] == [("task-1-x", None)] and ev["merges"] == []


def test_a_repair_is_merged_with_a_merge_commit(release_pipeline, fake_bin):
    d, ev = release_pipeline
    d.run_pipeline({"id": 1, "title": "Repair release R-4", "description": DESC}, "task-1-x")
    assert ev["merges"] == [("release/r-4", True)]


def test_a_moved_release_that_breaks_the_tests_is_not_merged(release_pipeline, fake_bin, monkeypatch):
    d, ev = release_pipeline
    monkeypatch.setattr(d.rgit, "update_ticket", lambda wt, b: rl.Sync(True, [], merged=True))
    results = iter([(True, ""), (True, ""), (False, "1 failed")])   # base check, gate, moved head
    monkeypatch.setattr(dmod, "run_tests", lambda *a: next(results))
    d.run_pipeline({"id": 1, "title": "x", "description": DESC}, "task-1-x")
    assert ev["merges"] == [] and any("tests fail once release/r-4" in c for c in ev["comments"])


def test_a_conflict_with_main_files_a_repair_and_puts_the_ticket_back(release_pipeline, fake_bin, monkeypatch):
    d, ev = release_pipeline
    monkeypatch.setattr(d.rgit, "sync", lambda b: rl.Sync(False, ["app/views.py"]))
    d.run_pipeline({"id": 1, "title": "x", "description": DESC}, "task-1-x")
    title, desc = d.vk.created[0]
    assert title == "Repair release R-4" and rl.target_of(desc) == rl.Target("R-4", "release/r-4")
    assert "app/views.py" in desc and "merge commit" in desc
    assert d.vk.relations == [(900, 77, "subtask")] and (77, 1) in d.vk.labels and (77, 9) in d.vk.labels
    assert (77, "ready") in ev["moved"] and (1, "ready") in ev["moved"]
    assert d.ledger.get(1)["state"] == "blocked" and ev["prs"] == []
    assert any(c.startswith("waiting: release R-4") for c in ev["comments"])


def test_a_red_release_head_files_a_repair_instead_of_blaming_the_ticket(release_pipeline, fake_bin, monkeypatch):
    d, ev = release_pipeline
    monkeypatch.setattr(dmod, "run_tests", lambda *a: (False, "FAILED test_x"))
    d.run_pipeline({"id": 1, "title": "x", "description": DESC}, "task-1-x")
    assert d.vk.created and "FAILED test_x" in d.vk.created[0][1] and (1, "ready") in ev["moved"]


# --- done and release PRs ------------------------------------------------------------------------
def test_a_release_ticket_is_done_with_its_branch_named(tmp_path, monkeypatch):
    led = Ledger(tmp_path / "l.db")
    _pr_open_row(led)
    pr = {"number": 2, "state": "MERGED", "headRefName": "task-7-x", "baseRefName": "release/r-4",
          "url": "https://gh/pr/2", "mergeCommit": {"oid": "abcdef1234567"}}
    vk, _nt = _run_cleanup(monkeypatch, tmp_path, led, [pr])
    assert any(c[2] == "<p>merged: abcdef123 into release/r-4 (https://gh/pr/2)</p>" for c in vk.calls if c[0] == "comment")


class _VKRel(_VKW):
    def __init__(self, tracker, subs, board_tasks):
        super().__init__([{"title": "Ready", "tasks": [tracker, *board_tasks]}])
        self.tracker, self.subs = tracker, subs

    def task(self, tid):
        return {**self.tracker, "related_tasks": {"subtask": self.subs}}


def _release_dispatcher(tmp_path, monkeypatch, subs, board_tasks=(), prs=()):
    d = _dispatcher(tmp_path)
    tracker = _t(900, "Release R-4", "Batch: B-1", labels=("release-tracker",))
    d.vk = _VKRel(tracker, subs, list(board_tasks))
    ev = {"moved": [], "comments": [], "alerts": [], "created": [], "deleted": []}
    d.move = lambda tid, key: ev["moved"].append((tid, key))
    d.comment = lambda tid, text: ev["comments"].append((tid, text))
    d.slack.alert = lambda text: ev["alerts"].append(text)
    monkeypatch.setattr(dmod.prmod, "release_prs", lambda cfg: list(prs))
    monkeypatch.setattr(dmod.prmod, "create_release_pr",
                        lambda cfg, branch, title, body: ev["created"].append((branch, title, body)) or "https://gh/pr/9")
    monkeypatch.setattr(d.rgit, "sync", lambda b: rl.Sync(True, []))
    monkeypatch.setattr(d.rgit, "delete", lambda b: ev["deleted"].append(b) or True)
    monkeypatch.setattr(d, "_release_head_green", lambda rel, branch: (True, ""))
    return d, ev


def test_a_finished_release_gets_its_pr_to_main(tmp_path, monkeypatch):
    subs = [{"id": 1, "title": "Fix A", "done": True}, {"id": 2, "title": "Fix B", "done": False}]
    tickets = [_t(1, "Fix A", DESC), _t(2, "Fix B", DESC)]
    d, ev = _release_dispatcher(tmp_path, monkeypatch, subs, tickets)
    d.release_pass()
    assert ev["created"] == []                                   # one ticket still open
    subs[1]["done"] = True
    d.release_pass()
    branch, title, body = ev["created"][0]
    assert (branch, title) == ("release/r-4", "Release R-4") and "merge commit, not a squash" in body
    assert "#1 Fix A" in body and "Tracker: #900" in body and (900, "pr: https://gh/pr/9") in ev["comments"]
    assert "MERGE COMMIT" in ev["alerts"][-1]


@pytest.mark.parametrize("outcome,note,said", [
    ("merged", None, "the pipeline merged it into main with a merge commit"),
    ("queued", "auto-merge into main is on", "once the required checks pass"),
    ("refused", "not merged into main: protected; the owner merges it", "needs you")])
def test_with_merge_to_main_on_the_pipeline_merges_the_release_pr(tmp_path, monkeypatch, outcome, note, said):
    d, ev = _release_dispatcher(tmp_path, monkeypatch, [{"id": 1, "title": "Fix A", "done": True}],
                                [_t(1, "Fix A", DESC)])
    d.cfg = dataclasses.replace(d.cfg, release_merge_to_main=True)
    merges = []
    monkeypatch.setattr(d.rgit, "sha", lambda ref: "feedface" if ref == "origin/release/r-4" else "")
    monkeypatch.setattr(dmod.prmod, "merge_release_into_main",
                        lambda cfg, url, branch, sha: merges.append((url, branch, sha)) or
                        (outcome, "protected" if outcome == "refused" else ""))
    d.release_pass()
    assert merges == [("https://gh/pr/9", "release/r-4", "feedface")]
    tracker_notes = [c for tid, c in ev["comments"] if tid == 900 and not c.startswith("pr: ")]
    assert (tracker_notes == []) if note is None else (note in tracker_notes[0])
    assert said in ev["alerts"][-1] and "https://gh/pr/9" in ev["alerts"][-1]


def test_an_open_release_pr_is_not_merged_again(tmp_path, monkeypatch):
    prs = [{"state": "OPEN", "headRefName": "release/r-4", "baseRefName": "main", "url": "https://gh/pr/9"}]
    d, ev = _release_dispatcher(tmp_path, monkeypatch, [{"id": 1, "done": True}], [_t(1, "Fix A", DESC)], prs)
    d.cfg = dataclasses.replace(d.cfg, release_merge_to_main=True)
    monkeypatch.setattr(dmod.prmod, "merge_release_into_main", lambda *a: pytest.fail("merged twice"))
    d.release_pass()
    assert ev["created"] == [] and ev["alerts"] == []


def test_with_merge_to_main_off_the_release_pr_waits_for_a_human(tmp_path, monkeypatch):
    d, ev = _release_dispatcher(tmp_path, monkeypatch, [{"id": 1, "done": True}], [_t(1, "Fix A", DESC)])
    monkeypatch.setattr(dmod.prmod, "merge_release_into_main", lambda *a: pytest.fail("merged with the setting off"))
    d.release_pass()
    assert ev["created"] and "MERGE COMMIT" in ev["alerts"][-1] and "needs you" not in ev["alerts"][-1]


def test_a_red_head_at_the_end_files_a_repair(tmp_path, monkeypatch):
    subs = [{"id": 1, "title": "Fix A", "done": True}]
    d, ev = _release_dispatcher(tmp_path, monkeypatch, subs, [_t(1, "Fix A", DESC)])
    monkeypatch.setattr(d, "_release_head_green", lambda rel, branch: (False, "boom"))
    d.release_pass()
    assert ev["created"] == [] and d.vk.created[0][0] == "Repair release R-4" and "boom" in d.vk.created[0][1]


def test_a_merged_release_closes_its_tracker_and_branch(tmp_path, monkeypatch):
    prs = [{"state": "MERGED", "headRefName": "release/r-4", "baseRefName": "main", "url": "https://gh/pr/9",
            "mergeCommit": {"oid": "1234567890abc"}}]
    d, ev = _release_dispatcher(tmp_path, monkeypatch, [{"id": 1, "done": True}], [_t(1, "Fix A", DESC)], prs)
    d.release_pass()
    assert (900, "done") in ev["moved"] and ev["deleted"] == ["release/r-4"]
    assert (900, "merged: 123456789 into main (https://gh/pr/9)") in ev["comments"]


def test_a_half_removed_ticket_is_reported_once(tmp_path, monkeypatch):
    subs = [{"id": 1, "done": False}, {"id": 3, "done": False}]
    board = [_t(1, "Fix A", DESC), _t(2, "Fix B", DESC), _t(3, "Fix C", "<p>no release line</p>")]
    d, ev = _release_dispatcher(tmp_path, monkeypatch, subs, board)
    d.release_pass()
    d.release_pass()
    half = [a for a in ev["alerts"] if "half removed" in a]
    assert len(half) == 1 and "subtask relation: [2]" in half[0] and "Release line: [3]" in half[0]


def test_the_release_head_is_tested_once_per_commit(tmp_path, monkeypatch, origin):
    _bare, _seed, repo = origin
    d = _dispatcher(tmp_path, TARGET_REPO_PATH=str(repo), RELEASE_CHECK_CMD="python manage.py check")
    d.rgit = rl.ReleaseGit(repo, tmp_path / "scratch")
    d.rgit.sync("release/r-4")
    gates, runs = [], []
    monkeypatch.setattr(d.ntree, "new", lambda s, b=None: Path(repo))
    monkeypatch.setattr(d.ntree, "rm", lambda s: None)
    monkeypatch.setattr(d.ntree, "remove_branch", lambda s: None)
    monkeypatch.setattr(d.ntree, "run", lambda s, cmd, timeout=None: runs.append(cmd) or subprocess.CompletedProcess(cmd, 0, "ok", ""))
    monkeypatch.setattr(dmod, "run_tests", lambda *a: gates.append(1) or (True, ""))
    assert d._release_head_green("R-4", "release/r-4") == (True, "ok")
    assert d._release_head_green("R-4", "release/r-4")[0] and gates == [1] and runs == [["python", "manage.py", "check"]]
