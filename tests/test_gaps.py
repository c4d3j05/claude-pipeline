"""Regression tests for the pipeline gaps found in the audit.

A fake `claude` (and `ntree`) on PATH exercises the real launcher, watchdog and review
code without network access.
"""

from __future__ import annotations

import stat
import subprocess
import time
from pathlib import Path

import pytest

from dispatcher import cleanup, repoprep
from dispatcher import config as cfgmod
from dispatcher import daemon as dmod
from dispatcher.db import Ledger
from dispatcher.ntree import Ntree, NtreeError
from dispatcher.pr import is_pipeline_branch
from dispatcher.review import MAX_DIFF_CHARS, adversarial_review
from dispatcher.stream import SessionResult
from dispatcher.worker import launch

FAKE_CLAUDE = """#!/usr/bin/env python3
import json, os, sys, time
prompt = sys.stdin.read()
open(os.environ["FAKE_PROMPT_OUT"], "w").write(prompt)
if os.environ.get("FAKE_CALLS_OUT"):
    with open(os.environ["FAKE_CALLS_OUT"], "a") as _f:
        _f.write(json.dumps({"argv": sys.argv, "prompt": prompt}) + chr(10))
if os.environ.get("FAKE_ENV_OUT"):
    open(os.environ["FAKE_ENV_OUT"], "w").write(json.dumps(
        {"env": {k: os.environ.get(k) for k in ("NTREE_WORKSPACE", "PORT")}, "argv": sys.argv}))
mode = os.environ.get("FAKE_MODE", "ok")
print(json.dumps({"type": "system", "subtype": "init", "session_id": "s1"}), flush=True)
if mode == "hang":
    time.sleep(60)
if mode == "cumcost":
    # like the real CLI: a resumed session reports the CUMULATIVE cost of the conversation
    prev = float(open(os.environ["FAKE_CUM_FILE"]).read() or 0) if "--resume" in sys.argv else 0.0
    cost = prev + 0.5
    open(os.environ["FAKE_CUM_FILE"], "w").write(str(cost))
    print(json.dumps({"type": "result", "subtype": "success", "total_cost_usd": cost,
                      "num_turns": 2, "is_error": False, "result": "ok"}), flush=True)
    sys.exit(0)
if mode in ("turncap_once", "turncap_always"):
    n = int(open(os.environ["FAKE_CAP_COUNTER"]).read() or 0) + 1
    open(os.environ["FAKE_CAP_COUNTER"], "w").write(str(n))
    if mode == "turncap_always" or n == 1:
        print(json.dumps({"type": "result", "subtype": "error_max_turns", "total_cost_usd": 0.5,
                          "num_turns": 99, "is_error": True, "result": "Reached max turns"}), flush=True)
        sys.exit(1)
if mode == "noconv" and "--resume" in sys.argv:
    print(json.dumps({"type": "result", "total_cost_usd": 0, "num_turns": 0, "is_error": True,
                      "result": "No conversation found with session ID: x"}), flush=True)
    sys.exit(1)
text = os.environ.get("FAKE_TEXT", "done")
print(json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}}), flush=True)
if mode == "flaky":
    n = int(open(os.environ["FAKE_FLAKY_COUNTER"]).read() or 0) + 1
    open(os.environ["FAKE_FLAKY_COUNTER"], "w").write(str(n))
    if n <= int(os.environ["FAKE_FLAKY_TIMES"]):
        text = os.environ["FAKE_FLAKY_TEXT"]
        print(json.dumps({"type": "result", "total_cost_usd": 0.5, "num_turns": 1,
                          "is_error": True, "result": text}), flush=True)
        sys.exit(1)
err = mode == "error"
print(json.dumps({"type": "result", "total_cost_usd": 0.5, "num_turns": 3,
                  "is_error": err, "result": text}), flush=True)
sys.exit(1 if err else 0)
"""


def _script(path: Path, body: str) -> None:
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


@pytest.fixture
def fake_bin(tmp_path, monkeypatch):
    b = tmp_path / "bin"
    b.mkdir()
    _script(b / "claude", FAKE_CLAUDE)
    monkeypatch.setenv("PATH", f"{b}:{__import__('os').environ['PATH']}")
    monkeypatch.setenv("FAKE_PROMPT_OUT", str(tmp_path / "prompt.txt"))
    return b


def _launch(tmp_path, prompt="hello", wall=1, **kw):
    return launch(worktree=tmp_path, prompt=prompt, settings_file="s.json",
                  session_log=tmp_path / "logs" / "s.jsonl", max_turns=5,
                  wall_clock_limit_min=wall, **kw)


def _git(repo, *args):
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True,
                   env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
                        "GIT_COMMITTER_EMAIL": "t@t", "PATH": __import__("os").environ["PATH"],
                        "HOME": str(repo)})


def _cfg(tmp_path, repo=None, **extra):
    env = tmp_path / ".env"
    lines = {"VIKUNJA_URL": "http://x/api/v1", "VIKUNJA_API_TOKEN": "t", "VIKUNJA_PROJECT_ID": "1",
             "TARGET_REPO_PATH": str(repo or tmp_path), "PIPELINE_DB": str(tmp_path / "p.db"),
             "BASE_BRANCH": "main", **extra}
    env.write_text("\n".join(f"{k}={v}" for k, v in lines.items()))
    return cfgmod.load(str(env))


# --- worker launcher -------------------------------------------------------
def test_prompt_is_sent_on_stdin_not_argv(tmp_path, fake_bin):
    big = "x" * 300_000  # would exceed Linux's 128 KiB single-argument limit as argv
    result, rc = _launch(tmp_path, prompt=big)
    assert rc == 0 and result.session_id == "s1" and result.cost_usd == 0.5
    assert (tmp_path / "prompt.txt").read_text() == big


def test_silent_hang_is_killed_by_watchdog(tmp_path, fake_bin, monkeypatch):
    monkeypatch.setenv("FAKE_MODE", "hang")
    t0 = time.monotonic()
    _result, rc = _launch(tmp_path, wall=0.03)  # ~2s
    assert rc == 124
    assert time.monotonic() - t0 < 20


def test_session_log_is_appended_not_truncated(tmp_path, fake_bin):
    _launch(tmp_path)
    first = (tmp_path / "logs" / "s.jsonl").read_text()
    _launch(tmp_path)
    assert (tmp_path / "logs" / "s.jsonl").read_text() == first * 2


# --- error / cap detection --------------------------------------------------
def _dispatcher(tmp_path, **extra):
    return dmod.Dispatcher(_cfg(tmp_path, **extra), str(tmp_path / ".env"))


def test_errored_worker_is_blocked_not_gated(tmp_path):
    d = _dispatcher(tmp_path)
    res = SessionResult(is_error=True, result_text="Invalid API key", num_turns=1)
    assert "worker error" in d._cap_hit(res, 1) and "Invalid API key" in d._cap_hit(res, 1)
    assert d._cap_hit(SessionResult(num_turns=1), 0) is None


def test_per_task_cost_cap(tmp_path):
    d = _dispatcher(tmp_path, TASK_CAP_USD="1.0")
    d.ledger.upsert_claim(1, "task-1-a", "task-1-a")
    assert d._over_task_budget(1) is None
    d.ledger.add_cost(1, 1.2)
    assert "task cost" in d._over_task_budget(1)


def test_requeue_resets_budget_and_retries(tmp_path):
    led = Ledger(tmp_path / "l.db")
    led.upsert_claim(1, "s", "s")
    led.update(1, test_retries=2, review_retries=1, state="blocked")
    led.add_cost(1, 9.0)
    led.upsert_claim(1, "s", "s")
    row = led.get(1)
    assert (row["test_retries"], row["review_retries"], row["cost_usd"]) == (0, 0, 0)


# --- review gate --------------------------------------------------------------
@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "init", "-q", "-b", "main")
    (r / "a.txt").write_text("a\n")
    _git(r, "add", "."); _git(r, "commit", "-qm", "base")
    _git(r, "checkout", "-qb", "task-1-x")
    return r


def _change(repo, size=10):
    (repo / "b.txt").write_text("b" * size + "\n")
    _git(repo, "add", "."); _git(repo, "commit", "-qm", "work")


def test_review_cost_is_reported(tmp_path, repo, fake_bin, monkeypatch):
    _change(repo)
    monkeypatch.setenv("FAKE_TEXT", '{"verdict":"pass","findings":[]}')
    v = adversarial_review(_cfg(tmp_path, repo), repo, "spec", tmp_path / "r.jsonl")
    assert v["verdict"] == "pass" and v["cost_usd"] == 0.5 and not v["truncated"]


def test_review_session_failure_is_flagged_as_error(tmp_path, repo, fake_bin, monkeypatch):
    _change(repo)
    monkeypatch.setenv("FAKE_MODE", "error")
    monkeypatch.setenv("FAKE_TEXT", "API overloaded")
    v = adversarial_review(_cfg(tmp_path, repo), repo, "spec", tmp_path / "r.jsonl")
    assert v["verdict"] == "block" and v["error"] is True


def test_truncated_diff_cannot_pass_silently(tmp_path, repo, fake_bin, monkeypatch):
    _change(repo, size=MAX_DIFF_CHARS + 5000)
    monkeypatch.setenv("FAKE_TEXT", '{"verdict":"pass","findings":[]}')
    v = adversarial_review(_cfg(tmp_path, repo), repo, "spec", tmp_path / "r.jsonl")
    assert v["truncated"] is True
    assert any("only the first" in f["note"] for f in v["findings"])


def test_unrecognised_verdict_is_a_block(tmp_path, repo, fake_bin, monkeypatch):
    _change(repo)
    monkeypatch.setenv("FAKE_TEXT", '{"verdict":"looks good"}')
    v = adversarial_review(_cfg(tmp_path, repo), repo, "spec", tmp_path / "r.jsonl")
    assert v["verdict"] == "block"


