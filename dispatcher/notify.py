"""Slack notifications. A single channel, messages only on the events the spec lists."""

from __future__ import annotations

import json
from collections.abc import Callable

import requests

from . import slackthread
from .log import get

log = get("notify")


def _money(x: float) -> str:
    return f"${x:.2f}"


RESUME_HINT = ("The work is saved in the workspace. Drag the card back to Ready (or press r in the "
               "dashboard) to resume the same session with a fresh budget.")
RESTART_HINT = ("Drag the card back to Ready (or press r in the dashboard) to retry from scratch; "
                "the workspace is kept for inspection until the nightly cleanup.")


def next_step(reason: str, resumable: bool) -> str:
    """What the human should do about a blocked task (used in Slack and on the card)."""
    if reason.startswith("review error"):
        return ("The tests passed; only the automated review failed. Drag the card back to Ready "
                "(or press r in the dashboard): the work is saved and only the review re-runs.")
    if reason.startswith("environment"):
        return ("The code is not the problem: a service the tests need is down. Start it, then "
                "drag the card back to Ready (or press r in the dashboard). The work is saved "
                "and only the test gates are re-run.")
    if resumable:
        return RESUME_HINT
    if reason.startswith("baseline"):
        return ("The base branch already fails its tests, so no work was started. Fix the base "
                "branch (or the environment), then drag the card back to Ready.")
    if "credentials" in reason:
        return ("Fix the login (~/.worker.env), resume dispatching (p in the dashboard), then "
                "drag the card back to Ready.")
    return RESTART_HINT


class Slack:
    """Messages go to the webhook channel. The events of a task that claude-planner filed (PR
    opened, blocked, merged, PR needs attention) go to that task's planner thread instead, using
    the planner bot's token; with no thread, no token, or a failed post they fall back to the
    webhook, so a notification is never lost and Slack can never fail a task."""

    def __init__(self, webhook_url: str, bot_token: str = "",
                 thread_for: Callable[[int], tuple[str, str] | None] | None = None,
                 http_post=requests.post):
        self.url = webhook_url
        self.bot_token = bot_token
        self._thread_for = thread_for
        self._http_post = http_post

    def post_for_task(self, task_id: int, text: str) -> None:
        if self.bot_token and self._thread_for:
            try:
                found = self._thread_for(task_id)
            except Exception as e:  # noqa: BLE001 - a ledger problem must not stop a notification
                log.warning("could not look up the thread of task %s: %s", task_id, e)
                found = None
            if found and slackthread.post(self.bot_token, slackthread.Thread(*found), text,
                                          http_post=self._http_post):
                return
            if found:
                log.warning("task %s: thread post failed, using the webhook", task_id)
        self.post(text)

    def post(self, text: str) -> None:
        if not self.url:
            log.info("[slack disabled] %s", text)
            return
        try:
            requests.post(self.url, data=json.dumps({"text": text}),
                          headers={"Content-Type": "application/json"}, timeout=15)
        except requests.RequestException as e:
            log.warning("slack post failed: %s", e)

    def alert(self, text: str) -> None:
        self.post(f":rotating_light: claude-pipeline: {text}")

    def task_blocked(self, task_id: int, reason: str, link: str, *, slug: str = "",
                     cost: float | None = None, lifetime: float | None = None,
                     resumable: bool = False) -> None:
        lines = [f":no_entry: Task {task_id} *blocked*" + (f" ({slug})" if slug else ""),
                 f"*Why:* {reason}"]
        if cost is not None:
            spent = f"*Spent:* {_money(cost)} this attempt"
            if lifetime is not None and abs(lifetime - cost) > 0.005:
                spent += f" ({_money(lifetime)} over the task's life)"
            lines.append(spent)
        lines += [f"*Next:* {next_step(reason, resumable)}", link]
        self.post_for_task(task_id, "\n".join(lines))

    def task_merged(self, task_id: int, pr_url: str, cost: float | None = None,
                    lifetime: float | None = None) -> None:
        lines = [f":white_check_mark: Task {task_id} *merged*: {pr_url}"]
        if cost is not None:
            spent = f"Cost: {_money(cost)} this attempt"
            if lifetime is not None and abs(lifetime - cost) > 0.005:
                spent += f" ({_money(lifetime)} over the task's life)"
            lines.append(spent + ". Card moved to Done; workspace and branch cleaned up.")
        self.post_for_task(task_id, "\n".join(lines))

    def pr_opened(self, task_id: int, pr_url: str) -> None:
        self.post_for_task(task_id, f":rocket: PR opened for task {task_id}: {pr_url}")

    def pr_needs_attention(self, task_id: int, problem: str, pr_url: str) -> None:
        self.post_for_task(task_id, f":warning: Task {task_id}: PR needs attention: {problem} {pr_url}")

    def daily_cap(self, spent: float, cap: float) -> None:
        self.post(f":moneybag: Daily cap reached: ${spent:.2f} / ${cap:.2f}. Dispatching paused.")

    def nightly(self, summary: str) -> None:
        self.post(f":broom: Nightly cleanup:\n{summary}")
