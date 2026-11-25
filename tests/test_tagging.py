"""Release tags (integration D17): tag the newest commit explorer marked `explorer/batch-settled`.
git and gh are a scripted fake runner; nothing reaches a network or a real repo."""

from __future__ import annotations

import datetime as dt
import json
import subprocess

from dispatcher import tagging as tg
from dispatcher.db import Ledger

from .test_gaps import _cfg

DAY = dt.date(2026, 11, 24)
A, B, C = "a" * 40, "b" * 40, "c" * 40          # first-parent commits, newest first


def _status(state="success", desc="B-1 settled: 3 findings", context=tg.CONTEXT, lane=""):
    if lane:
        context = f"{tg.CONTEXT}/{lane}"
    return {"context": context, "state": state, "description": desc}


class FakeRepo:
    """Answers the git and gh calls tagging makes; records every call."""

    def __init__(self, *, tags=None, commits=(A, B, C), statuses=None, fail=(), log=None, prs=()):
        self.tags = dict(tags or {})                 # name -> sha (local, after fetch)
        self.pushed = set(self.tags)
        self.releases: dict[str, str] = {}
        self.commits, self.statuses = list(commits), statuses or {}
        self.fail, self.log = set(fail), log or {}
        self.prs = list(prs)                         # merged release PRs `gh pr list` returns
        self.calls: list[list[str]] = []

    def __call__(self, args, **kw):
        self.calls.append(list(args))
        tool, *rest = args
        key = " ".join(rest[:2])
        if any(f in " ".join(args) for f in self.fail):
            return subprocess.CompletedProcess(args, 1, "", "boom")
        ok = lambda out="": subprocess.CompletedProcess(args, 0, out, "")
        no = subprocess.CompletedProcess(args, 1, "", "")
        if tool == "git":
            if rest[0] == "fetch":
                return ok()
            if rest[0] == "describe":           # like git: a local, unpushed tag counts too
                prefixed = [t for t in self.tags if t.startswith("v")]
                return ok(prefixed[-1]) if prefixed else no
            if rest[0] == "rev-list":
                stop = next((a[1:] for a in rest if a.startswith("^")), None)
                out = self.commits
                if stop:
                    out = out[:out.index(self.tags[stop])] if self.tags[stop] in out else out
                return ok("\n".join(out))
            if rest[0] == "rev-parse":
                ref = rest[-1].removeprefix("refs/tags/").removesuffix("^{commit}")
                return ok(self.tags[ref]) if ref in self.tags else no
            if rest[0] == "show":
                return ok(self.log.get(rest[-1], f"commit {rest[-1][:4]}\x1f"))
            if rest[0] == "tag":
                name, sha = rest[2], rest[3]
                if name in self.tags:
                    return no
                self.tags[name] = sha
                return ok()
            if rest[0] == "push":
                self.pushed.add(rest[-1].removeprefix("refs/tags/"))
                return ok()
        if tool == "gh":
            if rest[:2] == ["pr", "list"]:
                return ok(json.dumps(list(self.prs)))
            if rest[0] == "api":
                sha = rest[1].split("/commits/")[1].split("/")[0]
                return ok(json.dumps(self.statuses.get(sha, [])))
            if key.startswith("release view"):
                url = self.releases.get(rest[2])
                return ok(url) if url else no
            if key.startswith("release create"):
                self.releases[rest[2]] = f"https://github.com/o/r/releases/tag/{rest[2]}"
                return ok(self.releases[rest[2]] + "\n")
        raise AssertionError(f"unexpected call {args}")


class FakeSlack:
    def __init__(self):
        self.posts: list[str] = []
        self.alerts: list[str] = []

    def post(self, text):
        self.posts.append(text)

    def alert(self, text):
        self.alerts.append(text)


def _tagger(tmp_path, repo, *, ledger=None, slack=None, **env):
    cfg = _cfg(tmp_path, RELEASE_TAGS="true", **env)
    return tg.Tagger(cfg, slack=slack, ledger=ledger, run=repo, today=lambda: DAY)


def _writes(repo):
    return [c for c in repo.calls if c[:2] in (["git", "tag"], ["git", "push"], ["gh", "release"])
            and c[:3] != ["gh", "release", "view"]]


def test_no_settled_status_tags_nothing(tmp_path):
    repo = FakeRepo(statuses={A: [_status(context="ci/test")], B: [_status(state="failure")]})
    out = _tagger(tmp_path, repo, slack=(s := FakeSlack())).run_pass()
    assert out.plan is None and not out.error and not _writes(repo) and not s.posts


