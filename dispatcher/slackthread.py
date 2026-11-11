"""Post task events into the Slack thread claude-planner opened for the task.

A task filed by the planner ends with a line `Source: <permalink of the thread's first message>`.
The channel and thread timestamp are read from it when the task is claimed. Posting uses the
planner bot's token, because only the app that owns a DM can post in it. Everything here is best
effort: a missing link, a bad token or a Slack outage never affects a task, and the caller falls
back to the webhook channel so a notification is not lost.
"""

from __future__ import annotations

import re
from typing import NamedTuple

import requests

from .log import get
from .prompt import _strip_html

log = get("slackthread")

API = "https://slack.com/api/chat.postMessage"
_SOURCE = re.compile(r"(?im)^\s*Source:\s*(\S+)")
_LINK = re.compile(
    r"^https://[A-Za-z0-9-]+\.slack\.com/archives/(?P<channel>[CDG][A-Z0-9]+)/p(?P<digits>\d{7,})"
    r"(?:\?(?P<query>\S*))?$")


class Thread(NamedTuple):
    channel: str
    ts: str


def parse_source(description: str) -> Thread | None:
    """The thread named by the description's `Source:` permalink, or None if there is none.

    A permalink looks like `https://<ws>.slack.com/archives/<CHANNEL>/p1699999999000100`; its
    timestamp is the digits with a dot before the last six. A `thread_ts` query parameter, when
    present, names the thread instead.
    """
    m = _SOURCE.search(_strip_html(description or ""))
    if not m:
        return None
    link = _LINK.match(m.group(1).rstrip(".,;)·"))
    if not link:
        return None
    digits = link.group("digits")
    ts = f"{digits[:-6]}.{digits[-6:]}"
    for part in (link.group("query") or "").split("&"):
        key, _, value = part.partition("=")
        if key == "thread_ts" and re.fullmatch(r"\d+\.\d+", value):
            ts = value
    return Thread(link.group("channel"), ts)


def post(token: str, thread: Thread, text: str, *, http_post=requests.post) -> bool:
    """chat.postMessage into the thread. True only if Slack accepted it."""
    try:
        r = http_post(API, json={"channel": thread.channel, "thread_ts": thread.ts, "text": text,
                                 "unfurl_links": False},
                      headers={"Authorization": f"Bearer {token}"}, timeout=15)
        body = r.json()
    except (requests.RequestException, ValueError) as e:
        log.warning("thread post failed: %s", e)
        return False
    if not body.get("ok"):
        log.warning("thread post refused: %s", body.get("error"))
        return False
    return True
