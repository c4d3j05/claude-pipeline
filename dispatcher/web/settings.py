"""Operational settings the dashboard may edit (OpenSpec change dashboard-settings).

Only the allow-list below is ever read from, written to, shown or logged. Every other line of a
`.env` (tokens, passwords, webhooks) is skipped by the parser and never kept in memory.
"""

from __future__ import annotations

import datetime as dt
import math
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from ..config import Config

BACKUPS_KEPT = 10
BOOL_TRUE = ("true", "1", "yes")
BOOL_FALSE = ("false", "0", "no")
INT_RE = re.compile(r"[+-]?\d+")
FLOAT_RE = re.compile(r"[+-]?(\d+\.?\d*|\.\d+)")


@dataclass(frozen=True)
class Setting:
    key: str
    kind: str            # "int" | "float" | "bool"
    default: str         # the code default, shown when the key is unset
    min: float | None
    max: float | None
    restart: str | None  # unit to restart for it to apply; None = hot-reloaded
    help: str


PIPELINE_UNIT = "claude-pipeline-dispatcher"
PLANNER_UNIT = "claude-planner"
EXPLORER_UNIT = "explorer-serve"


def _p(key, kind, default, lo, hi, hot, help_):
    return Setting(key, kind, default, lo, hi, None if hot else PIPELINE_UNIT, help_)


def _pl(key, kind, default, lo, hi, help_):
    return Setting(key, kind, default, lo, hi, PLANNER_UNIT, help_)


def _ex(key, kind, default, lo, hi, help_):
    return Setting(key, kind, default, lo, hi, EXPLORER_UNIT, help_)


ALLOWED: dict[str, tuple[Setting, ...]] = {
    "pipeline": (
        _p("MAX_WORKERS", "int", "2", 1, 8, True, "How many tickets are built at the same time."),
        _p("DAILY_CAP_USD", "float", "50", 1, None, True,
           "Most the pipeline may spend on workers in one day."),
        _p("TASK_CAP_USD", "float", "10", 1, None, True,
           "Most one ticket may cost before it is blocked."),
        _p("WALL_CLOCK_LIMIT_MIN", "int", "45", 10, 240, True,
           "Minutes a worker may run on one ticket before it is stopped."),
        _p("MAX_TURNS", "int", "60", 10, 300, True, "Most turns a worker may take per session."),
        _p("RELEASE_TAGS", "bool", "false", None, None, True,
           "Tag the target repo when explorer settles a release batch."),
        _p("RELEASE_AUTO_MERGE", "bool", "false", None, None, False,
           "Merge a ticket PR into its release branch automatically."),
        _p("RELEASE_MERGE_TO_MAIN", "bool", "false", None, None, False,
           "Merge the release PR into main once every ticket is done and green."),
    ),
    "planner": (
        _pl("MAX_INVESTIGATIONS", "int", "3", 1, 6, "How many investigations may run at once."),
        _pl("THREAD_CAP_USD", "float", "5", 1, None, "Most one planner thread may spend."),
        _pl("DAILY_CAP_USD", "float", "20", 1, None, "Most the planner may spend in one day."),
        _pl("IDLE_EXPIRE_HOURS", "int", "72", 1, 720,
            "Hours a thread may sit idle before it expires."),
    ),
    "explorer": (
        _ex("POLL_INTERVAL_MIN", "int", "10", 1, 60, "Minutes between explorer's checks for work."),
        _ex("RUN_CAP_USD", "float", "10", 0.5, None, "Most one explorer run may spend."),
        _ex("DAILY_CAP_USD", "float", "75", 1, None, "Most explorer may spend in one day."),
        _ex("LANE_DAILY_CAP_USD", "float", "10", 0.5, None,
            "Most one lane may spend in one day."),
        _ex("EXPLORE_CAP_USD", "float", "0.60", 0.1, None,
            "Most one exploration session may spend."),
        _ex("EXPLORE_WALL_CLOCK_MIN", "int", "8", 3, 60,
            "Minutes one exploration session may run."),
        _ex("RECHECK_WALL_CLOCK_MIN", "int", "15", 3, 60,
            "Minutes one recheck of a finding may run."),
        _ex("LANE_RUN_MAX_MIN", "int", "45", 5, 240, "Minutes one lane run may take in all."),
        _ex("LANE_PRACTITIONER_PER_DAY", "int", "1", 0, 5,
            "Practitioner sessions a lane may start per day."),
        _ex("FINDINGS_PER_RUN", "int", "10", 0, 50, "Most findings one run may file."),
        _ex("QUESTIONS_PER_RUN", "int", "5", 0, 50, "Most questions one run may ask the owner."),
        _ex("BATCH_MAX_FINDINGS", "int", "25", 1, 60, "Most findings in one release batch."),
        _ex("BATCH_VETO_HOURS", "float", "12", 0, 72,
            "Hours the owner may veto a batch before it is sent."),
    ),
}