def test_the_newest_settled_commit_is_tagged_and_every_batch_is_in_the_notes(tmp_path):
    log = {B: "Merge pull request #640 from o/release/r-2\x1fRelease R-2\n",
           C: "Merge pull request #635 from o/release/r-1\x1fRelease R-1\n"}
    repo = FakeRepo(tags={"v2026.11.20": "d" * 40}, commits=(A, B, C),
                    statuses={A: [], B: [_status(desc="B-2 settled")], C: [_status(desc="B-1 settled")]}, log=log)
    slack = FakeSlack()
    out = _tagger(tmp_path, repo, slack=slack).run_pass()
    assert out.tagged and out.plan.sha == B and out.plan.name == "v2026.11.24"
    assert out.plan.batches == ["B-1", "B-2"] and out.plan.previous == "v2026.11.20"
    assert repo.tags["v2026.11.24"] == B and "v2026.11.24" in repo.pushed
    assert "- #640 Release R-2" in out.plan.notes and "- #635 Release R-1" in out.plan.notes
    assert out.plan.notes.startswith("B-1, B-2 settled on UAT at `bbbbbbbbb`")
    assert slack.posts == [(":label: Tagged v2026.11.24 on bbbbbbbbb: B-1, B-2 settled · "
                            "https://github.com/o/r/releases/tag/v2026.11.24")]


def test_a_second_tag_the_same_day_is_dot_two(tmp_path):
    repo = FakeRepo(tags={"v2026.11.24": C}, commits=(A, B, C), statuses={A: [_status()]})
    out = _tagger(tmp_path, repo).run_pass()
    assert out.plan.name == "v2026.11.24.2" and repo.tags["v2026.11.24.2"] == A
    assert tg.tag_name("v", DAY, lambda n: n in {"v2026.11.24", "v2026.11.24.2"}) == "v2026.11.24.3"


def test_only_the_latest_status_of_the_context_counts():
    assert tg.settled_batches([_status(state="failure"), _status()]) == []
    assert tg.settled_batches([_status(desc="B-12 settled: 4 findings"), _status(state="failure")]) == ["B-12"]
    assert tg.settled_batches([_status(desc="something else")]) == []


def test_notes_lines_for_pr_merges_and_plain_commits():
    assert tg.merged_line("Merge pull request #633 from o/task-837-x", "T2 due date fix\n\nmore") == "- #633 T2 due date fix"
    assert tg.merged_line("Merge pull request #9 from o/b", "") == "- #9 Merge pull request #9 from o/b"
    assert tg.merged_line("Fix typo", "") == "- Fix typo"


def test_dry_run_changes_nothing(tmp_path):
    repo = FakeRepo(commits=(A, B), statuses={B: [_status()]})
    slack = FakeSlack()
    out = _tagger(tmp_path, repo, slack=slack).run_pass(dry_run=True)
    assert out.plan.sha == B and not out.tagged and not _writes(repo) and not slack.posts
    assert "would tag v2026.11.24 on " + B in tg.describe(out)


def test_without_a_tag_only_the_last_scan_commits_are_read(tmp_path):
    repo = FakeRepo(commits=(A, B), statuses={})
    _tagger(tmp_path, repo, TAG_SCAN_COMMITS="7").run_pass()
    rev = next(c for c in repo.calls if c[:2] == ["git", "rev-list"])
    assert "--max-count=7" in rev and not any(a.startswith("^") for a in rev)


def test_a_failure_is_alerted_once_and_retried(tmp_path):
    ledger = Ledger(tmp_path / "p.db")
    slack = FakeSlack()
    repo = FakeRepo(commits=(A,), statuses={A: [_status()]}, fail=("release create",))
    t = _tagger(tmp_path, repo, ledger=ledger, slack=slack)
    first, second = t.run_pass(), t.run_pass()
    assert first.error and second.error and len(slack.alerts) == 1 and not slack.posts
    assert repo.tags["v2026.11.24"] == A                     # the tag is reused, not renamed
    repo.fail.clear()
    third = t.run_pass()
    assert third.tagged and third.plan.name == "v2026.11.24" and len(slack.posts) == 1


def test_release_tags_off_is_a_no_op_in_the_daemon(tmp_path, monkeypatch):
    from dispatcher import daemon as dmod

    from .test_gaps import _dispatcher
    d = _dispatcher(tmp_path)
    assert not d.cfg.release_tags
    called = []
    monkeypatch.setattr(d.tagger, "run_pass", lambda **k: called.append(1))
    d._maybe_tag(10_000.0)
    assert not called and not d._tag_pass_running.is_set()
    assert dmod.tagging is tg


