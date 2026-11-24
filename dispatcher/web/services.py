"""The `services` source: what `systemctl --user status` shows for each unit, read-only.

For every unit in DASHBOARD_UNITS: its state and since when, main PID, memory, CPU, restarts, and
the processes running in its cgroup (so a long release test or a worker session is visible from
the dashboard). Timers also get their last and next run. Nothing here starts, stops or reloads a
unit: the only commands are `systemctl --user show` and reads under /sys/fs/cgroup and /proc.

On a host without systemd (macOS) the source answers `available: false` instead of failing.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

PROPS = ("Id", "Description", "LoadState", "ActiveState", "SubState", "UnitFileState", "Result",
         "ActiveEnterTimestamp", "InactiveEnterTimestamp", "MainPID", "MemoryCurrent", "CPUUsageNSec",
         "NRestarts", "ControlGroup", "LastTriggerUSec", "NextElapseUSecRealtime")
SHOW_TIMEOUT_SEC = 10
MAX_PROCESSES = 12
MAX_ARGS = 4000
UNSET = ("", "[not set]", "n/a", "0", "infinity", "18446744073709551615")


def parse_show(text: str) -> list[dict[str, str]]:
    """`systemctl show` output for several units: blocks of key=value separated by blank lines."""
    units, cur = [], {}
    for line in text.splitlines():
        if not line.strip():
            if cur:
                units.append(cur)
                cur = {}
            continue
        key, _, value = line.partition("=")
        cur[key] = value
    if cur:
        units.append(cur)
    return units


def _int(value: str | None) -> int | None:
    v = (value or "").strip()
    return int(v) if v.isdigit() and v not in UNSET else None


def _when(value: str | None) -> str | None:
    v = (value or "").strip()
    return None if v in UNSET else v


def processes(control_group: str, *, root: Path = Path("/sys/fs/cgroup"), proc: Path = Path("/proc")) -> list[dict]:
    """The processes in a unit's cgroup, with their command lines (cut short)."""
    if not control_group:
        return []
    out: list[dict] = []
    base = root / control_group.lstrip("/")
    pids: list[int] = []
    for f in [base / "cgroup.procs", *sorted(base.glob("**/cgroup.procs"))]:
        try:
            pids += [int(p) for p in f.read_text().split() if p.isdigit()]
        except OSError:
            continue
    for pid in list(dict.fromkeys(pids))[:MAX_PROCESSES]:
        try:
            raw = (proc / str(pid) / "cmdline").read_bytes()
        except OSError:
            continue
        args = " ".join(part.decode(errors="replace") for part in raw.split(b"\0") if part)
        if args:
            out.append({"pid": pid, "args": args if len(args) <= MAX_ARGS else args[:MAX_ARGS] + "…"})
    return out


def describe(props: dict[str, str], *, procs=processes) -> dict:
    unit = props.get("Id", "")
    active = props.get("ActiveState", "unknown")
    memory = _int(props.get("MemoryCurrent"))
    cpu_ns = _int(props.get("CPUUsageNSec"))
    out = {
        "unit": unit,
        "description": props.get("Description", ""),
        "installed": props.get("LoadState", "") not in ("not-found", ""),
        "enabled": props.get("UnitFileState", ""),
        "active": active,
        "sub": props.get("SubState", ""),
        "result": props.get("Result", ""),
        "since": _when(props.get("ActiveEnterTimestamp") if active == "active"
                       else props.get("InactiveEnterTimestamp")),
        "main_pid": _int(props.get("MainPID")),
        "memory_mb": round(memory / 1_048_576, 1) if memory is not None else None,
        "cpu_sec": round(cpu_ns / 1e9) if cpu_ns is not None else None,
        "restarts": _int(props.get("NRestarts")) or 0,
        "is_timer": unit.endswith(".timer"),
        "last_run": _when(props.get("LastTriggerUSec")),
        "next_run": _when(props.get("NextElapseUSecRealtime")),
        "processes": [],
    }
    if active == "active" and not out["is_timer"]:
        out["processes"] = procs(props.get("ControlGroup", ""))
    return out


def read_services(units: list[str], *, run=subprocess.run, which=shutil.which, procs=processes) -> dict:
    """Every configured unit, in order. `available` is false where there is no systemctl."""
    if not units:
        return {"available": True, "units": []}
    if which("systemctl") is None:
        return {"available": False, "units": []}
    cp = run(["systemctl", "--user", "show", "--no-pager", "-p", ",".join(PROPS), *units],
             capture_output=True, text=True, timeout=SHOW_TIMEOUT_SEC, check=False)
    if cp.returncode != 0 and not cp.stdout.strip():
        raise RuntimeError(f"systemctl --user show failed: {(cp.stderr or '').strip()[:200]}")
    shown = {p.get("Id", ""): p for p in parse_show(cp.stdout)}
    rows = []
    for unit in units:
        # systemctl answers in the order asked; a unit it does not know comes back as not-found
        props = shown.get(unit) or shown.get(f"{unit}.service") or {"Id": unit, "LoadState": "not-found"}
        rows.append(describe(props, procs=procs))
    return {"available": True, "units": rows}


def needs_attention(row: dict) -> str | None:
    """Why this unit should be on the Overview's alerts, or None."""
    if not row["installed"]:
        return "is not installed"
    if row["active"] == "failed":
        return f"has failed ({row['result'] or 'unknown reason'})"
    if row["enabled"] == "enabled" and row["active"] != "active":
        return f"is {row['active']} although it is enabled"
    return None
