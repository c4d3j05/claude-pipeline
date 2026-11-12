"""Which files a task changes: the plan (a `Touches:` line written by claude-planner) and the fact
(the files its diff changed once a PR exists).

Entries are repository-relative: a file, or a directory ending in `/`.
"""

from __future__ import annotations

import fnmatch
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


def _norm(path: str) -> str:
    return path.removeprefix("./")


def _hot(path: str, patterns: tuple[str, ...]) -> set[str]:
    return {p for p in patterns if fnmatch.fnmatchcase(path, p)}


def clashes(a: list[str], b: list[str], hot: tuple[str, ...] = ()) -> list[str]:
    """The entries of `a` that collide with an entry of `b`, as readable pairs; empty = no clash.

    Two entries collide when they are the same file, when one is a directory (ending in `/`) that
    contains the other, or when both match the same hot-path pattern (such as migrations, where
    different files still conflict). An empty list on either side never clashes: a task with no plan
    is not constrained.
    """
    out: list[str] = []
    for x in map(_norm, a):
        for y in map(_norm, b):
            same = x == y
            inside = (x.endswith("/") and y.startswith(x)) or (y.endswith("/") and x.startswith(y))
            both_hot = bool(_hot(x, hot) & _hot(y, hot))
            if same or inside or both_hot:
                out.append(x if x == y else f"{x} ~ {y}")
    return out