def test_the_daemon_runs_a_pass_at_most_every_poll_interval(tmp_path):
    import dataclasses

    from .test_gaps import _dispatcher
    d = _dispatcher(tmp_path)
    d.cfg = dataclasses.replace(d.cfg, release_tags=True, tag_poll_min=10)
    submitted = []
    d.pool.submit = lambda fn: submitted.append(fn)
    d._maybe_tag(1000.0)
    d._tag_pass_running.clear()
    d._maybe_tag(1000.0 + 599)
    d._maybe_tag(1000.0 + 600)
    assert len(submitted) == 2


def test_the_cli_refuses_a_real_pass_while_release_tags_is_off(tmp_path, capsys):
    from dispatcher.__main__ import main
    _cfg(tmp_path)
    assert main(["--env", str(tmp_path / ".env"), "tag"]) == 2
    assert "RELEASE_TAGS is off" in capsys.readouterr().err


def test_a_failed_push_is_retried_with_the_same_tag_and_no_release_yet(tmp_path):
    ledger = Ledger(tmp_path / "p.db")
    slack = FakeSlack()
    repo = FakeRepo(commits=(A,), statuses={A: [_status()]}, fail=("git push",))
    t = _tagger(tmp_path, repo, ledger=ledger, slack=slack)
    first = t.run_pass()
    assert first.error.startswith("git push") and not repo.releases
    assert ledger.get_meta(tg.PENDING_KEY)       # the local tag now hides the commit from the scan
    repo.fail.clear()
    second = t.run_pass()
    assert second.tagged and second.plan.name == "v2026.11.24" and "v2026.11.24" in repo.pushed
    assert not ledger.get_meta(tg.PENDING_KEY) and len(slack.alerts) == 1 and len(slack.posts) == 1


# --- lane contexts (change lane-tags) -------------------------------------------------------------
R6 = {"number": 650, "headRefName": "release/r-6", "mergeCommit": {"oid": B}, "body": "Batch: B-6\n"}


def test_settled_on_reads_every_lane_context_latest_first():
    statuses = [_status(desc="B-4 settled; check passed: 5/5 scripts", lane="billing"),
                _status(state="failure", desc="B-6 settled; check failed: 1 script", lane="portal"),
                _status(desc="B-1 settled: 3 findings"),
                _status(context="ci/test", desc="ok")]
    assert tg.settled_on(statuses) == {"B-4": "billing", "B-1": ""}
    assert tg.settled_on([_status(desc="B-2 settled", context="explorer/batch-settled/Bad_Lane")]) == {}
    newer_failure = [_status(state="failure", desc="B-4 settled", lane="billing"),
                     _status(desc="B-4 settled", lane="billing")]
    assert tg.settled_on(newer_failure) == {}


def test_one_batch_under_several_contexts_failure_wins():
    def row(*pairs):
        return tg.settled_on([_status(state=st, desc="B-4 settled", lane=lane) for st, lane in pairs])
    assert row(("success", ""), ("success", "billing")) == {"B-4": "billing"}
    assert row(("failure", ""), ("success", "billing")) == {}
    assert row(("success", "portal"), ("success", "billing")) == {"B-4": "billing"}
    assert row(("pending", "billing"), ("success", "")) == {"B-4": ""}


def test_release_prs_are_read_through_gh(tmp_path):
    repo = FakeRepo(prs=[R6])
    assert _tagger(tmp_path, repo)._release_prs() == ([R6], "")
    call = next(c for c in repo.calls if c[:3] == ["gh", "pr", "list"])
    assert "--state" in call and call[call.index("--state") + 1] == "merged"
    got, why = _tagger(tmp_path, FakeRepo(fail=("pr list",)))._release_prs()
    assert got is None and why.startswith("could not list the release PRs")


def test_an_old_pending_plan_without_lanes_still_loads(tmp_path):
    led = Ledger(str(tmp_path / "l.db"))
    led.set_meta(tg.PENDING_KEY, json.dumps({"name": "v2026.11.24", "sha": A, "batches": ["B-1"],
                                             "previous": "", "notes": "n"}))
    repo = FakeRepo(tags={"v2026.11.24": A})
    out = _tagger(tmp_path, repo, ledger=led, slack=FakeSlack()).run_pass()
    assert out.tagged and out.plan.lanes == {}
