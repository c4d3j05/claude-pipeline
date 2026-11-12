"""Which files a task changes: the plan (a `Touches:` line written by claude-planner) and the fact
(the files its diff changed once a PR exists).

Entries are repository-relative: a file, or a directory ending in `/`.
"""

from __future__ import annotations

import json
import re

from .prompt import _strip_html

_TOUCHES_LINE = re.compile(r"^touches:\s*(.*)$", re.IGNORECASE)


def parse(description_html: str | None) -> list[str]:
    """The paths of the last `Touches:` line in a task description; empty when there is none."""
    found: list[str] = []
    for line in _strip_html(description_html or "").splitlines():
        m = _TOUCHES_LINE.match(line.strip())
        if m:
            found = [p.strip() for p in m.group(1).split(",") if p.strip()]
    return found


def dump(paths: list[str] | None) -> str | None:
    return json.dumps(paths) if paths else None


def load(raw: str | None) -> list[str]:
    try:
        return list(json.loads(raw)) if raw else []
    except (ValueError, TypeError):
        return []