# --- gate loop: re-verify after every worker turn -----------------------------
def test_verify_reruns_tests_after_commit_or_discard(tmp_path, monkeypatch):
    d = _dispatcher(tmp_path)
    d.ledger.upsert_claim(1, "s", "s")
    runs, dirty = [], iter([True, False])
    monkeypatch.setattr(dmod, "run_tests", lambda *a: (runs.append(1) or True, ""))
    monkeypatch.setattr(dmod, "working_tree_dirty", lambda wt: next(dirty))
    monkeypatch.setattr(d, "_resume_step", lambda *a: None)
    assert d._verify(1, tmp_path, "s", dmod._Run(SessionResult(), "sid")) is None
    assert len(runs) == 2  # tests ran again after the discard turn


def test_verify_blocks_if_still_dirty_after_settle_turn(tmp_path, monkeypatch):
    d = _dispatcher(tmp_path)
    d.ledger.upsert_claim(1, "s", "s")
    monkeypatch.setattr(dmod, "run_tests", lambda *a: (True, ""))
    monkeypatch.setattr(dmod, "working_tree_dirty", lambda wt: True)
    monkeypatch.setattr(d, "_resume_step", lambda *a: None)
    stop = d._verify(1, tmp_path, "s", dmod._Run(SessionResult(), "sid"))
    assert stop and stop[0] == "block" and "uncommitted" in stop[1]


# --- claiming / restart ---------------------------------------------------------
class _VK:
    def __init__(self, marker=False):
        self.marker = marker

    def board(self, *_):
        return [{"title": "Ready", "tasks": [{"id": 1, "labels": [{"title": "claude"}]}]}]

    def tasks_in_bucket(self, b):
        return b["tasks"]

    def task_labels(self, t):
        return {"claude"}

    def has_comment_prefix(self, *_):
        return self.marker


@pytest.mark.parametrize("state,marker,expected", [
    (None, False, 1),        # fresh card
    (None, True, 0),         # claimed before, ledger lost: don't double-claim
    ("blocked", True, 1),    # human moved a blocked card back to Ready: retry
    ("pr_open", True, 0),
    ("working", True, 0),
    ("done", True, 0),
])
def test_ready_tasks_claim_guard(tmp_path, state, marker, expected):
    d = _dispatcher(tmp_path)
    d.vk = _VK(marker)
    if state:
        d.ledger.upsert_claim(1, "s", "s")
        d.ledger.update(1, state=state)
    assert len(d.ready_tasks(limit=5)) == expected


def test_reap_orphans_moves_card_and_never_kills_non_claude_pid(tmp_path, monkeypatch):
    d = _dispatcher(tmp_path)
    d.ledger.upsert_claim(1, "s", "s")
    d.ledger.update(1, state="working", pid=1)  # pid 1 is alive but is not claude
    moved, killed = [], []
    monkeypatch.setattr(d, "move", lambda tid, key: moved.append((tid, key)))
    monkeypatch.setattr(d, "comment", lambda *a: None)
    monkeypatch.setattr(dmod, "kill_group", lambda pid: killed.append(pid))
    d.reap_orphans()
    assert d.ledger.get(1)["state"] == "blocked" and d.ledger.get(1)["pid"] is None
    assert moved == [(1, "blocked")] and killed == []


def test_second_dispatcher_cannot_start(tmp_path):
    lock = tmp_path / "p.lock"
    first = dmod._acquire_instance_lock(lock)
    with pytest.raises(SystemExit):
        dmod._acquire_instance_lock(lock)
    first.close()


# --- cleanup ------------------------------------------------------------------------
def test_old_closed_pr_does_not_tear_down_requeued_task():
    prs = [{"headRefName": "task-1-x", "state": "CLOSED"},
           {"headRefName": "task-1-x", "state": "OPEN"},
           {"headRefName": "task-2-y", "state": "MERGED"}]
    assert [p["headRefName"] for p in cleanup.closed_prs_to_process(prs)] == ["task-2-y"]


def test_stale_blocked_pruned_once(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, BLOCKED_RETENTION_DAYS="0")
    led = Ledger(tmp_path / "l.db")
    led.upsert_claim(1, "task-1-x", "task-1-x")
    led.update(1, state="blocked")
    time.sleep(1.1)
    calls = []
    nt = Ntree(tmp_path)
    monkeypatch.setattr(nt, "rm", lambda s: calls.append(("rm", s)))
    monkeypatch.setattr(nt, "remove_branch", lambda s: calls.append(("branch", s)))
    assert cleanup.prune_stale(cfg, led, nt) == ["task-1-x"]
    assert cleanup.prune_stale(cfg, led, nt) == []  # not repeated
    assert ("rm", "task-1-x") in calls and ("branch", "task-1-x") in calls


# --- ntree wrapper / PR / repo prep ----------------------------------------------------
def test_failed_setup_command_is_fatal(tmp_path, monkeypatch):
    b = tmp_path / "bin"; b.mkdir()
    _script(b / "ntree", '#!/bin/sh\necho "! setup exited 1 — deps may be incomplete" >&2\nexit 0\n')
    monkeypatch.setenv("PATH", f"{b}:{__import__('os').environ['PATH']}")
    with pytest.raises(NtreeError, match="setup exited"):
        Ntree(tmp_path).new("task-1-x")


def test_pipeline_branch_detection():
    assert is_pipeline_branch("task-12-add-login")
    assert not is_pipeline_branch("main") and not is_pipeline_branch("feature/task-1")


def test_stop_hook_fails_when_tests_fail_even_under_dash(tmp_path, monkeypatch):
    b = tmp_path / "bin"; b.mkdir()
    _script(b / "ntree", "#!/bin/sh\necho FAIL\nexit 3\n")
    monkeypatch.setenv("PATH", f"{b}:{__import__('os').environ['PATH']}")
    monkeypatch.setenv("NTREE_WORKSPACE", "w")
    cp = subprocess.run(["sh", "-c", repoprep.stop_hook_command("uv run pytest -x -q")],
                        capture_output=True, text=True)
    assert cp.returncode != 0 and "FAIL" in cp.stdout


def test_rendered_settings_use_test_cmd():
    text = repoprep.render_worker_settings("npm test")
    assert "npm test" in repoprep.hook_command(text)
    assert repoprep.hook_command(text) == repoprep.stop_hook_command("npm test")


# --- end to end: the real run_pipeline against a fake claude and a real git repo -------
@pytest.fixture
def pipeline(tmp_path, repo, fake_bin, monkeypatch):
    _change(repo)  # the "worker's" commit, present on branch task-1-x
    d = _dispatcher(tmp_path, TARGET_REPO_PATH=str(repo), TASK_CAP_USD="10")
    d.logs_root = tmp_path / "logs"
    events = {"moved": [], "comments": [], "prs": [], "slack": []}
    d.move = lambda tid, key: events["moved"].append(key)
    d.comment = lambda tid, text: events["comments"].append(text)
    d.slack.task_blocked = lambda *a: events["slack"].append(a)
    d.slack.pr_opened = lambda *a: events["slack"].append(a)
    monkeypatch.setattr(d.ntree, "base_ref", lambda: "main")
    monkeypatch.setattr(d.ntree, "rm", lambda s: None)
    monkeypatch.setattr(d.ntree, "remove_branch", lambda s: None)
    monkeypatch.setattr(d.ntree, "new", lambda s, b=None: repo)
    monkeypatch.setattr(dmod, "run_tests", lambda *a: (True, ""))
    monkeypatch.setattr(dmod.prmod, "push_and_create",
                        lambda **kw: events["prs"].append(kw["branch"]) or "https://gh/pr/1")
    monkeypatch.setenv("FAKE_TEXT", '{"verdict":"pass","findings":[]}')
    d.ledger.upsert_claim(1, "task-1-x", "task-1-x")
    return d, events


def test_happy_path_opens_pr_and_counts_worker_and_review_cost(pipeline):
    d, ev = pipeline
    d.run_pipeline({"id": 1, "title": "x", "description": "<p>do it</p>"}, "task-1-x")
    row = d.ledger.get(1)
    assert row["state"] == "pr_open" and row["pid"] is None
    assert ev["prs"] == ["task-1-x"] and ev["moved"] == ["review"]
    assert row["cost_usd"] == pytest.approx(1.0)  # 0.5 worker + 0.5 review


def test_errored_worker_blocks_with_real_reason(pipeline, monkeypatch):
    d, ev = pipeline
    monkeypatch.setenv("FAKE_MODE", "error")
    monkeypatch.setenv("FAKE_TEXT", "Invalid API key")
    d.run_pipeline({"id": 1, "title": "x", "description": ""}, "task-1-x")
    assert d.ledger.get(1)["state"] == "blocked" and ev["prs"] == []
    assert any("worker error" in c and "Invalid API key" in c for c in ev["comments"])


def test_cost_cap_stops_before_review(pipeline):
    d, ev = pipeline
    object.__setattr__(d.cfg, "task_cap_usd", 0.4)  # one worker run (0.5) already exceeds it
    d.run_pipeline({"id": 1, "title": "x", "description": ""}, "task-1-x")
    assert d.ledger.get(1)["state"] == "blocked" and ev["prs"] == []
    assert any("task cost" in c for c in ev["comments"])


