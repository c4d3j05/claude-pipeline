"""The dispatcher daemon: poll, claim, launch, gate, review, PR, write-back, caps."""

from __future__ import annotations

import fcntl
import os
import re
import signal
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from . import config as cfgmod
from . import pr as prmod
from .cleanup import process_closed_prs, watch_open_prs
from .config import Config
from .db import ACTIVE_STATES, Ledger
from .log import get
from .notify import Slack
from .ntree import Ntree, NtreeError
from .proc import kill_group
from .procenv import load_env_file
from .prompt import (
    FRESH_SESSION_NOTE,
    commit_or_discard_prompt,
    continue_prompt,
    retry_prompt_review,
    retry_prompt_tests,
    task_slug,
    worker_prompt,
)
from .review import GateTimeout, adversarial_review, has_work, run_tests, working_tree_dirty
from .stream import SessionResult, blocked_reason
from .vikunja import Vikunja
from .worker import launch

log = get("daemon")

CLEANUP_INTERVAL = 300  # poll open PRs every 5 min


@dataclass
class _Run:
    """Mutable per-task state threaded through the gate helpers."""
    result: SessionResult
    session_id: str


Stop = tuple[str, str]  # ("cancel" | "block", reason)

# Seconds to wait before retrying a session that failed for a transient reason.
TRANSIENT_BACKOFF_S = (30.0, 120.0)
_TRANSIENT = re.compile(
    r"overloaded|\b529\b|\b50[234]\b|rate.?limit|too many requests|econnreset|etimedout|"
    r"enotfound|eai_again|network|connection (?:reset|refused|error|closed)|temporar|try again",
    re.IGNORECASE)
# Credentials problems fail EVERY task identically: pause dispatching instead of draining
# the whole queue into Blocked.
_AUTH = re.compile(
    r"not logged in|please run /login|invalid api key|authentication|unauthori[sz]ed|"
    r"oauth token|credit balance|\b401\b", re.IGNORECASE)
TICK_FAILURES_BEFORE_ALERT = 3
# Blocks that leave a worker's progress in the workspace: a re-queue resumes instead of wiping.
_RESUMABLE = ("cap: turns", "cap: wall-clock", "cap: task cost")

POOL_CEILING = 32  # hard cap on threads; live concurrency is gated by cfg.max_workers