# service -> (the key that must not be exceeded, keys that must not exceed it)
CROSS_CHECKS: dict[str, tuple[tuple[str, str], ...]] = {
    "pipeline": (("TASK_CAP_USD", "DAILY_CAP_USD"),),
    "planner": (("THREAD_CAP_USD", "DAILY_CAP_USD"),),
    "explorer": (("LANE_DAILY_CAP_USD", "DAILY_CAP_USD"), ("RUN_CAP_USD", "DAILY_CAP_USD")),
}


def find(service: str, key: str) -> Setting | None:
    if not isinstance(service, str) or not isinstance(key, str):
        return None
    for s in ALLOWED.get(service, ()):
        if s.key == key:
            return s
    return None


def _num(v: float) -> str:
    return f"{v:g}"


# --- where the files are ------------------------------------------------------------------------
def path_of(cfg: Config, service: str) -> Path | None:
    """The service's .env, or None when its checkout is not configured or has no .env."""
    if service == "pipeline":
        raw = (cfg.env_file or "").strip()
        path = Path(raw).expanduser() if raw else None
    elif service in ("planner", "explorer"):
        d = (getattr(cfg, f"dashboard_{service}_dir", "") or "").strip()
        path = Path(d).expanduser() / ".env" if d else None
    else:
        return None
    return path if path is not None and path.is_file() else None


# --- reading --------------------------------------------------------------------------------------
def _parse_value(raw: str) -> str:
    v = raw.strip()
    if v[:1] in ("'", '"'):
        end = v.find(v[0], 1)
        if end != -1:
            return v[1:end]
    return re.sub(r"\s+#.*$", "", v).strip()


def read(cfg: Config, service: str) -> dict[str, str | None]:
    """The allow-listed keys of that service's .env; None = unset. Nothing else is kept."""
    keys = {s.key for s in ALLOWED.get(service, ())}
    out: dict[str, str | None] = {s.key: None for s in ALLOWED.get(service, ())}
    path = path_of(cfg, service)
    if path is None:
        return out
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return out
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        key, sep, rest = line.partition("=")
        key = key.strip()
        if sep and key in keys:
            out[key] = _parse_value(rest)   # the last one wins, as in config.load
    return out


# --- validating -----------------------------------------------------------------------------------
def _bounds_text(s: Setting) -> str:
    if s.min is not None and s.max is not None:
        return f"{s.key} must be between {_num(s.min)} and {_num(s.max)}"
    if s.min is not None:
        return f"{s.key} must be at least {_num(s.min)}"
    return f"{s.key} must be at most {_num(s.max or 0)}"


def _parse(s: Setting, value: str) -> tuple[bool | float | None, str | None]:
    v = value.strip() if isinstance(value, str) else ""
    if s.kind == "bool":
        if v.lower() in BOOL_TRUE:
            return True, None
        if v.lower() in BOOL_FALSE:
            return False, None
        return None, f"{s.key} must be true or false"
    if s.kind == "int":
        if not INT_RE.fullmatch(v):
            return None, f"{s.key} must be a whole number"
        num: float = int(v)
    else:
        if not FLOAT_RE.fullmatch(v):
            return None, f"{s.key} must be a number"
        num = float(v)
        if not math.isfinite(num):
            return None, f"{s.key} must be a number"
    if (s.min is not None and num < s.min) or (s.max is not None and num > s.max):
        return None, _bounds_text(s)
    return num, None