# --- worker credentials file --------------------------------------------------------------
def test_credentials_are_scoped_to_the_subprocess_that_needs_them(tmp_path, monkeypatch):
    from dispatcher import procenv

    f = tmp_path / "worker.env"
    f.write_text("# creds\nexport CLAUDE_CODE_OAUTH_TOKEN=tok1\nGH_TOKEN='gh1'\n"
                 "VIKUNJA_TOKEN=vk\nBAD LINE\nQ=\"a b\"\n")
    monkeypatch.setenv("WORKER_ENV_FILE", str(f))
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setenv("CLAUDE_CODE_MESSAGING_TOKEN", "leak")
    monkeypatch.setenv("GH_TOKEN", "from-the-dispatchers-own-env")   # inherited secrets are stripped too
    secrets = {"CLAUDE_CODE_OAUTH_TOKEN", "GH_TOKEN", "GITHUB_TOKEN", "VIKUNJA_TOKEN"}

    plain = procenv.clean_env()                       # test gate, ntree, local git
    assert not (secrets & set(plain)) and plain["Q"] == "a b"
    claude = procenv.clean_env("claude")              # worker / reviewer
    assert claude["CLAUDE_CODE_OAUTH_TOKEN"] == "tok1" and "GH_TOKEN" not in claude
    assert "VIKUNJA_TOKEN" not in claude
    gh = procenv.clean_env("github")                  # gh / git push
    assert gh["GH_TOKEN"] == "gh1" and "CLAUDE_CODE_OAUTH_TOKEN" not in gh
    for env in (plain, claude, gh):
        assert "CLAUDECODE" not in env and "CLAUDE_CODE_MESSAGING_TOKEN" not in env
    f.write_text("export CLAUDE_CODE_OAUTH_TOKEN=tok2\n")  # rotated between tasks
    assert procenv.clean_env("claude")["CLAUDE_CODE_OAUTH_TOKEN"] == "tok2"


def test_missing_worker_env_file_is_fine(tmp_path, monkeypatch):
    from dispatcher import procenv

    monkeypatch.setenv("WORKER_ENV_FILE", str(tmp_path / "nope"))
    assert isinstance(procenv.clean_env(), dict)


# --- git config lock / tracking -------------------------------------------------------------
def test_ntree_runs_with_autosetupmerge_disabled(tmp_path, monkeypatch):
    b = tmp_path / "bin"; b.mkdir()
    _script(b / "ntree", '#!/bin/sh\necho "$GIT_CONFIG_COUNT $GIT_CONFIG_KEY_0=$GIT_CONFIG_VALUE_0"\n')
    monkeypatch.setenv("PATH", f"{b}:{__import__('os').environ['PATH']}")
    cp = Ntree(tmp_path)._run(["x"])
    assert cp.stdout.strip() == "1 branch.autoSetupMerge=false"


def test_with_git_config_preserves_existing_entries():
    from dispatcher.procenv import with_git_config

    env = with_git_config({"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "a.b",
                           "GIT_CONFIG_VALUE_0": "1"}, branch__autoSetupMerge="false")
    assert env["GIT_CONFIG_COUNT"] == "2" and env["GIT_CONFIG_KEY_0"] == "a.b"
    assert env["GIT_CONFIG_KEY_1"] == "branch.autoSetupMerge"


def test_stale_locks_are_swept_but_fresh_ones_are_not(tmp_path):
    import os

    r = tmp_path / "r"; r.mkdir()
    _git(r, "init", "-q", "-b", "main")
    g = r / ".git"
    (g / "worktrees" / "w").mkdir(parents=True)
    locks = [g / "config.lock", g / "worktrees" / "w" / "index.lock"]
    for lk in locks:
        lk.write_text("")
    nt = Ntree(r)
    assert nt.clear_stale_git_locks() == [] and all(lk.exists() for lk in locks)   # fresh
    old = time.time() - 600
    for lk in locks:
        os.utime(lk, (old, old))
    assert sorted(nt.clear_stale_git_locks()) == sorted(locks)
    assert not any(lk.exists() for lk in locks)


def test_lock_held_by_a_live_process_is_never_removed(tmp_path):
    import os

    r = tmp_path / "r"; r.mkdir()
    _git(r, "init", "-q", "-b", "main")
    lock = r / ".git" / "config.lock"
    holder = subprocess.Popen(["sh", "-c", f"exec 9>{lock}; sleep 30"])
    try:
        time.sleep(0.3)
        old = time.time() - 600
        os.utime(lock, (old, old))
        assert Ntree(r).clear_stale_git_locks() == [] and lock.exists()
    finally:
        holder.kill(); holder.wait()


def test_new_waits_and_retries_when_a_lock_blocks_it(tmp_path, monkeypatch):
    import os

    from dispatcher import ntree as ntmod

    r = tmp_path / "r"; r.mkdir()
    _git(r, "init", "-q", "-b", "main")
    lock = r / ".git" / "config.lock"
    lock.write_text("")
    old = time.time() - 600
    os.utime(lock, (old, old))
    b = tmp_path / "bin"; b.mkdir()
    _script(b / "ntree", f"""#!/bin/sh
case "$1" in
  new) if [ -e "{lock}" ]; then echo "error: could not lock config file .git/config: File exists" >&2; exit 255; fi ;;
  path) echo "{tmp_path}/ws" ;;
esac
exit 0
""")
    monkeypatch.setenv("PATH", f"{b}:{os.environ['PATH']}")
    monkeypatch.setattr(ntmod, "LOCK_RETRY_SLEEP", 0.01)
    assert Ntree(r).new("task-1-x") == Path(f"{tmp_path}/ws")
    assert not lock.exists()


def test_new_gives_up_after_bounded_retries_on_a_live_lock(tmp_path, monkeypatch):
    import os

    from dispatcher import ntree as ntmod

    b = tmp_path / "bin"; b.mkdir()
    _script(b / "ntree", '#!/bin/sh\necho "error: could not lock config file .git/config" >&2\nexit 255\n')
    monkeypatch.setenv("PATH", f"{b}:{os.environ['PATH']}")
    monkeypatch.setattr(ntmod, "LOCK_RETRY_SLEEP", 0.01)
    r = tmp_path / "r"; r.mkdir(); _git(r, "init", "-q", "-b", "main")
    with pytest.raises(NtreeError, match="could not lock"):
        Ntree(r).new("task-1-x")


def test_kill_group_gives_sigterm_handlers_time_to_clean_up(tmp_path):
    """git removes its locks on SIGTERM; a SIGKILL after 2s (the old grace) would not let it."""
    lock = tmp_path / "x.lock"
    child = subprocess.Popen(
        ["python3", "-c", (
            "import signal,sys,time,pathlib\n"
            f"p=pathlib.Path({str(lock)!r}); p.write_text('')\n"
            "def bye(*a):\n    time.sleep(3.5); p.unlink(); sys.exit(0)\n"
            "signal.signal(signal.SIGTERM, bye); print('ready',flush=True); time.sleep(60)")],
        stdout=subprocess.PIPE, text=True, start_new_session=True)
    child.stdout.readline()
    from dispatcher.worker import kill_group

    kill_group(child.pid, grace=15, proc=child)
    assert not lock.exists()           # cleaned up by its own handler, not SIGKILLed
    assert child.returncode == 0


def test_kill_group_still_sigkills_a_process_that_ignores_sigterm(tmp_path):
    child = subprocess.Popen(
        ["python3", "-c", (
            "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
            "print('ready',flush=True); time.sleep(60)")],
        stdout=subprocess.PIPE, text=True, start_new_session=True)
    child.stdout.readline()
    from dispatcher.worker import kill_group

    t0 = time.monotonic()
    kill_group(child.pid, grace=1, proc=child)
    assert child.wait(timeout=5) != 0 and time.monotonic() - t0 < 10


# --- worker environment / reviewer settings / restart clock ----------------------------------
def test_worker_env_comes_from_env_workspace_with_slug_fallback(tmp_path):
    (tmp_path / ".env.workspace").write_text("NTREE_WORKSPACE=ws1\nPORT=8102\nAPI_PORT=8102\n")
    assert dmod.workspace_env(tmp_path, "slug")["PORT"] == "8102"
    assert dmod.workspace_env(tmp_path, "slug")["NTREE_WORKSPACE"] == "ws1"
    empty = tmp_path / "empty"; empty.mkdir()
    assert dmod.workspace_env(empty, "slug") == {"NTREE_WORKSPACE": "slug"}


def test_launched_worker_actually_receives_workspace_env(tmp_path, fake_bin, monkeypatch):
    import json

    out = tmp_path / "env.json"
    monkeypatch.setenv("FAKE_ENV_OUT", str(out))
    monkeypatch.delenv("NTREE_WORKSPACE", raising=False)
    _launch(tmp_path, extra_env={"NTREE_WORKSPACE": "ws1", "PORT": "8102"})
    assert json.loads(out.read_text())["env"] == {"NTREE_WORKSPACE": "ws1", "PORT": "8102"}


def test_dispatcher_launch_passes_worktree_env_to_worker(tmp_path, monkeypatch):
    d = _dispatcher(tmp_path)
    d.ledger.upsert_claim(1, "task-1-x", "task-1-x")
    (tmp_path / ".env.workspace").write_text("NTREE_WORKSPACE=task-1-x\nPORT=8105\n")
    seen = {}
    monkeypatch.setattr(dmod, "launch", lambda **kw: seen.update(kw) or (SessionResult(), 0))
    d._launch(1, tmp_path, "task-1-x", "p", "session.jsonl")
    assert seen["extra_env"] == {"NTREE_WORKSPACE": "task-1-x", "PORT": "8105"}


def test_reviewer_gets_settings_without_the_stop_hook(tmp_path, repo, fake_bin, monkeypatch):
    import json

    _change(repo)
    (repo / ".claude").mkdir()
    (repo / ".claude" / "worker-settings.json").write_text(json.dumps(
        {"permissions": {"deny": ["Bash(rm -rf*)"]}, "hooks": {"Stop": [{"hooks": []}]}}))
    out = tmp_path / "args.json"
    monkeypatch.setenv("FAKE_ENV_OUT", str(out))
    monkeypatch.setenv("FAKE_TEXT", '{"verdict":"pass","findings":[]}')
    adversarial_review(_cfg(tmp_path, repo), repo, "spec", tmp_path / "logs" / "r.jsonl")
    argv = json.loads(out.read_text())["argv"]
    used = json.loads(Path(argv[argv.index("--settings") + 1]).read_text())
    assert "hooks" not in used and used["permissions"]["deny"] == ["Bash(rm -rf*)"]


