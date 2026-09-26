"""Slack notifications. A single channel, messages only on the events the spec lists."""

from __future__ import annotations

import json

import requests

from .log import get

log = get("notify")


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

    def task_blocked(self, task_id: int, reason: str, link: str) -> None:
        self.post(f":no_entry: Task {task_id} *blocked*: {reason}\n{link}")

    def pr_opened(self, task_id: int, pr_url: str) -> None:
        self.post(f":rocket: PR opened for task {task_id}: {pr_url}")

    def daily_cap(self, spent: float, cap: float) -> None:
        self.post(f":moneybag: Daily cap reached: ${spent:.2f} / ${cap:.2f}. Dispatching paused.")

    def nightly(self, summary: str) -> None:
        self.post(f":broom: Nightly cleanup:\n{summary}")
