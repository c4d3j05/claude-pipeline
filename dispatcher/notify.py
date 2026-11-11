"""Slack notifications. A single channel, messages only on the events the spec lists."""

from __future__ import annotations

import json

import requests

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
    def __init__(self, webhook_url: str):
        self.url = webhook_url

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
        self.post("\n".join(lines))

    def task_merged(self, task_id: int, pr_url: str, cost: float | None = None,
                    lifetime: float | None = None) -> None:
        lines = [f":white_check_mark: Task {task_id} *merged*: {pr_url}"]
        if cost is not None:
            spent = f"Cost: {_money(cost)} this attempt"
            if lifetime is not None and abs(lifetime - cost) > 0.005:
                spent += f" ({_money(lifetime)} over the task's life)"
            lines.append(spent + ". Card moved to Done; workspace and branch cleaned up.")
        self.post("\n".join(lines))

    def pr_opened(self, task_id: int, pr_url: str) -> None:
        self.post(f":rocket: PR opened for task {task_id}: {pr_url}")

    def daily_cap(self, spent: float, cap: float) -> None:
        self.post(f":moneybag: Daily cap reached: ${spent:.2f} / ${cap:.2f}. Dispatching paused.")

    def nightly(self, summary: str) -> None:
        self.post(f":broom: Nightly cleanup:\n{summary}")