def test_requeue_restarts_the_elapsed_clock(tmp_path):
    led = Ledger(tmp_path / "l.db")
    led.upsert_claim(1, "s", "s")
    led.conn.execute("UPDATE tasks SET started_at='2020-01-01T00:00:00' WHERE task_id=1")
    led.conn.commit()
    led.upsert_claim(1, "s", "s")
    assert led.get(1)["started_at"] > "2020-01-01T00:00:00"


# =====================================================================================
# Audit 2
# =====================================================================================
def _pr_open_row(led, tid=7, url="https://gh/pr/2"):
    led.upsert_claim(tid, f"task-{tid}-x", f"task-{tid}-x")
    led.update(tid, state="pr_open", pr_url=url)


class _VKStub:
    def __init__(self):
        self.calls = []

    def move_task(self, *a):
        self.calls.append(("move", a[-1]))

    def comment(self, tid, text):
        self.calls.append(("comment", tid, text))


class _NTStub:
    def __init__(self):
        self.removed = []

    def rm(self, slug):
        self.removed.append(slug)


def _run_cleanup(monkeypatch, tmp_path, led, prs):
    monkeypatch.setattr(cleanup, "open_worker_prs", lambda cfg: prs)
    monkeypatch.setattr(cleanup.subprocess, "run",
                        lambda *a, **k: subprocess.CompletedProcess(a[0], 0, "", ""))
    vk, nt = _VKStub(), _NTStub()
    b = {"done": {"project_view_id": 1, "id": 1}, "blocked": {"project_view_id": 1, "id": 2}}
    cleanup.process_closed_prs(_cfg(tmp_path), led, nt, vk, b, None)
    return vk, nt


def test_old_closed_pr_never_tears_down_a_running_retry(tmp_path, monkeypatch):
    led = Ledger(tmp_path / "l.db")
    led.upsert_claim(7, "task-7-x", "task-7-x")
    led.update(7, state="working")                        # re-queued; worker is running
    old = {"number": 1, "state": "CLOSED", "headRefName": "task-7-x", "url": "https://gh/pr/1"}
    vk, nt = _run_cleanup(monkeypatch, tmp_path, led, [old])
    assert nt.removed == [] and vk.calls == [] and led.get(7)["state"] == "working"


def test_old_closed_pr_is_ignored_once_the_retry_has_its_own_pr(tmp_path, monkeypatch):
    led = Ledger(tmp_path / "l.db")
    _pr_open_row(led, url="https://gh/pr/2")
    old = {"number": 1, "state": "CLOSED", "headRefName": "task-7-x", "url": "https://gh/pr/1"}
    _vk, nt = _run_cleanup(monkeypatch, tmp_path, led, [old])
    assert nt.removed == [] and led.get(7)["state"] == "pr_open"


def test_merged_pr_moves_card_to_done_and_tears_down(tmp_path, monkeypatch):
    led = Ledger(tmp_path / "l.db")
    _pr_open_row(led)
    pr = {"number": 2, "state": "MERGED", "headRefName": "task-7-x", "url": "https://gh/pr/2",
          "mergeCommit": {"oid": "abcdef1234567"}}
    vk, nt = _run_cleanup(monkeypatch, tmp_path, led, [pr])
    assert nt.removed == ["task-7-x"] and led.get(7)["state"] == "done"
    assert ("move", 7) in vk.calls
    assert any("merged: abcdef123" in c[2] for c in vk.calls if c[0] == "comment")


def test_closed_unmerged_pr_blocks_the_card(tmp_path, monkeypatch):
    led = Ledger(tmp_path / "l.db")
    _pr_open_row(led)
    pr = {"number": 2, "state": "CLOSED", "headRefName": "task-7-x", "url": "https://gh/pr/2"}
    vk, _nt = _run_cleanup(monkeypatch, tmp_path, led, [pr])
    assert led.get(7)["state"] == "blocked"
    assert any("closed without merge" in c[2] for c in vk.calls if c[0] == "comment")


# --- open PR watch -------------------------------------------------------------------------
def test_pr_problem_detection():
    assert cleanup.pr_problem({"mergeable": "MERGEABLE", "statusCheckRollup": [
        {"name": "lint", "conclusion": "SUCCESS"}]}) == ""
    assert "conflicts" in cleanup.pr_problem({"mergeable": "CONFLICTING"})
    assert "failing checks: tests" in cleanup.pr_problem({"statusCheckRollup": [
        {"name": "tests", "conclusion": "FAILURE"}, {"name": "lint", "conclusion": "SUCCESS"}]})
    assert "changes requested" in cleanup.pr_problem({"reviewDecision": "CHANGES_REQUESTED"})
    assert cleanup.pr_problem({"mergeable": "UNKNOWN"}) == ""


def test_pr_watch_notifies_once_per_change(tmp_path, monkeypatch):
    led = Ledger(tmp_path / "l.db")
    _pr_open_row(led)
    pr = {"state": "OPEN", "headRefName": "task-7-x", "url": "https://gh/pr/2", "mergeable": "CONFLICTING"}
    alerts, vk = [], _VKStub()
    slack = type("S", (), {"alert": lambda self, t: alerts.append(t)})()
    monkeypatch.setattr(cleanup, "open_worker_prs", lambda cfg: [pr])
    cleanup.watch_open_prs(_cfg(tmp_path), led, vk, slack)
    cleanup.watch_open_prs(_cfg(tmp_path), led, vk, slack)          # unchanged: silent
    assert len(alerts) == 1 and len(vk.calls) == 1
    pr["mergeable"] = "MERGEABLE"
    cleanup.watch_open_prs(_cfg(tmp_path), led, vk, slack)
    assert len(vk.calls) == 2 and "healthy again" in vk.calls[1][2]


def test_old_task_logs_are_pruned(tmp_path):
    cfg = _cfg(tmp_path, LOG_RETENTION_DAYS="0")
    led = Ledger(tmp_path / "l.db")
    led.upsert_claim(1, "task-1-x", "task-1-x"); led.update(1, state="done")
    led.upsert_claim(2, "task-2-y", "task-2-y")                     # still active: keep
    (tmp_path / "logs" / "task-1-x").mkdir(parents=True)
    (tmp_path / "logs" / "task-2-y").mkdir(parents=True)
    time.sleep(1.1)
    assert cleanup.prune_logs(cfg, led, tmp_path / "logs") == 1
    assert not (tmp_path / "logs" / "task-1-x").exists() and (tmp_path / "logs" / "task-2-y").exists()


# --- timeouts must not leak process trees ----------------------------------------------------
def test_run_group_timeout_kills_the_whole_tree(tmp_path):
    from dispatcher.proc import TIMEOUT_RC, run_group

    marker = tmp_path / "grandchild.pid"
    cp = run_group(["sh", "-c", f"sleep 47 & echo $! > {marker}; sleep 47"], timeout=1, grace=2)
    assert cp.returncode == TIMEOUT_RC and "timed out" in cp.stderr
    time.sleep(0.3)
    pid = int(marker.read_text())
    alive = subprocess.run(["ps", "-p", str(pid), "-o", "stat="], capture_output=True, text=True).stdout.strip()
    assert alive == "" or alive.startswith("Z"), f"grandchild {pid} survived the timeout"


def test_gate_timeout_blocks_with_a_clear_reason(tmp_path, monkeypatch):
    from dispatcher.proc import TIMEOUT_RC
    from dispatcher.review import GateTimeout, run_tests

    class NT:
        def run(self, *a, **k):
            return subprocess.CompletedProcess([], TIMEOUT_RC, "", "")
    with pytest.raises(GateTimeout):
        run_tests(_cfg(tmp_path), NT(), "s")
    d = _dispatcher(tmp_path)
    d.ledger.upsert_claim(1, "s", "s")
    monkeypatch.setattr(dmod, "run_tests", lambda *a: (_ for _ in ()).throw(GateTimeout("test gate timed out after 30m")))
    stop = d._verify(1, tmp_path, "s", dmod._Run(SessionResult(), "sid"))
    assert stop == ("block", "gate: test gate timed out after 30m")


def test_gates_are_serialised(tmp_path, monkeypatch):
    import threading

    d = _dispatcher(tmp_path, MAX_GATES="1")
    live, peak, lock = [0], [0], threading.Lock()

    def fake(*a):
        with lock:
            live[0] += 1; peak[0] = max(peak[0], live[0])
        time.sleep(0.15)
        with lock:
            live[0] -= 1
        return True, ""
    monkeypatch.setattr(dmod, "run_tests", fake)
    ts = [threading.Thread(target=d._gate, args=("s",)) for _ in range(4)]
    [t.start() for t in ts]; [t.join() for t in ts]
    assert peak[0] == 1


# --- cancel is honoured at every phase --------------------------------------------------------
def test_cancel_during_setup_never_launches_a_worker(pipeline, monkeypatch):
    d, ev = pipeline
    d.ledger.request_cancel(1)
    monkeypatch.setattr(d, "_launch", lambda *a, **k: pytest.fail("worker launched after cancel"))
    d.run_pipeline({"id": 1, "title": "x", "description": ""}, "task-1-x")
    assert d.ledger.get(1)["state"] == "blocked" and "blocked" in ev["moved"]


def test_cancel_during_gate_skips_the_review(pipeline, monkeypatch):
    d, ev = pipeline

    def gate_then_cancel(*a):
        d.ledger.request_cancel(1)                 # operator cancels while tests run
        return True, ""
    monkeypatch.setattr(dmod, "run_tests", gate_then_cancel)
    monkeypatch.setattr(d, "_review", lambda *a, **k: pytest.fail("reviewer ran after cancel"))
    monkeypatch.setattr(d.cfg.__class__, "baseline_check", False, raising=False)
    object.__setattr__(d.cfg, "baseline_check", False)
    d.run_pipeline({"id": 1, "title": "x", "description": ""}, "task-1-x")
    assert d.ledger.get(1)["state"] == "blocked" and ev["prs"] == []


