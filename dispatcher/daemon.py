"""The dispatcher daemon: poll, claim, launch, gate, review, PR, write-back, caps."""

from __future__ import annotations

import os
import signal
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import config as cfgmod
from . import pr as prmod
from .cleanup import process_closed_prs
from .config import Config
from .db import Ledger
from .log import get
from .notify import Slack
from .ntree import Ntree, NtreeError
from .prompt import (
    commit_or_discard_prompt,
    retry_prompt_review,
    retry_prompt_tests,
    task_slug,
    worker_prompt,
)
from .review import adversarial_review, run_tests, working_tree_dirty
from .stream import blocked_reason
from .vikunja import Vikunja
from .worker import launch

log = get("daemon")

CLEANUP_INTERVAL = 300  # poll open PRs every 5 min


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
        self.refresh_board()
        self.reap_orphans()
        log.info(
            "dispatcher up: project=%s view=%s max_workers=%s repo=%s",
            self.cfg.project_id, self.view_id, self.cfg.max_workers, self.cfg.repo_path,
        )
        signal.signal(signal.SIGTERM, lambda *_: self._stop.set())
        signal.signal(signal.SIGINT, lambda *_: self._stop.set())

        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:
                log.exception("tick failed")
            self._stop.wait(self.cfg.poll_interval)

        log.info("shutting down; waiting for workers…")
        self.pool.shutdown(wait=True)
        self.ledger.close()

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
            if pid and _pid_alive(pid) and pid != os.getpid():
                log.warning("cancel requested for task %s — killing worker pid %s",
                            row["task_id"], pid)
                _kill_pgid(pid)
            # run_pipeline will see cancel_requested after the worker dies and finalize.

    def tick(self) -> None:
        self._reload_config()
        self._honor_cancels()

        now = time.monotonic()
        if now - self._last_cleanup >= CLEANUP_INTERVAL:
            self._last_cleanup = now
            self.refresh_board()
            process_closed_prs(self.cfg, self.ledger, self.ntree, self.vk, self.buckets, self.slack)

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
            self.claim_and_dispatch(task)

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
            if self.ledger.is_tracked_active(t["id"]):
                continue
            if self.vk.has_comment_prefix(t["id"], "dispatched:"):
                continue
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

    def _cap_hit(self, result, rc: int) -> str | None:
        if rc == 124:
            return f"cap: wall-clock {self.cfg.wall_clock_limit_min}m"
        if result.num_turns >= self.cfg.max_turns:
            return f"cap: turns={self.cfg.max_turns}"
        if len(result.permission_denials) >= 3:
            return f"permission denials (x{len(result.permission_denials)}): {result.permission_denials[-1]}"
        return None

    def run_pipeline(self, task: dict, slug: str) -> None:
        tid = task["id"]
        title = task.get("title", f"task-{tid}")
        desc = task.get("description", "") or ""
        cfg = self.cfg

        # Workspace: start pristine. A leftover worktree AND its branch from a
        # prior/blocked run would otherwise be reused stale (ntree new is a no-op
        # if the workspace exists, and reuses the old branch off a stale base).
        self.ntree.rm(slug)
        self.ntree.remove_branch(slug)
        worktree = self.ntree.new(slug)
        self.ledger.update(tid, state="working", workspace=slug, pid=os.getpid())

        # First worker attempt
        prompt = worker_prompt(tid, title, desc, slug)
        result, rc = launch(
            worktree=worktree,
            prompt=prompt,
            settings_file=cfg.settings_file,
            session_log=self._session_log(slug, "session.jsonl"),
            max_turns=cfg.max_turns,
            wall_clock_limit_min=cfg.wall_clock_limit_min,
            model=cfg.worker_model,
            permission_mode=cfg.permission_mode,
            on_pid=lambda p: self.ledger.update(tid, pid=p),
        )
        self._account_cost(tid, result)
        self.comment(tid, f"dispatched: session={result.session_id} pid={os.getpid()} workspace={slug}")
        session_id = result.session_id

        if self.ledger.cancel_requested(tid):
            return self._cancelled(tid, slug)
        reason = blocked_reason(result)
        if reason:
            return self._block(tid, reason)
        cap = self._cap_hit(result, rc)
        if cap:
            return self._block(tid, cap)

        # GATE 1: tests (the dispatcher runs them; it does not trust the worker)
        self.ledger.update(tid, state="testing")
        green, tail = run_tests(cfg, self.ntree, slug)
        while not green:
            row = self.ledger.get(tid)
            if row["test_retries"] >= cfg.max_test_retries:
                return self._block(tid, "gate: tests red")
            n = row["test_retries"] + 1
            self.ledger.update(tid, test_retries=n)
            self.comment(tid, f"gate: tests red, retry {n}/{cfg.max_test_retries}")
            result, rc = self._resume(tid, worktree, slug, session_id, retry_prompt_tests(tail), "session.jsonl")
            self._account_cost(tid, result)
            if self.ledger.cancel_requested(tid):
                return self._cancelled(tid, slug)
            cap = self._cap_hit(result, rc)
            if cap:
                return self._block(tid, cap)
            green, tail = run_tests(cfg, self.ntree, slug)

        # Working-tree check: uncommitted changes -> one more turn.
        if working_tree_dirty(worktree):
            result, rc = self._resume(tid, worktree, slug, session_id, commit_or_discard_prompt(), "session.jsonl")
            self._account_cost(tid, result)
            if working_tree_dirty(worktree):
                return self._block(tid, "gate: uncommitted changes after commit-or-discard turn")

        # GATE 2: adversarial review
        self.ledger.update(tid, state="reviewing")
        spec = f"{title}\n\n{desc}"
        review = adversarial_review(cfg, worktree, spec, self._session_log(slug, "review.jsonl"))
        while review.get("verdict") == "block":
            row = self.ledger.get(tid)
            if row["review_retries"] >= cfg.max_review_retries:
                findings = _fmt_findings(review.get("findings", []))
                self.comment(tid, f"gate: review blocking\n{findings}")
                return self._block(tid, "gate: review blocking")
            n = row["review_retries"] + 1
            self.ledger.update(tid, review_retries=n)
            self.comment(tid, "gate: review blocking, retry")
            result, rc = self._resume(
                tid, worktree, slug, session_id, retry_prompt_review(review.get("findings", [])), "session.jsonl"
            )
            self._account_cost(tid, result)
            if self.ledger.cancel_requested(tid):
                return self._cancelled(tid, slug)
            cap = self._cap_hit(result, rc)
            if cap:
                return self._block(tid, cap)
            green, tail = run_tests(cfg, self.ntree, slug)
            if not green:
                return self._block(tid, "gate: tests red after review retry")
            review = adversarial_review(cfg, worktree, spec, self._session_log(slug, "review.jsonl"))

        # PASS -> open the PR
        row = self.ledger.get(tid)
        needs_qa = "manual qa" in desc.lower()
        body = prmod.build_body(
            cfg=cfg, task_id=tid, task_title=title, description_html=desc,
            worker_summary=result.result_text or result.last_message,
            review=review, session_id=session_id, cost_usd=row["cost_usd"],
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

    def _resume(self, tid, worktree, slug, session_id, prompt, logname):
        return launch(
            worktree=worktree,
            prompt=prompt,
            settings_file=self.cfg.settings_file,
            session_log=self._session_log(slug, logname),
            max_turns=self.cfg.max_turns,
            wall_clock_limit_min=self.cfg.wall_clock_limit_min,
            model=self.cfg.worker_model,
            permission_mode=self.cfg.permission_mode,
            resume_session=session_id,
            on_pid=lambda p: self.ledger.update(tid, pid=p),
        )

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
        try:
            self.move(task_id, "blocked")
        except Exception as e:  # noqa: BLE001
            log.warning("could not move task %s to blocked: %s", task_id, e)
        self.comment(task_id, f"blocked: {reason}")
        self.ledger.update(task_id, state="blocked")
        self.slack.task_blocked(task_id, reason, self.task_link(task_id))

    # --- restart recovery ------------------------------------------------
    def reap_orphans(self) -> None:
        """A dispatcher restart reaps orphaned workers by PID (spec)."""
        for row in self.ledger.active():
            pid = row["pid"]
            if pid and _pid_alive(pid) and pid != os.getpid():
                log.info("reaping orphan pid %s from task %s", pid, row["task_id"])
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            # Re-queue: mark blocked so a human/next run re-triages (queue truth is Vikunja).
            self.ledger.update(row["task_id"], state="blocked")
            self.comment(row["task_id"], "dispatcher restarted; worker interrupted — re-triage")


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


def _kill_pgid(pid: int) -> None:
    try:
        os.killpg(os.getpgid(pid), signal.SIGTERM)
        time.sleep(2)
        os.killpg(os.getpgid(pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, owned by someone else
    except OSError:
        return False
    return True