class Dispatcher:
    def __init__(self, cfg: Config, env_file: str = ".env"):
        self.cfg = cfg
        self.env_file = env_file
        self.vk = Vikunja(cfg.vikunja_url, cfg.vikunja_token)
        self.ledger = Ledger(cfg.db_path)
        self.slack = Slack(cfg.slack_webhook)
        self.ntree = Ntree(cfg.repo_path, cfg.base_branch)
        # Pool has a fixed ceiling; the tick gate limits real concurrency to
        # cfg.max_workers, so changing MAX_WORKERS live (hot-reload) takes effect.
        self.pool = ThreadPoolExecutor(max_workers=POOL_CEILING, thread_name_prefix="worker")
        self.logs_root = Path("logs")
        self.view_id = 0
        self.buckets: dict[str, dict] = {}
        self._stop = threading.Event()
        self._last_cleanup = 0.0
        self._label_id = 0
        self._gate_sem = threading.BoundedSemaphore(cfg.max_gates)
        self._baseline_lock = threading.Lock()
        self._tick_failures = 0

    # --- bucket / label resolution --------------------------------------
    def refresh_board(self) -> None:
        self.view_id = self.vk.kanban_view_id(self.cfg.project_id)
        bmap = self.vk.bucket_map(self.cfg.project_id, self.view_id)
        resolved = {}
        for key, title in self.cfg.bucket_names.items():
            b = bmap.get(title.strip().lower())
            if not b:
                raise NtreeError(  # reuse as a generic startup error
                    f"bucket '{title}' not found in project {self.cfg.project_id}; "
                    f"available: {sorted(bmap)}"
                )
            resolved[key] = b
        self.buckets = resolved
        self._label_id = self.vk.ensure_label(self.cfg.claude_label)

    def move(self, task_id: int, bucket_key: str) -> None:
        b = self.buckets[bucket_key]
        self.vk.move_task(self.cfg.project_id, self.view_id, b["id"], task_id)

    def comment(self, task_id: int, text: str) -> None:
        try:
            self.vk.comment(task_id, f"<p>{text}</p>")
        except Exception as e:  # noqa: BLE001
            log.warning("comment on task %s failed: %s", task_id, e)

    def task_link(self, task_id: int) -> str:
        root = self.cfg.vikunja_url.rsplit("/api/", 1)[0]
        return f"{root}/tasks/{task_id}"

    # --- main loop -------------------------------------------------------
    def run(self) -> None:
        self._lock_fh = _acquire_instance_lock(self.cfg.db_path.with_suffix(".lock"))
        self.refresh_board()
        self.ntree.clear_stale_git_locks()   # leftovers from a previous crash/kill
        self.reap_orphans()
        log.info(
            "dispatcher up: project=%s view=%s max_workers=%s repo=%s",
            self.cfg.project_id, self.view_id, self.cfg.max_workers, self.cfg.repo_path,
        )
        signal.signal(signal.SIGTERM, self._on_signal)
        signal.signal(signal.SIGINT, self._on_signal)

        while not self._stop.is_set():
            self._guarded_tick()
            self._stop.wait(self.cfg.poll_interval)

        log.info("shutting down; waiting for %d running task(s) to finish…",
                 self.ledger.active_count())
        self.pool.shutdown(wait=True)
        self.ledger.close()

    def _guarded_tick(self) -> None:
        """One poll; repeated failures (expired Vikunja token, API down) alert the human."""
        try:
            self.tick()
        except Exception as e:
            self._tick_failures += 1
            log.exception("tick failed (%d in a row)", self._tick_failures)
            if self._tick_failures == TICK_FAILURES_BEFORE_ALERT:
                self.slack.alert(f"dispatcher poll has failed {self._tick_failures}x in a row: {e}")
            return
        if self._tick_failures >= TICK_FAILURES_BEFORE_ALERT:
            self.slack.alert("dispatcher poll recovered")
        self._tick_failures = 0

    def _on_signal(self, signum, _frame) -> None:
        """First signal drains (finish running tasks); a second one stops workers and exits."""
        if self._stop.is_set():
            self._force_stop()
        log.info("signal %s: draining running tasks (send again to stop them and exit)", signum)
        self._stop.set()

    def _force_stop(self) -> None:
        log.warning("second signal: stopping workers and exiting now")
        for row in self.ledger.active():
            if row["pid"] and _is_claude_proc(row["pid"]):
                kill_group(row["pid"], grace=5)
        os._exit(1)  # ledger rows stay active; the next start reaps/blocks them

    def _reload_config(self) -> None:
        """Hot-reload the operational knobs from .env (dashboard edits them live).

        Structural fields (Vikunja URL/token/project, DB path, repo) keep their
        startup values; only the HOT_KEYS take effect without a restart.
        """
        try:
            self.cfg = cfgmod.load(self.env_file)
        except Exception:  # noqa: BLE001
            log.warning("config reload failed; keeping current config")

    def _honor_cancels(self) -> None:
        for row in self.ledger.pending_cancels():
            pid = row["pid"]
            if pid and _is_claude_proc(pid):
                log.warning("cancel requested for task %s — killing worker pid %s",
                            row["task_id"], pid)
                kill_group(pid)
            # run_pipeline will see cancel_requested after the worker dies and finalize.

    def tick(self) -> None:
        self._reload_config()
        self._honor_cancels()

        now = time.monotonic()
        if now - self._last_cleanup >= CLEANUP_INTERVAL:
            self._last_cleanup = now
            self.refresh_board()
            process_closed_prs(self.cfg, self.ledger, self.ntree, self.vk, self.buckets, self.slack)
            watch_open_prs(self.cfg, self.ledger, self.vk, self.slack)

        # Operator pause (from the dashboard): keep running workers, stop claiming.
        if self.ledger.is_paused():
            return

        # Budget: stop claiming when the daily cap is reached.
        spent = self.ledger.today_cost()
        if spent >= self.cfg.daily_cap_usd:
            if not self.ledger.cap_already_notified():
                self.slack.daily_cap(spent, self.cfg.daily_cap_usd)
                self.ledger.mark_cap_notified()
                log.warning("daily cap reached (%.2f/%.2f) — pausing claims", spent, self.cfg.daily_cap_usd)
            return

        free = self.cfg.max_workers - self.ledger.active_count()
        if free <= 0:
            return

        for task in self.ready_tasks(limit=free):
            try:
                self.claim_and_dispatch(task)
            except Exception:
                log.exception("could not claim task %s", task.get("id"))

    def ready_tasks(self, limit: int) -> list[dict]:
        """Tasks in the ready bucket carrying the claude label, not already claimed."""
        board = self.vk.board(self.cfg.project_id, self.view_id)
        ready = next(
            (b for b in board if b["title"].strip().lower() == self.cfg.bucket_ready.strip().lower()),
            None,
        )
        if not ready:
            return []
        out: list[dict] = []
        for t in self.vk.tasks_in_bucket(ready):
            if len(out) >= limit:
                break
            if t.get("done"):
                continue
            if self.cfg.claude_label.lower() not in self.vk.task_labels(t):
                continue
            row = self.ledger.get(t["id"])
            if row is not None:
                # In flight, PR open, or finished: never re-claim. A *blocked* row sitting
                # in Ready means a human re-queued it — that is a deliberate retry.
                if row["state"] in (*ACTIVE_STATES, "pr_open", "done"):
                    continue
            elif self.vk.has_comment_prefix(t["id"], "dispatched:"):
                continue  # claimed by a run whose ledger row is gone; don't double-claim
            out.append(t)
        return out

    def claim_and_dispatch(self, task: dict) -> None:
        tid = task["id"]
        title = task.get("title", f"task-{tid}")
        slug = task_slug(tid, title)
        # Claim: move + marker comment (guards against re-claim).
        self.move(tid, "in_progress")
        self.comment(tid, f"dispatched: session=pending pid={os.getpid()} workspace={slug}")
        self.ledger.upsert_claim(tid, slug, slug)
        log.info("claimed task %s -> %s", tid, slug)
        self.pool.submit(self._safe_pipeline, task, slug)

    # --- per-task pipeline ----------------------------------------------
    def _safe_pipeline(self, task: dict, slug: str) -> None:
        tid = task["id"]
        try:
            self.run_pipeline(task, slug)
        except Exception as e:
            log.exception("pipeline crashed for task %s", tid)
            self._block(tid, f"pipeline error: {e}")

    def _session_log(self, slug: str, name: str) -> Path:
        return self.logs_root / slug / name

    def _account_cost(self, tid: int, result) -> None:
        self.ledger.update(tid, session_id=result.session_id or None)
        self.ledger.add_cost(tid, result.cost_usd)

    def _launch(self, tid, worktree, slug, prompt, logname, resume_session=""):
        """Run one worker session, account its cost, always clear the stored PID.

        A transient failure (API overloaded, network blip, rate limit) is retried with
        backoff instead of blocking the task; every attempt's cost is accounted.
        """
        attempts = 1 + max(0, self.cfg.transient_retries)
        for attempt in range(attempts):
            try:
                result, rc = launch(
                    worktree=worktree,
                    prompt=prompt,
                    settings_file=self.cfg.settings_file,
                    session_log=self._session_log(slug, logname),
                    max_turns=self.cfg.max_turns,
                    wall_clock_limit_min=self.cfg.wall_clock_limit_min,
                    model=self.cfg.worker_model,
                    permission_mode=self.cfg.permission_mode,
                    resume_session=resume_session,
                    on_pid=lambda p: self.ledger.update(tid, pid=p),
                    extra_env=workspace_env(worktree, slug),
                )
            finally:
                self.ledger.update(tid, pid=None)  # a stale PID could be reused later
            self._account_cost(tid, result)
            if attempt + 1 >= attempts or not self._is_transient(result, rc):
                break
            if self.ledger.cancel_requested(tid):
                break
            delay = TRANSIENT_BACKOFF_S[min(attempt, len(TRANSIENT_BACKOFF_S) - 1)]
            log.warning("transient failure for task %s (rc=%s); retry %d/%d in %.0fs",
                        tid, rc, attempt + 1, attempts - 1, delay)
            self.comment(tid, f"transient error, retrying in {delay:.0f}s "
                              f"({attempt + 1}/{attempts - 1})")
            if self._stop.wait(delay):
                break
        return result, rc

    def _is_transient(self, result, rc: int) -> bool:
        if rc == 124 or result.num_turns >= self.cfg.max_turns:
            return False
        if rc == 0 and not result.is_error:
            return False
        text = f"{result.result_text} {result.last_message}"
        return bool(_TRANSIENT.search(text)) and not _AUTH.search(text)

    def _cap_hit(self, result, rc: int) -> str | None:
        if rc == 124:
            return f"cap: wall-clock {self.cfg.wall_clock_limit_min}m"
        if result.num_turns >= self.cfg.max_turns:
            return f"cap: turns={self.cfg.max_turns}"
        if len(result.permission_denials) >= 3:
            return f"permission denials (x{len(result.permission_denials)}): {result.permission_denials[-1]}"
        if rc != 0 or result.is_error:
            # Auth expired, API overloaded, crash… Don't feed a dead session to the gates.
            detail = (result.result_text or result.last_message or "no output").strip()
            return f"worker error (rc={rc}): {detail[:200]}"
        return None

    def _over_task_budget(self, tid: int) -> str | None:
        cap = self.cfg.task_cap_usd
        row = self.ledger.get(tid)
        if cap and row and row["cost_usd"] >= cap:
            return f"cap: task cost ${row['cost_usd']:.2f} >= ${cap:.2f}"
        return None

    def _stop_reason(self, tid: int, result, rc: int) -> Stop | None:
        """Why the task must stop after a worker session, or None to carry on."""
        if self.ledger.cancel_requested(tid):
            return ("cancel", "")
        reason = blocked_reason(result)
        if reason:
            return ("block", reason)
        cap = self._cap_hit(result, rc) or self._over_task_budget(tid)
        if cap and cap.startswith("worker error") and _AUTH.search(cap):
            self.ledger.set_paused(True)
            self.slack.alert(f"credentials problem ({cap[:120]}); dispatching is PAUSED — "
                             "fix the login, then resume from the dashboard")
            cap += " — dispatching paused until credentials are fixed"
        return ("block", cap) if cap else None

    def _halt(self, tid: int, slug: str, stop: Stop) -> None:
        kind, reason = stop
        if kind == "cancel":
            return self._cancelled(tid, slug)
        return self._block(tid, reason)

    def _resume_step(self, tid, worktree, slug, run: _Run, prompt: str) -> Stop | None:
        result, rc = self._launch(tid, worktree, slug, prompt, "session.jsonl", run.session_id)
        run.result = result
        run.session_id = result.session_id or run.session_id  # resume may fork a new id
        return self._stop_reason(tid, result, rc)

    def _gate(self, slug: str) -> tuple[bool, str]:
        """Run the test gate, at most MAX_GATES at a time (the queue wait is not timed)."""
        with self._gate_sem:
            return run_tests(self.cfg, self.ntree, slug)

    def _verify(self, tid, worktree, slug, run: _Run) -> Stop | None:
        try:
            return self._verify_inner(tid, worktree, slug, run)
        except GateTimeout as e:
            return ("block", f"gate: {e}")

    def _baseline(self, tid, worktree, slug, base_ref: str) -> Stop | None:
        """Run the gate on the untouched base once per commit; red base = environment problem.

        Without this a broken environment sends a paid worker off to "fix" unrelated
        failures. The result is cached per base SHA, so only the first task pays the time.
        """
        if not self.cfg.baseline_check:
            return None
        sha = self.ntree.rev(base_ref) or base_ref
        try:
            with self._baseline_lock:  # single-flight: concurrent tasks reuse the result
                row = self.ledger.get_baseline(sha)
                if row is None:
                    green, tail = self._gate(slug)
                    self.ledger.set_baseline(sha, green, tail)
                else:
                    green, tail = bool(row["green"]), row["tail"]
        except GateTimeout as e:
            return ("block", f"baseline: {e}")
        if green:
            return None
        return ("block", (f"baseline red: tests already fail on {base_ref} ({sha[:9]}) before any "
                          f"change — fix the base branch or environment, then re-queue\n"
                          f"{tail[-600:]}"))

    def _verify_inner(self, tid, worktree, slug, run: _Run) -> Stop | None:
        """GATE 1: tests green AND working tree clean, re-checked after every worker turn.

        Pushing only sends commits, so a green run on a dirty tree proves nothing about
        what the PR will contain; a discard can likewise invalidate an earlier green.
        """
        cfg = self.cfg
        self.ledger.update(tid, state="testing")
        settled = False
        while True:
            if self.ledger.cancel_requested(tid):
                return ("cancel", "")
            green, tail = self._gate(slug)
            while not green:
                row = self.ledger.get(tid)
                if row["test_retries"] >= cfg.max_test_retries:
                    return ("block", "gate: tests red")
                n = row["test_retries"] + 1
                self.ledger.update(tid, test_retries=n)
                self.comment(tid, f"gate: tests red, retry {n}/{cfg.max_test_retries}")
                stop = self._resume_step(tid, worktree, slug, run, retry_prompt_tests(tail))
                if stop:
                    return stop
                if self.ledger.cancel_requested(tid):
                    return ("cancel", "")
                green, tail = self._gate(slug)
            if not working_tree_dirty(worktree):
                return None
            if settled:
                return ("block", "gate: uncommitted changes after commit-or-discard turn")
            settled = True
            stop = self._resume_step(tid, worktree, slug, run, commit_or_discard_prompt())
            if stop:
                return stop

    def _review(self, tid, worktree, slug, spec: str, base_ref: str) -> dict:
        review = adversarial_review(
            self.cfg, worktree, spec, self._session_log(slug, "review.jsonl"), base_ref)
        self.ledger.add_cost(tid, review.get("cost_usd", 0.0))  # review spend counts too
        return review

    def _resume_workspace(self, tid: int, slug: str, base_ref: str) -> Path | None:
        """The existing workspace if this task was blocked mid-work and can be resumed."""
        row = self.ledger.get(tid)
        if not row or not row["resumable"] or not row["session_id"]:
            return None
        try:
            wt = self.ntree.path(slug)
        except NtreeError:
            return None
        if not wt.exists() or not has_work(wt, base_ref):
            return None
        return wt

    def _worth_continuing(self, tid: int, worktree: Path, base_ref: str) -> bool:
        """Resume a turn-capped worker only if it produced something and budget remains."""
        return (not self.ledger.cancel_requested(tid)
                and self._over_task_budget(tid) is None
                and has_work(worktree, base_ref))

    def run_pipeline(self, task: dict, slug: str) -> None:
        tid = task["id"]
        title = task.get("title", f"task-{tid}")
        desc = task.get("description", "") or ""
        cfg = self.cfg

        base_ref = self.ntree.base_ref()   # fetched, so workers never fork from a stale main
        resume_wt = self._resume_workspace(tid, slug, base_ref)
        if resume_wt:
            # Blocked on a cap with work in progress: keep the workspace and its session.
            worktree = resume_wt
            log.info("task %s: resuming the existing workspace and session", tid)
            self.comment(tid, "resuming the previous session in the existing workspace "
                              "(no restart from scratch)")
        else:
            # Start pristine. A leftover worktree AND its branch from a prior run would
            # otherwise be reused stale (ntree new is a no-op if the workspace exists).
            self.ntree.rm(slug)
            self.ntree.remove_branch(slug)
            worktree = self.ntree.new(slug, base_ref)
        self.ledger.update(tid, state="working", workspace=slug, resumable=0)

        # A cancel requested during setup (minutes of installs) must not cost a worker run.
        if self.ledger.cancel_requested(tid):
            return self._cancelled(tid, slug)
        if not resume_wt:  # a resumed workspace is not pristine; the base was checked before
            stop = self._baseline(tid, worktree, slug, base_ref)
            if stop:
                return self._halt(tid, slug, stop)
        if self.ledger.cancel_requested(tid):
            return self._cancelled(tid, slug)

        # First worker attempt (or resume of the interrupted one)
        task_prompt = worker_prompt(tid, title, desc, slug)
        if resume_wt:
            sid = self.ledger.get(tid)["session_id"]
            result, rc = self._launch(tid, worktree, slug, continue_prompt(), "session.jsonl", sid)
            if rc != 0 and "no conversation found" in f"{result.result_text} {result.last_message}".lower():
                log.warning("task %s: session %s cannot be resumed; fresh session on the same files",
                            tid, sid)
                result, rc = self._launch(tid, worktree, slug, task_prompt + FRESH_SESSION_NOTE,
                                          "session.jsonl")
        else:
            result, rc = self._launch(tid, worktree, slug, task_prompt, "session.jsonl")
        self.comment(tid, f"dispatched: session={result.session_id} pid={os.getpid()} workspace={slug}")
        run = _Run(result=result, session_id=result.session_id)
        stop = self._stop_reason(tid, result, rc)
        # Out of turns but making progress: resume the same session (bounded) instead of
        # discarding the work. Stuck workers (no changes) and over-budget tasks still block.
        continuations = 0
        while (stop and stop[0] == "block" and stop[1].startswith("cap: turns")
               and continuations < cfg.max_continuations
               and self._worth_continuing(tid, worktree, base_ref)):
            continuations += 1
            self.comment(tid, f"turn limit reached with work in progress; resuming the same "
                              f"session ({continuations}/{cfg.max_continuations})")
            stop = self._resume_step(tid, worktree, slug, run, continue_prompt())
        if stop:
            return self._halt(tid, slug, stop)

        # GATE 1: tests + clean tree (the dispatcher runs them; it does not trust the worker)
        stop = self._verify(tid, worktree, slug, run)
        if stop:
            return self._halt(tid, slug, stop)

        # GATE 2: adversarial review
        spec = f"{title}\n\n{desc}"
        if self.ledger.cancel_requested(tid):  # cancelled during the test gate
            return self._cancelled(tid, slug)
        self.ledger.update(tid, state="reviewing")
        review = self._review(tid, worktree, slug, spec, base_ref)
        while True:
            if review.get("error"):  # the reviewer itself failed — not the worker's fault
                return self._block(tid, "review error: reviewer session failed")
            over = self._over_task_budget(tid)
            if over:
                return self._block(tid, over)
            if review.get("verdict") != "block":
                break
            row = self.ledger.get(tid)
            if row["review_retries"] >= cfg.max_review_retries:
                self.comment(tid, f"gate: review blocking\n{_fmt_findings(review.get('findings', []))}")
                return self._block(tid, "gate: review blocking")
            n = row["review_retries"] + 1
            self.ledger.update(tid, review_retries=n)
            self.comment(tid, "gate: review blocking, retry")
            stop = self._resume_step(
                tid, worktree, slug, run, retry_prompt_review(review.get("findings", [])))
            if not stop:
                stop = self._verify(tid, worktree, slug, run)  # tests + clean tree again
            if stop:
                return self._halt(tid, slug, stop)
            self.ledger.update(tid, state="reviewing")
            review = self._review(tid, worktree, slug, spec, base_ref)

        if self.ledger.cancel_requested(tid):  # cancelled while the review was running
            return self._cancelled(tid, slug)

        # PASS -> open the PR
        row = self.ledger.get(tid)
        needs_qa = "manual qa" in desc.lower()
        body = prmod.build_body(
            cfg=cfg, task_id=tid, task_title=title, description_html=desc,
            worker_summary=run.result.result_text or run.result.last_message,
            review=review, session_id=run.session_id, cost_usd=row["cost_usd"],
        )
        pr_url = prmod.push_and_create(
            cfg=cfg, worktree=worktree, branch=slug,
            title=title, body=body, needs_qa=needs_qa,
        )
        self.move(tid, "review")
        self.comment(tid, f"pr: {pr_url}")
        self.ledger.update(tid, state="pr_open", pr_url=pr_url)
        self.slack.pr_opened(tid, pr_url)
        log.info("task %s -> PR %s", tid, pr_url)

    def _cancelled(self, task_id: int, slug: str) -> None:
        log.warning("task %s cancelled by operator", task_id)
        self.ledger.clear_cancel(task_id)
        _safe_call(lambda: self.ntree.rm(slug))
        _safe_call(lambda: self.ntree.remove_branch(slug))
        try:
            self.move(task_id, "blocked")
        except Exception as e:  # noqa: BLE001
            log.warning("could not move cancelled task %s to blocked: %s", task_id, e)
        self.comment(task_id, "blocked: cancelled by operator")
        self.ledger.update(task_id, state="blocked")
        self.slack.task_blocked(task_id, "cancelled by operator", self.task_link(task_id))

    def _block(self, task_id: int, reason: str) -> None:
        log.warning("task %s blocked: %s", task_id, reason)
        if reason.startswith(_RESUMABLE):
            self.ledger.update(task_id, resumable=1)
        try:
            self.move(task_id, "blocked")
        except Exception as e:  # noqa: BLE001
            log.warning("could not move task %s to blocked: %s", task_id, e)
        self.comment(task_id, f"blocked: {reason}")
        self.ledger.update(task_id, state="blocked")
        self.slack.task_blocked(task_id, reason, self.task_link(task_id))

    # --- restart recovery ------------------------------------------------
    def reap_orphans(self) -> None:
        """A dispatcher restart reaps workers it no longer owns and unsticks their cards."""
        for row in self.ledger.active():
            tid, pid = row["task_id"], row["pid"]
            # Verify it's still a claude process: a recycled PID must never be killed.
            if pid and _is_claude_proc(pid):
                log.info("reaping orphan pid %s from task %s", pid, tid)
                kill_group(pid)
            self.ledger.update(tid, state="blocked", pid=None)
            try:
                self.move(tid, "blocked")  # otherwise the card sits in In Progress forever
            except Exception as e:  # noqa: BLE001
                log.warning("could not move interrupted task %s to blocked: %s", tid, e)
            self.comment(tid, "blocked: dispatcher restarted; worker interrupted — "
                              "move this card back to Ready to retry")