# --- transient failures and credential problems --------------------------------------------------
def _flaky(tmp_path, monkeypatch, fail_times, text):
    counter = tmp_path / "count"
    counter.write_text("0")
    monkeypatch.setenv("FAKE_FLAKY_COUNTER", str(counter))
    monkeypatch.setenv("FAKE_FLAKY_TIMES", str(fail_times))
    monkeypatch.setenv("FAKE_FLAKY_TEXT", text)
    monkeypatch.setenv("FAKE_MODE", "flaky")
    monkeypatch.setattr(dmod, "TRANSIENT_BACKOFF_S", (0.01, 0.01))
    return counter


def test_transient_error_is_retried_and_every_attempt_costs(tmp_path, fake_bin, monkeypatch):
    counter = _flaky(tmp_path, monkeypatch, 2, "API Error: 529 overloaded_error")
    d = _dispatcher(tmp_path)
    d.ledger.upsert_claim(1, "s", "s")
    result, rc = d._launch(1, tmp_path, "s", "p", "session.jsonl")
    assert rc == 0 and not result.is_error and counter.read_text() == "3"
    assert d.ledger.get(1)["cost_usd"] == pytest.approx(1.5)      # 3 attempts x 0.5


def test_transient_retries_are_bounded(tmp_path, fake_bin, monkeypatch):
    counter = _flaky(tmp_path, monkeypatch, 99, "API Error: 529 overloaded_error")
    d = _dispatcher(tmp_path, TRANSIENT_RETRIES="2")
    d.ledger.upsert_claim(1, "s", "s")
    _result, rc = d._launch(1, tmp_path, "s", "p", "session.jsonl")
    assert rc != 0 and counter.read_text() == "3"                 # 1 try + 2 retries, then give up


def test_auth_failure_is_not_retried_and_pauses_dispatching(tmp_path, fake_bin, monkeypatch):
    counter = _flaky(tmp_path, monkeypatch, 99, "Not logged in · Please run /login")
    d = _dispatcher(tmp_path)
    alerts = []
    d.slack.alert = alerts.append
    d.ledger.upsert_claim(1, "s", "s")
    result, rc = d._launch(1, tmp_path, "s", "p", "session.jsonl")
    assert counter.read_text() == "1"                              # no retry for a login problem
    stop = d._stop_reason(1, result, rc)
    assert stop[0] == "block" and "paused" in stop[1]
    assert d.ledger.is_paused() and len(alerts) == 1


# --- poll failures alert the human -----------------------------------------------------------------
def test_repeated_poll_failures_alert_once_then_recovery(tmp_path, monkeypatch):
    d = _dispatcher(tmp_path)
    alerts = []
    d.slack.alert = alerts.append
    boom = {"on": True}

    def tick():
        if boom["on"]:
            raise RuntimeError("401 from Vikunja")
    monkeypatch.setattr(d, "tick", tick)
    for _ in range(5):
        d._guarded_tick()
    assert len(alerts) == 1 and "401 from Vikunja" in alerts[0]
    boom["on"] = False
    d._guarded_tick()
    assert len(alerts) == 2 and "recovered" in alerts[1]


# --- baseline: a red base blocks for free ------------------------------------------------------------
def test_red_baseline_blocks_without_spending_on_a_worker(pipeline, monkeypatch):
    d, ev = pipeline
    monkeypatch.setattr(d.ntree, "rev", lambda ref: "deadbeef" * 5)
    monkeypatch.setattr(dmod, "run_tests", lambda *a: (False, "ImportError: libpango missing"))
    monkeypatch.setattr(d, "_launch", lambda *a, **k: pytest.fail("worker launched on a red base"))
    d.run_pipeline({"id": 1, "title": "x", "description": ""}, "task-1-x")
    assert d.ledger.get(1)["state"] == "blocked" and d.ledger.get(1)["cost_usd"] == 0
    assert any("baseline red" in c and "libpango" in c for c in ev["comments"])


def test_baseline_result_is_cached_per_commit(pipeline, monkeypatch):
    d, _ev = pipeline
    monkeypatch.setattr(d.ntree, "rev", lambda ref: "cafe" * 10)
    runs = []
    monkeypatch.setattr(dmod, "run_tests", lambda *a: (runs.append(1) or True, ""))
    assert d._baseline(1, Path("."), "s", "main") is None
    assert d._baseline(1, Path("."), "s", "main") is None
    assert len(runs) == 1                                          # second task reuses the result


def test_baseline_can_be_disabled(pipeline, monkeypatch):
    d, _ = pipeline
    object.__setattr__(d.cfg, "baseline_check", False)
    monkeypatch.setattr(dmod, "run_tests", lambda *a: pytest.fail("baseline ran while disabled"))
    assert d._baseline(1, Path("."), "s", "main") is None


# --- permission-denial detection ---------------------------------------------------------------------------
def test_shell_permission_denied_is_not_a_tool_denial():
    from dispatcher import stream

    shell = ('{"type":"user","message":{"content":[{"type":"tool_result","is_error":true,'
             '"content":"bash: ./deploy.sh: Permission denied"}]}}')
    assert stream.parse_lines([shell] * 3).permission_denials == []
    real = ('{"type":"user","message":{"content":[{"type":"tool_result","is_error":true,'
            '"content":"Claude requested permissions to use Bash, but you haven\'t granted it yet."}]}}')
    assert len(stream.parse_lines([real]).permission_denials) == 1


def test_result_event_denials_are_authoritative():
    from dispatcher import stream

    noisy = ('{"type":"user","message":{"content":[{"type":"tool_result","is_error":true,'
             '"content":"requested permissions to use Bash"}]}}')
    result = '{"type":"result","total_cost_usd":0.1,"num_turns":2,"permission_denials":[]}'
    assert stream.parse_lines([noisy, result]).permission_denials == []
    result2 = '{"type":"result","total_cost_usd":0.1,"permission_denials":[{"tool_name":"Bash"}]}'
    assert len(stream.parse_lines([result2]).permission_denials) == 1


# --- shutdown / retry bookkeeping ---------------------------------------------------------------------------------
def test_second_signal_force_stops_workers_and_exits(tmp_path, monkeypatch):
    d = _dispatcher(tmp_path)
    d.ledger.upsert_claim(1, "s", "s")
    d.ledger.update(1, state="working", pid=4242)
    killed, exited = [], []
    monkeypatch.setattr(dmod, "_is_claude_proc", lambda pid: True)
    monkeypatch.setattr(dmod, "kill_group", lambda pid, grace=0: killed.append(pid))
    monkeypatch.setattr(dmod.os, "_exit", lambda code: exited.append(code))
    d._on_signal(15, None)
    assert d._stop.is_set() and killed == []                        # first: just drain
    d._on_signal(15, None)
    assert killed == [4242] and exited == [1]                       # second: stop and leave


def test_retry_keeps_lifetime_cost(tmp_path):
    led = Ledger(tmp_path / "l.db")
    led.upsert_claim(1, "s", "s")
    led.add_cost(1, 2.0)
    led.upsert_claim(1, "s", "s")                                   # re-queued: budget resets
    led.add_cost(1, 1.0)
    row = led.get(1)
    assert row["cost_usd"] == pytest.approx(1.0) and row["lifetime_cost"] == pytest.approx(3.0)


# --- PR listing must survive a token that cannot read CI status ---------------------------------------
def _fake_gh(tmp_path, monkeypatch, checks_allowed):
    import os

    from dispatcher import pr as prmod

    b = tmp_path / "bin"; b.mkdir(exist_ok=True)
    rows = ('[{"number":3,"state":"OPEN","headRefName":"task-3-x","url":"u3","mergeable":"CONFLICTING"},'
            '{"number":2,"state":"CLOSED","headRefName":"task-2-x","url":"u2"}]')
    checks = '[{"number":3,"statusCheckRollup":[{"name":"ci","conclusion":"FAILURE"}]}]'
    if checks_allowed:
        on_checks = f"echo '{checks}'"
    else:
        on_checks = 'echo "GraphQL: Resource not accessible by personal access token" >&2; exit 1'
    _script(b / "gh", f"""#!/bin/sh
case "$*" in
  *statusCheckRollup*) {on_checks} ;;
  *) echo '{rows}' ;;
esac
""")
    monkeypatch.setenv("PATH", f"{b}:{os.environ['PATH']}")
    monkeypatch.setattr(prmod, "_checks_unavailable", False)
    return prmod


def test_pr_listing_survives_a_token_that_cannot_read_ci(tmp_path, monkeypatch):
    prmod = _fake_gh(tmp_path, monkeypatch, checks_allowed=False)
    rows = prmod.open_worker_prs(_cfg(tmp_path))
    assert [r["number"] for r in rows] == [3, 2]            # cleanup still sees every PR
    assert cleanup.pr_problem(rows[0]) == "merge conflicts with the base branch"
    assert prmod._checks_unavailable is True                # remembered; not retried every poll


def test_pr_listing_adds_ci_status_when_the_token_allows_it(tmp_path, monkeypatch):
    prmod = _fake_gh(tmp_path, monkeypatch, checks_allowed=True)
    rows = prmod.open_worker_prs(_cfg(tmp_path))
    assert "failing checks: ci" in cleanup.pr_problem(rows[0])


# =====================================================================================
# Resume instead of starting over
# =====================================================================================
def _calls(tmp_path, monkeypatch):
    out = tmp_path / "calls.jsonl"
    monkeypatch.setenv("FAKE_CALLS_OUT", str(out))
    return out