def render(s: Setting, parsed: bool | float) -> str:
    """The text written to the file for a parsed value."""
    if s.kind == "bool":
        return "true" if parsed else "false"
    if s.kind == "int":
        return str(int(parsed))
    text = format(float(parsed), "f").rstrip("0").rstrip(".")
    return text or "0"


def normalize(service: str, key: str, value: str) -> str | None:
    """The canonical text for a valid value, or None (used for display and the log message)."""
    s = find(service, key)
    if s is None:
        return None
    parsed, err = _parse(s, value)
    return None if err else render(s, parsed)


def _effective(s: Setting, current: dict[str, str | None]) -> float | None:
    raw = current.get(s.key)
    try:
        return float(raw if raw not in (None, "") else s.default)
    except ValueError:
        return float(s.default)


def validate(service: str, key: str, value: str, current: dict) -> str | None:
    """An error text, or None when the value may be written."""
    s = find(service, key)
    if s is None:
        return "not a setting that can be changed here"
    parsed, err = _parse(s, value)
    if err:
        return err
    after = dict(current)
    after[key] = render(s, parsed)
    for small, big in CROSS_CHECKS.get(service, ()):
        if key not in (small, big):
            continue
        a, b = find(service, small), find(service, big)
        va, vb = _effective(a, after), _effective(b, after)
        if va is not None and vb is not None and va > vb:
            return f"{small} must not exceed {big} ({_num(vb)})" if key == small \
                else f"{big} must not be below {small} ({_num(va)})"
    return None


# --- writing --------------------------------------------------------------------------------------
def _backup(target: Path, now: dt.datetime) -> Path:
    stamp = now.strftime("%Y%m%d%H%M%S")
    backup = target.with_name(f"{target.name}.bak-{stamp}")
    n = 1
    while backup.exists():
        n += 1
        backup = target.with_name(f"{target.name}.bak-{stamp}-{n}")
    shutil.copy2(target, backup)
    old = sorted(p for p in target.parent.glob(f"{target.name}.bak-*") if p.is_file())
    for p in old[:-BACKUPS_KEPT]:
        p.unlink(missing_ok=True)
    return backup


def write(cfg: Config, service: str, key: str, value: str, now: dt.datetime) -> Path:
    """Change one allow-listed key in that service's .env; returns the backup path.

    Raises ValueError for anything not allowed or invalid, before touching a file.
    """
    s = find(service, key)
    if s is None:
        raise ValueError("not a setting that can be changed here")
    path = path_of(cfg, service)
    if path is None:
        raise ValueError(f"{service} is not configured")
    err = validate(service, key, value, read(cfg, service))
    if err:
        raise ValueError(err)
    text_value = normalize(service, key, value) or ""
    target = path.resolve()
    lines = target.read_text(encoding="utf-8").splitlines(keepends=True)
    hit = None
    for i, line in enumerate(lines):
        st = line.lstrip()
        if not st.startswith("#") and st.startswith(f"{key}="):
            hit = i              # the last one: it is the one config.load ends up with
    new_line = f"{key}={text_value}\n"
    if hit is not None:
        ending = "\r\n" if lines[hit].endswith("\r\n") else "\n" if lines[hit].endswith("\n") else ""
        lines[hit] = f"{key}={text_value}{ending}"
    else:
        if lines and not lines[-1].endswith("\n"):
            lines[-1] += "\n"
        lines += [f"# set from the dashboard {now.date().isoformat()}\n", new_line]
    backup = _backup(target, now)
    fd, tmp = tempfile.mkstemp(prefix=".env.", suffix=".tmp", dir=str(target.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            f.write("".join(lines))
        shutil.copymode(target, tmp)
        os.replace(tmp, target)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return backup
