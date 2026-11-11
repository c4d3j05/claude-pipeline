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
