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


# --- the rule across lanes ------------------------------------------------------------------------
def _pr(number, rel, batch, merged_at, head=None, body=None):
    return {"number": number, "headRefName": head or f"release/r-{rel}", "mergeCommit": {"oid": merged_at},
            "body": body if body is not None else f"Release R-{rel}\n\nTracker: #1\nBatch: {batch}\n"}


R5, R6B = _pr(645, 5, "B-5", C), _pr(650, 6, "B-6", B)
BLOCKED_R6 = [("blocked by R-6 (B-6, PR #650, merged bbbbbbbbb): "
               "no successful explorer/batch-settled status for B-6 since its merge")]


def _blocked_by_r6():
    return FakeRepo(prs=[R5, R6B],
                    statuses={A: [_status(desc="B-5 settled", lane="accounts-contacts")]})


def test_an_unreadable_pr_list_is_an_error(tmp_path):
    repo = FakeRepo(fail=("pr list",), statuses={A: [_status()]})
    out = _tagger(tmp_path, repo).run_pass()
    assert out.error.startswith("could not list the release PRs") and out.blockers == []
    assert out.plan is None and not _writes(repo)


def test_one_lane_settled_while_another_release_is_merged_blocks_the_tag(tmp_path):
    repo, slack = _blocked_by_r6(), FakeSlack()
    tagger = _tagger(tmp_path, repo, slack=slack)
    assert tagger.plan() == (None, "", BLOCKED_R6)
    out = tagger.run_pass()
    assert out.plan is None and not out.tagged and not out.error and out.blockers == BLOCKED_R6
    assert not _writes(repo) and not slack.posts and not slack.alerts


def test_both_lanes_settled_tags_and_names_both(tmp_path):
    repo, slack = FakeRepo(prs=[R5, R6B], statuses={A: [
        _status(desc="B-5 settled", lane="accounts-contacts"), _status(desc="B-6 settled", lane="billing")]}), FakeSlack()
    out = _tagger(tmp_path, repo, slack=slack).run_pass()
    assert out.tagged and out.plan.sha == A and out.plan.batches == ["B-5", "B-6"]
    assert out.plan.lanes == {"B-5": "accounts-contacts", "B-6": "billing"}
    assert out.plan.notes.startswith("B-5 (accounts-contacts), B-6 (billing) settled on UAT at `aaaaaaaaa`")
    assert "B-5 (accounts-contacts), B-6 (billing) settled" in slack.posts[0]
    tag_call = next(c for c in repo.calls if c[:2] == ["git", "tag"])
    assert "B-5 (accounts-contacts), B-6 (billing) settled on UAT" in tag_call[-1]


def _older_commit_fixture():
    return FakeRepo(prs=[R5, _pr(650, 6, "B-6", A)], statuses={
        B: [_status(desc="B-5 settled", lane="accounts-contacts")],
        A: [_status(desc="B-5 settled", lane="accounts-contacts")]})


def test_an_older_covered_commit_is_tagged_when_the_newest_is_blocked(tmp_path):
    repo = _older_commit_fixture()
    tagger = _tagger(tmp_path, repo)
    plan, err, blockers = tagger.plan()
    assert plan.sha == B and not err and blockers[0].startswith("blocked by R-6 (B-6, PR #650")
    out = _tagger(tmp_path, _older_commit_fixture()).run_pass()
    assert out.tagged and out.plan.sha == B and out.blockers == []


def test_a_legacy_status_covers_its_own_batch(tmp_path):
    repo = FakeRepo(prs=[_pr(641, 1, "B-1", B)], statuses={A: [_status(desc="B-1 settled")]})
    out = _tagger(tmp_path, repo).run_pass()
    assert out.tagged and out.plan.sha == A and out.plan.lanes == {"B-1": ""}
    assert out.plan.notes.startswith("B-1 settled")


def test_a_status_before_the_merge_does_not_cover(tmp_path):
    repo = FakeRepo(prs=[_pr(644, 4, "B-4", A)], statuses={
        B: [_status(desc="B-4 settled", lane="billing")], A: [_status(desc="B-9 settled", lane="portal")]})
    plan, _err, blockers = _tagger(tmp_path, repo).plan()
    assert plan.sha == B and len(blockers) == 1 and blockers[0].startswith("blocked by R-4 (B-4, PR #644")


def test_a_release_pr_without_a_batch_line_never_blocks(tmp_path):
    repo = FakeRepo(prs=[_pr(642, 2, "", B, body="Tracker: #841\n")],
                    statuses={A: [_status(desc="B-9 settled", lane="billing")]})
    out = _tagger(tmp_path, repo).run_pass()
    assert out.tagged and out.plan.sha == A


def test_a_failure_status_alone_tags_nothing(tmp_path):
    repo = FakeRepo(prs=[R6B], statuses={A: [
        _status(state="failure", desc="B-4 settled; check failed", lane="billing")]})
    out = _tagger(tmp_path, repo).run_pass()
    assert out.plan is None and out.blockers == [] and not out.error
    assert not any(c[:3] == ["gh", "pr", "list"] for c in repo.calls)


def test_two_releases_of_one_batch_each_need_coverage(tmp_path):
    prs = [R5, _pr(651, 7, "B-5", A)]
    out = _tagger(tmp_path, FakeRepo(prs=prs, statuses={B: [_status(desc="B-5 settled")]})).run_pass()
    assert out.tagged and out.plan.sha == B
    out = _tagger(tmp_path, FakeRepo(prs=prs, statuses={A: [_status(desc="B-5 settled")]})).run_pass()
    assert out.tagged and out.plan.sha == A


def test_a_release_pr_with_another_head_still_counts(tmp_path):
    repo = FakeRepo(prs=[_pr(652, 0, "B-8", B, head="hotfix-totals")],
                    statuses={A: [_status(desc="B-9 settled", lane="billing")]})
    plan, err, blockers = _tagger(tmp_path, repo).plan()
    assert plan is None and not err
    assert blockers[0].startswith("blocked by hotfix-totals (B-8, PR #652, merged bbbbbbbbb)")


# --- what a blocked pass reports ------------------------------------------------------------------
def test_dry_run_prints_the_blockers(tmp_path):
    repo = _blocked_by_r6()
    out = _tagger(tmp_path, repo).run_pass(dry_run=True)
    assert tg.describe(out) == ("Nothing to tag: every commit with a settled batch also contains a "
                                "release that is not settled yet.\n" + BLOCKED_R6[0])
    assert not _writes(repo)


def test_blockers_are_not_reported_when_an_older_commit_is_tagged(tmp_path):
    out = _tagger(tmp_path, _older_commit_fixture()).run_pass()
    assert out.tagged and out.blockers == []
    assert not any(ln.startswith("blocked by") for ln in tg.describe(out).splitlines())