def _read_calls(path):
    import json

    return [json.loads(line) for line in path.read_text().splitlines()]


def _arm_resume(d, repo, monkeypatch, session="sess-old"):
    d.ledger.update(1, resumable=1, session_id=session)
    monkeypatch.setattr(d.ntree, "path", lambda s: repo)
    for name in ("rm", "remove_branch"):
        monkeypatch.setattr(d.ntree, name, lambda *a: pytest.fail("workspace was wiped on resume"))
    monkeypatch.setattr(d.ntree, "new", lambda *a: pytest.fail("workspace was recreated on resume"))
    monkeypatch.setattr(d, "_baseline", lambda *a: pytest.fail("baseline ran on a resumed workspace"))


def test_requeued_capped_task_resumes_its_session_without_wiping(pipeline, repo, tmp_path, monkeypatch):
    d, ev = pipeline
    calls = _calls(tmp_path, monkeypatch)
    _arm_resume(d, repo, monkeypatch)
    d.run_pipeline({"id": 1, "title": "x", "description": ""}, "task-1-x")
    first = _read_calls(calls)[0]
    assert first["argv"][first["argv"].index("--resume") + 1] == "sess-old"
    assert "Do NOT start over" in first["prompt"]
    assert d.ledger.get(1)["state"] == "pr_open" and d.ledger.get(1)["resumable"] == 0
    assert any("resuming the previous session" in c for c in ev["comments"])


def test_resume_falls_back_to_a_fresh_session_on_the_same_files(pipeline, repo, tmp_path, monkeypatch):
    d, _ = pipeline
    calls = _calls(tmp_path, monkeypatch)
    monkeypatch.setenv("FAKE_MODE", "noconv")
    _arm_resume(d, repo, monkeypatch)
    d.run_pipeline({"id": 1, "title": "x", "description": ""}, "task-1-x")
    c = _read_calls(calls)
    assert "--resume" in c[0]["argv"] and "--resume" not in c[1]["argv"]
    assert "previous attempt" in c[1]["prompt"] and "Do not start over" in c[1]["prompt"].replace("do not", "Do not")
    assert d.ledger.get(1)["state"] == "pr_open"


def test_turn_cap_with_progress_continues_the_same_session(pipeline, tmp_path, monkeypatch):
    d, ev = pipeline
    calls = _calls(tmp_path, monkeypatch)
    counter = tmp_path / "cap-count"; counter.write_text("0")
    monkeypatch.setenv("FAKE_CAP_COUNTER", str(counter))
    monkeypatch.setenv("FAKE_MODE", "turncap_once")
    d.run_pipeline({"id": 1, "title": "x", "description": ""}, "task-1-x")
    c = _read_calls(calls)
    assert "--resume" not in c[0]["argv"]                           # fresh first run
    assert c[1]["argv"][c[1]["argv"].index("--resume") + 1] == "s1"  # then the SAME session
    assert "ran out of turns" in c[1]["prompt"]
    assert d.ledger.get(1)["state"] == "pr_open"
    assert any("turn limit reached" in x for x in ev["comments"])


def test_continuations_are_bounded_and_the_block_stays_resumable(pipeline, tmp_path, monkeypatch):
    d, _ = pipeline
    calls = _calls(tmp_path, monkeypatch)
    counter = tmp_path / "cap-count"; counter.write_text("0")
    monkeypatch.setenv("FAKE_CAP_COUNTER", str(counter))
    monkeypatch.setenv("FAKE_MODE", "turncap_always")
    object.__setattr__(d.cfg, "max_continuations", 1)
    d.run_pipeline({"id": 1, "title": "x", "description": ""}, "task-1-x")
    assert len(_read_calls(calls)) == 2                             # 1 run + 1 continuation, then stop
    row = d.ledger.get(1)
    assert row["state"] == "blocked" and row["resumable"] == 1 and row["session_id"]


def test_no_continuation_when_the_worker_produced_nothing_or_budget_is_gone(tmp_path, repo):
    d = _dispatcher(tmp_path, TARGET_REPO_PATH=str(repo))
    d.ledger.upsert_claim(1, "task-1-x", "task-1-x")
    assert d._worth_continuing(1, repo, "main") is False            # clean branch == main: no progress
    _change(repo)
    assert d._worth_continuing(1, repo, "main") is True
    d.ledger.add_cost(1, 99.0)
    assert d._worth_continuing(1, repo, "main") is False            # over the task budget
    d.ledger.upsert_claim(1, "task-1-x", "task-1-x")                # fresh budget
    d.ledger.request_cancel(1)
    assert d._worth_continuing(1, repo, "main") is False            # operator cancelled


def test_only_cap_blocks_are_marked_resumable(tmp_path):
    d = _dispatcher(tmp_path)
    d.ledger.upsert_claim(1, "s", "s")
    d.move = lambda *a: None; d.comment = lambda *a: None
    for reason in ("gate: tests red", "gate: review blocking", "worker error (rc=1): x"):
        d._block(1, reason)
        assert d.ledger.get(1)["resumable"] == 0, reason
    for reason in ("cap: turns=60", "cap: wall-clock 45m", "cap: task cost $10.01 >= $10.00"):
        d.ledger.update(1, resumable=0)
        d._block(1, reason)
        assert d.ledger.get(1)["resumable"] == 1, reason


def test_resume_needs_a_session_a_workspace_and_progress(tmp_path, repo, monkeypatch):
    d = _dispatcher(tmp_path, TARGET_REPO_PATH=str(repo))
    d.ledger.upsert_claim(1, "task-1-x", "task-1-x")
    _change(repo)
    monkeypatch.setattr(d.ntree, "path", lambda s: repo)
    assert d._resume_workspace(1, "task-1-x", "main") is None        # not flagged resumable
    d.ledger.update(1, resumable=1)
    assert d._resume_workspace(1, "task-1-x", "main") is None        # no session id to resume
    d.ledger.update(1, session_id="sess")
    assert d._resume_workspace(1, "task-1-x", "main") == repo        # all three present
    monkeypatch.setattr(d.ntree, "path", lambda s: (_ for _ in ()).throw(NtreeError("gone")))
    assert d._resume_workspace(1, "task-1-x", "main") is None        # workspace was pruned


def test_requeue_keeps_session_and_resumable_flag(tmp_path):
    led = Ledger(tmp_path / "l.db")
    led.upsert_claim(1, "s", "s")
    led.update(1, session_id="sess", resumable=1, state="blocked")
    led.upsert_claim(1, "s", "s")                                   # human drags the card back
    row = led.get(1)
    assert row["session_id"] == "sess" and row["resumable"] == 1 and row["test_retries"] == 0


# =====================================================================================
# Cost of resumed sessions is cumulative: account only the new spend
# =====================================================================================
def _res(cost, sid="s"):
    return SessionResult(cost_usd=cost, session_id=sid)


def test_resumed_session_cost_is_accounted_as_a_delta(tmp_path):
    d = _dispatcher(tmp_path)
    d.ledger.upsert_claim(1, "s", "s")
    d._account_cost(1, _res(3.10))                       # fresh session
    d._account_cost(1, _res(4.58), resumed=True)         # resumed: reports 4.58 cumulative
    d._account_cost(1, _res(4.93), resumed=True)
    row = d.ledger.get(1)
    assert row["cost_usd"] == pytest.approx(4.93) and row["lifetime_cost"] == pytest.approx(4.93)
    assert row["session_cum"] == pytest.approx(4.93)
    assert d.ledger.today_cost() == pytest.approx(4.93)  # the daily cap sees the real spend too


def test_fresh_session_resets_the_cumulative_baseline(tmp_path):
    d = _dispatcher(tmp_path)
    d.ledger.upsert_claim(1, "s", "s")
    d._account_cost(1, _res(5.0))
    d._account_cost(1, _res(0.4))                        # a NEW conversation (e.g. the reviewer)
    assert d.ledger.get(1)["cost_usd"] == pytest.approx(5.4)
    d._account_cost(1, _res(0.9), resumed=True)          # resumes that conversation: +0.5 only
    assert d.ledger.get(1)["cost_usd"] == pytest.approx(5.9)


def test_failed_launch_with_no_cost_does_not_lose_the_baseline(tmp_path):
    d = _dispatcher(tmp_path)
    d.ledger.upsert_claim(1, "s", "s")
    d._account_cost(1, _res(3.0))
    d._account_cost(1, _res(0.0), resumed=True)          # e.g. a transient failure before any spend
    d._account_cost(1, _res(3.5), resumed=True)
    assert d.ledger.get(1)["cost_usd"] == pytest.approx(3.5)


def test_counter_restart_is_not_treated_as_negative_spend(tmp_path):
    d = _dispatcher(tmp_path)
    d.ledger.upsert_claim(1, "s", "s")
    d._account_cost(1, _res(3.0))
    d._account_cost(1, _res(1.0), resumed=True)          # CLI reported less than before: restarted
    assert d.ledger.get(1)["cost_usd"] == pytest.approx(4.0)


def test_launch_end_to_end_with_a_cumulative_cost_reporting_cli(tmp_path, fake_bin, monkeypatch):
    cum = tmp_path / "cum"
    monkeypatch.setenv("FAKE_CUM_FILE", str(cum))
    monkeypatch.setenv("FAKE_MODE", "cumcost")
    d = _dispatcher(tmp_path)
    d.ledger.upsert_claim(1, "s", "s")
    d._launch(1, tmp_path, "s", "p", "session.jsonl")                                  # 0.5
    d._launch(1, tmp_path, "s", "p", "session.jsonl", resume_session="s1")            # reports 1.0
    d._launch(1, tmp_path, "s", "p", "session.jsonl", resume_session="s1")            # reports 1.5
    assert d.ledger.get(1)["cost_usd"] == pytest.approx(1.5)                            # not 3.0
    assert d.ledger.today_cost() == pytest.approx(1.5)