def workspace_env(worktree: Path, slug: str) -> dict[str, str]:
    """The per-workspace environment `ntree` wrote (NTREE_WORKSPACE, PORT, API_PORT, …).

    The dispatcher launches `claude` directly, not through `ntree run`, so without this the
    worker's Stop hook (`ntree run "$NTREE_WORKSPACE" -- …`) fails at once and the worker has
    no per-workspace ports or database name. NTREE_WORKSPACE falls back to the slug.
    """
    return {"NTREE_WORKSPACE": slug, **load_env_file(worktree / ".env.workspace")}


def _fmt_findings(findings: list[dict]) -> str:
    return "\n".join(
        f"- [{f.get('severity','?')}] {f.get('file','?')}:{f.get('line','?')} — {f.get('note','')}"
        for f in findings
    ) or "(no details)"


def _safe_call(fn) -> None:
    try:
        fn()
    except Exception as e:  # noqa: BLE001
        log.warning("teardown step failed: %s", e)


def _acquire_instance_lock(path: Path):
    """Exclusive lock so two dispatchers can't double-claim the same board."""
    fh = open(path, "w")  # noqa: SIM115 — must stay open for the process lifetime
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        raise SystemExit(f"another dispatcher already holds {path}; refusing to start") from None
    fh.write(str(os.getpid()))
    fh.flush()
    return fh


def _is_claude_proc(pid: int) -> bool:
    """True only if `pid` is alive AND is a claude process (guards against PID reuse)."""
    try:
        out = subprocess.run(["ps", "-p", str(pid), "-o", "args="],
                             capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    return "claude" in out