# =====================================================================================
# Deleted tickets: forget their rows, workspaces, branches and logs
# =====================================================================================
class _TicketsVK:
    """exists: task_id -> True (exists) / False (deleted) / None (can't tell)."""

    def __init__(self, exists):
        self.exists = exists

    def task_exists(self, tid):
        return self.exists.get(tid, True)


class _NT2:
    def __init__(self):
        self.rm_calls, self.branch_calls = [], []

    def rm(self, slug):
        self.rm_calls.append(slug)

    def remove_branch(self, slug):
        self.branch_calls.append(slug)


def _row(led, tid, state):
    led.upsert_claim(tid, f"task-{tid}-x", f"task-{tid}-x")
    led.update(tid, state=state)


def test_sync_removes_finished_tasks_whose_ticket_is_deleted(tmp_path):
    led = Ledger(tmp_path / "l.db")
    _row(led, 1, "blocked"); _row(led, 2, "done"); _row(led, 3, "blocked")
    for tid in (1, 2, 3):
        (tmp_path / "logs" / f"task-{tid}-x").mkdir(parents=True)
    nt = _NT2()
    gone = cleanup.sync_deleted_tasks(_cfg(tmp_path), led, nt, _TicketsVK({1: False, 2: False}),
                                      tmp_path / "logs")
    assert sorted(gone) == [1, 2]
    assert led.get(1) is None and led.get(2) is None and led.get(3) is not None
    assert sorted(nt.rm_calls) == ["task-1-x", "task-2-x"] and sorted(nt.branch_calls) == ["task-1-x", "task-2-x"]
    assert not (tmp_path / "logs" / "task-1-x").exists() and (tmp_path / "logs" / "task-3-x").exists()


@pytest.mark.parametrize("state", ["claimed", "working", "testing", "reviewing", "pr_open"])
def test_sync_never_touches_running_tasks_or_ones_with_an_open_pr(tmp_path, state):
    led = Ledger(tmp_path / "l.db")
    _row(led, 1, state)
    nt = _NT2()
    assert cleanup.sync_deleted_tasks(_cfg(tmp_path), led, nt, _TicketsVK({1: False}), tmp_path / "logs") == []
    assert led.get(1) is not None and nt.rm_calls == []


def test_sync_treats_an_unreachable_vikunja_as_unknown_not_deleted(tmp_path):
    led = Ledger(tmp_path / "l.db")
    _row(led, 1, "blocked")
    nt = _NT2()
    assert cleanup.sync_deleted_tasks(_cfg(tmp_path), led, nt, _TicketsVK({1: None}), tmp_path / "logs") == []
    assert led.get(1) is not None and nt.rm_calls == []


def test_task_exists_distinguishes_404_from_other_failures():
    import requests

    from dispatcher.vikunja import Vikunja, VikunjaError

    vk = Vikunja("http://x/api/v1", "t")
    results = iter([{"id": 1}, VikunjaError("gone", status=404), VikunjaError("boom", status=500),
                    VikunjaError("denied", status=403), requests.ConnectionError("down")])

    def fake(task_id):
        r = next(results)
        if isinstance(r, Exception):
            raise r
        return r
    vk.task = fake
    assert [vk.task_exists(1) for _ in range(5)] == [True, False, None, None, None]


def test_nightly_reports_removed_tickets(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    led = Ledger(tmp_path / "l.db")
    _row(led, 1, "blocked")
    nt = Ntree(tmp_path)
    monkeypatch.setattr(nt, "rm", lambda s: None)
    monkeypatch.setattr(nt, "remove_branch", lambda s: None)
    monkeypatch.setattr(nt, "clear_stale_git_locks", lambda *a, **k: [])
    monkeypatch.setattr(nt, "doctor", lambda: subprocess.CompletedProcess([], 0, "", ""))
    out = cleanup.nightly(cfg, nt, None, led, tmp_path / "logs", vikunja=_TicketsVK({1: False}))
    assert "removed 1 task(s) whose ticket was deleted: #1" in out and led.get(1) is None


def test_startup_sync_failure_never_blocks_the_dispatcher(tmp_path, monkeypatch):
    d = _dispatcher(tmp_path)
    monkeypatch.setattr(dmod, "sync_deleted_tasks", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("vikunja down")))
    monkeypatch.setattr(d, "refresh_board", lambda: None)
    monkeypatch.setattr(d, "reap_orphans", lambda: None)
    monkeypatch.setattr(d.ntree, "clear_stale_git_locks", list)
    monkeypatch.setattr(d, "_guarded_tick", lambda: d._stop.set())
    monkeypatch.setattr(dmod.signal, "signal", lambda *a: None)
    d.run()                                  # would raise if the sync error escaped
    assert d._stop.is_set()


def test_dashboard_sync_reports_what_it_removed(tmp_path, monkeypatch):
    from dispatcher import dashboard

    led = Ledger(tmp_path / "l.db")
    _row(led, 7, "blocked")
    monkeypatch.setattr(cleanup, "sync_deleted_tasks", lambda *a, **k: [7, 9])
    assert dashboard.sync_deleted(_cfg(tmp_path), led) == "removed #7, #9"
    monkeypatch.setattr(cleanup, "sync_deleted_tasks", lambda *a, **k: [])
    assert dashboard.sync_deleted(_cfg(tmp_path), led) == "nothing to remove"


# =====================================================================================
# Filtering the task list
# =====================================================================================
def _mixed(tmp_path, db="l.db"):
    led = Ledger(tmp_path / db)
    for tid, state in [(1, "blocked"), (2, "done"), (3, "working"), (4, "blocked"),
                       (5, "pr_open"), (6, "testing"), (7, "claimed"), (8, "reviewing")]:
        _row(led, tid, state)
    return led


def test_filter_groups_tasks_by_what_a_human_cares_about(tmp_path):
    from dispatcher import dashboard

    tasks = dashboard.gather_status(_cfg(tmp_path), _mixed(tmp_path), tmp_path / "logs")["tasks"]
    ids = lambda name: sorted(t["task_id"] for t in dashboard.filter_tasks(tasks, name))
    assert ids("all") == [1, 2, 3, 4, 5, 6, 7, 8]
    assert ids("active") == [3, 6, 7, 8]          # claimed / working / testing / reviewing
    assert ids("blocked") == [1, 4]               # needs a decision
    assert ids("pr_open") == [5]                  # waiting for review/merge
    assert ids("done") == [2]
    assert ids("nonsense") == [1, 2, 3, 4, 5, 6, 7, 8]   # unknown name never hides anything


def test_snapshot_text_filter_shows_the_count_and_an_empty_message(tmp_path):
    from dispatcher import dashboard

    led = _mixed(tmp_path)
    out = dashboard.snapshot_text(_cfg(tmp_path), led, tmp_path / "logs", state="blocked")
    assert "filter: blocked (2 of 8)" in out
    assert "    1  blocked" in out and "    4  blocked" in out and "    2  done" not in out
    assert "filter:" not in dashboard.snapshot_text(_cfg(tmp_path), led, tmp_path / "logs")
    led2 = Ledger(tmp_path / "other.db"); _row(led2, 1, "done")
    assert "(no blocked tasks)" in dashboard.snapshot_text(_cfg(tmp_path), led2, tmp_path / "logs", state="blocked")


def test_tui_f_key_cycles_the_filter_and_selection_follows_the_visible_rows(tmp_path):
    import asyncio

    pytest.importorskip("textual")
    from textual.widgets import DataTable

    from dispatcher.tui import DashboardApp

    led = _mixed(tmp_path, db="p.db")                    # the DB file _cfg() points the app at
    led.conn.close()                                     # the app opens its own ledger connection

    async def run():
        app = DashboardApp(_cfg(tmp_path), str(tmp_path / ".env"))
        async with app.run_test(size=(120, 24)) as pilot:
            await pilot.pause(0.3)
            table = app.query_one("#tasks", DataTable)
            seen = {"all": len(app._row_ids)}
            for name in ("active", "blocked", "pr_open", "done"):
                await pilot.press("f")
                await pilot.pause(0.2)
                seen[name] = sorted(app._row_ids)
            await pilot.press("f")                       # wraps back to all
            await pilot.pause(0.2)
            seen["wrapped"] = len(app._row_ids)
            # in the blocked view, row 1 must be task 4 - retry/cancel act on THIS mapping
            for _ in range(2):                           # all -> active -> blocked
                await pilot.press("f")
                await pilot.pause(0.2)
            table.move_cursor(row=1)
            await pilot.pause(0.1)
            seen["selected_in_blocked"] = app._selected()["task_id"]
        return seen
    seen = asyncio.run(run())
    assert seen["all"] == 8 and seen["wrapped"] == 8
    assert seen["active"] == [3, 6, 7, 8] and seen["blocked"] == [1, 4]
    assert seen["pr_open"] == [5] and seen["done"] == [2]
    assert seen["selected_in_blocked"] == 4


# =====================================================================================
# Per-task detail view
# =====================================================================================
def _ev_assistant(mid, tool=None, target="", text="", model="m-opus", usage=None):
    block = ({"type": "tool_use", "name": tool, "input": {"file_path": target}} if tool
             else {"type": "text", "text": text})
    return {"type": "assistant", "message": {"id": mid, "model": model, "content": [block],
                                             "usage": usage or {"input_tokens": 10, "output_tokens": 100,
                                                                "cache_creation_input_tokens": 1000,
                                                                "cache_read_input_tokens": 50000}}}


def _ev_result(cost, turns=5, sub="success", model="m-opus"):
    return {"type": "result", "subtype": sub, "total_cost_usd": cost, "num_turns": turns,
            "modelUsage": {model: {"inputTokens": 100, "outputTokens": 1000, "cacheCreationInputTokens": 5000,
                                   "cacheReadInputTokens": 500000, "costUSD": cost}}}


def _write_log(path, events):
    import json

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(e) for e in events) + "\n")


def test_sessions_split_at_each_result_and_expose_the_one_in_progress():
    from dispatcher import detail

    evs = [_ev_assistant("a"), _ev_result(1.0), _ev_assistant("b"), _ev_result(1.5), _ev_assistant("c")]
    groups = detail.sessions(evs)
    assert [len(g) for g in groups] == [2, 2, 1]
    assert detail.sessions(evs[:4])[-1][-1]["type"] == "result"       # nothing in progress
    assert detail.sessions([]) == []


def test_session_summary_counts_turns_once_per_message_and_lists_latest_actions():
    from dispatcher import detail

    evs = [_ev_assistant("m1", "Read", "/w/a.py"), _ev_assistant("m1", "Edit", "/w/a.py"),   # same message
           _ev_assistant("m2", "Bash", "pytest -q"), _ev_assistant("m3", text="all done")]
    s = detail.summarize_session(evs)
    assert s["turns"] == 3 and s["tools"] == {"Read": 1, "Edit": 1, "Bash": 1}
    assert s["actions"][-1].startswith("Bash:") and "pytest" in s["actions"][-1]
    assert s["last_text"] == "all done"


def test_past_sessions_report_new_spend_not_the_running_total():
    from dispatcher import detail

    past = detail.past_sessions([_ev_result(3.10, 61, "error_max_turns"), _ev_result(4.58, 11), _ev_result(4.93, 4)])
    assert [round(p["new_cost"], 2) for p in past] == [3.10, 1.48, 0.35]
    assert past[0]["outcome"] == "error_max_turns"


def test_live_cost_estimate_is_calibrated_per_model_and_not_inflated_by_repeated_usage():
    from dispatcher import detail

    done = [_ev_result(2.0, model="m-opus"), _ev_result(0.2, model="m-sonnet")]
    opus = detail.calibrate_input_price(done, "m-opus")
    sonnet = detail.calibrate_input_price(done, "m-sonnet")
    assert opus > sonnet * 5                                    # priced separately, not mixed
    once = [_ev_assistant("m1", "Read", "/a")]
    repeated = [_ev_assistant("m1", "Read", "/a"), _ev_assistant("m1", text="same message, 2nd block")]
    assert detail.estimate_cost(repeated, opus) == pytest.approx(detail.estimate_cost(once, opus))
    assert detail.calibrate_input_price([], "m-opus") is None


def _ps(*rows):
    return [{"pid": p, "ppid": pp, "age": age, "args": a} for p, pp, age, a in rows]


SLUG = "task-9-x"
CLAUDE = "/home/u/.local/bin/claude -p --output-format stream-json --verbose --max-turns 60"
REVIEW = CLAUDE + " --permission-mode plan"
GATE = f"bash /usr/local/bin/ntree run {SLUG} -- scripts/worker-test.sh"


def test_process_detection_tells_a_hook_gate_from_a_dispatcher_gate():
    from dispatcher import detail

    in_cwd = lambda pid: f"/w/.ntree/{SLUG}" if pid in (10, 20) else ""
    hook = _ps((1, 0, 9000, "python claude-pipeline run"), (10, 1, 700, CLAUDE),
               (11, 10, 500, "sh -c bash -c 'set -o pipefail; ntree run \"$NTREE_WORKSPACE\" ..."),
               (12, 11, 500, "bash -c set -o pipefail; ntree run ..."), (13, 12, 500, GATE))
    p = detail.find_processes(hook, SLUG, in_cwd)
    assert p["worker"]["pid"] == 10 and p["gate"] == {"age": 500, "kind": "stop-hook"}
    disp = _ps((1, 0, 9000, "python /x/claude-pipeline/.venv/bin/python claude-pipeline run"), (13, 1, 300, GATE))
    p = detail.find_processes(disp, SLUG, in_cwd)
    assert p["gate"] == {"age": 300, "kind": "dispatcher"} and p["worker"] is None
    rev = _ps((1, 0, 9000, "python claude-pipeline run"), (20, 1, 40, REVIEW))
    assert detail.find_processes(rev, SLUG, in_cwd)["reviewer"]["pid"] == 20
    other = _ps((10, 1, 700, CLAUDE))
    assert detail.find_processes(other, SLUG, lambda pid: "/w/.ntree/some-other-task")["worker"] is None


@pytest.mark.parametrize("state,procs,expect", [
    ("claimed", {}, "creating the workspace"),
    ("working", {}, "setup or baseline gate"),
    ("working", {"worker": {"age": 125}}, "worker session running (2m05s"),
    ("working", {"gate": {"age": 61, "kind": "stop-hook"}}, "Stop-hook test gate is running (1m01s"),
    ("testing", {"gate": {"age": 30, "kind": "dispatcher"}}, "dispatcher test gate running"),
    ("testing", {}, "queued behind another gate"),
    ("reviewing", {"reviewer": {"age": 20}}, "review (read-only reviewer), running 0m20s"),
    ("pr_open", {}, "waiting for a human"),
    ("done", {}, "merged"),
])
def test_phase_is_derived_from_state_and_processes(state, procs, expect):
    from dispatcher import detail

    assert expect in detail.derive_phase(state, {"worker": None, "reviewer": None, "gate": None, **procs})


def test_blocked_phase_says_how_to_resume():
    from dispatcher import detail

    none = {"worker": None, "reviewer": None, "gate": None}
    assert "drag the card back to Ready" in detail.derive_phase("blocked", none, resumable=True)
    assert "needs a decision" in detail.derive_phase("blocked", none, resumable=False)


def test_task_detail_end_to_end_with_a_session_still_held_open(tmp_path, repo):
    from dispatcher import detail

    cfg = _cfg(tmp_path, repo)
    led = Ledger(tmp_path / "p.db")
    led.upsert_claim(9, SLUG, SLUG)
    led.update(9, state="working", test_retries=0)
    led.add_cost(9, 2.0)                                   # recorded so far: one finished session
    logs = tmp_path / "logs"
    _write_log(logs / SLUG / "session.jsonl", [
        _ev_assistant("a", "Read", "/w/x.py"), _ev_result(2.0, 61, "error_max_turns"),
        _ev_assistant("b", "Edit", "/w/y.py"), _ev_assistant("c", text="finished"),
        _ev_result(5.0, 12),                                # ended, but not recorded in the ledger yet
        _ev_assistant("d", "Bash", "pytest -q"),             # a new pass in progress
    ])
    _change(repo)
    ws = repo / ".ntree" / SLUG
    ws.parent.mkdir(parents=True, exist_ok=True)
    ws.symlink_to(repo)                                    # the workspace is a git checkout
    table = _ps((1, 0, 900, "python claude-pipeline run"), (10, 1, 700, CLAUDE),
                (11, 10, 120, "sh -c ..."), (12, 11, 120, GATE))
    d = detail.task_detail(cfg, led, 9, logs, table=table, cwd_of=lambda pid: str(ws) if pid == 10 else "")
    assert d["phase"].startswith("worker finished its turns; its own Stop-hook test gate")
    assert d["unrecorded"] == pytest.approx(3.0) and d["accounted_cost"] == pytest.approx(2.0)
    assert d["session"]["turns"] == 1 and d["session"]["actions"][-1].startswith("Bash:")
    assert [p["outcome"] for p in d["past"]] == ["error_max_turns", "success"]
    assert len(d["workspace"]["commits"]) == 1
    text = detail.format_detail(d)
    for needle in ("Task #9", "WORKING", "Stop-hook test gate", "turn 1 of 60", "pytest -q",
                   "Spent      at least ~$5.", "Earlier sessions", "1 commit(s)"):
        assert needle in text, needle
    assert detail.format_detail(None) == "(no such task in the ledger)"
    assert detail.task_detail(cfg, led, 12345) is None


def test_finished_task_detail_has_no_live_section(tmp_path, repo):
    from dispatcher import detail

    led = Ledger(tmp_path / "p.db")
    led.upsert_claim(9, SLUG, SLUG); led.update(9, state="done", pr_url="https://gh/pr/7")
    d = detail.task_detail(_cfg(tmp_path, repo), led, 9, tmp_path / "logs", table=[])
    text = detail.format_detail(d)
    assert "DONE" in text and "https://gh/pr/7" in text and "merged and cleaned up" in text
    assert "Live cost" not in text and "Latest actions" not in text


def test_cli_task_flag_prints_the_detail(tmp_path, capsys, monkeypatch):
    from dispatcher import __main__ as cli

    led = Ledger(tmp_path / "p.db")
    led.upsert_claim(9, SLUG, SLUG); led.update(9, state="blocked", resumable=1)
    led.conn.close()
    assert cli.main(["--env", str(_cfg_env(tmp_path)), "dashboard", "--once", "--task", "9"]) == 0
    assert "drag the card back to Ready" in capsys.readouterr().out


def _cfg_env(tmp_path):
    _cfg(tmp_path)
    return tmp_path / ".env"


def test_tui_enter_opens_the_detail_screen_and_escape_closes_it(tmp_path):
    import asyncio

    pytest.importorskip("textual")
    from textual.widgets import Static

    from dispatcher.tui import DashboardApp, DetailScreen

    led = _mixed(tmp_path, db="p.db")
    led.conn.close()

    async def run():
        app = DashboardApp(_cfg(tmp_path), str(tmp_path / ".env"))
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause(0.3)
            await pilot.press("down", "down", "enter")             # third row = task 3
            await pilot.pause(0.4)
            opened = isinstance(app.screen, DetailScreen)
            body = str(app.screen.query_one("#detail-body", Static).render()) if opened else ""
            await pilot.press("escape")
            await pilot.pause(0.2)
            return opened, body, isinstance(app.screen, DetailScreen)
    opened, body, still_open = asyncio.run(run())
    assert opened and "Task #3" in body and "WORKING" in body and not still_open
