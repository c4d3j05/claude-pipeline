"""Per-task detail: what is happening to one task right now.

Read-only. Everything is assembled from four sources that already exist:
  * the ledger row (state, retries, accounted cost, PR),
  * the worker's stream-json session log (turns, latest actions, live cost estimate),
  * the process table (is a worker / reviewer / test gate running, and for how long),
  * the task's git workspace (commits so far, uncommitted files).

The ledger alone cannot say much: `working` covers setup, the baseline gate, the worker's
turns AND the worker's own Stop-hook gate, and a running session's cost is only recorded
when it ends. This module fills those gaps.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from collections import Counter
from pathlib import Path

from .config import Config
from .db import ACTIVE_STATES, Ledger
from .procenv import clean_env

# Token classes priced relative to the input price (Anthropic's standard ratios). One unknown
# per model -- the input price -- is calibrated from sessions that already finished, so the
# estimate tracks whatever rates the account really pays.
_RATIO = {"input": 1.0, "cache_creation": 1.25, "cache_read": 0.10, "output": 5.0}
_DEFAULT_INPUT_PRICE = 5.0e-6   # $/token; only used before any session has finished


# --- session log --------------------------------------------------------------------------
def read_events(path: Path) -> list[dict]:
    events: list[dict] = []
    try:
        for line in path.read_text(errors="ignore").splitlines():
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(ev, dict):
                events.append(ev)
    except OSError:
        pass
    return events


def sessions(events: list[dict]) -> list[list[dict]]:
    """Events grouped per session: each group ends with its `result` event, except possibly
    the last one, which is the session still in progress."""
    groups: list[list[dict]] = [[]]
    for ev in events:
        groups[-1].append(ev)
        if ev.get("type") == "result":
            groups.append([])
    return groups if groups[-1] else groups[:-1]


def split_sessions(events: list[dict]) -> tuple[list[dict], list[dict]]:
    """(finished result events, events of the session still in progress)."""
    results = [e for e in events if e.get("type") == "result"]
    last = max((i for i, e in enumerate(events) if e.get("type") == "result"), default=-1)
    return results, events[last + 1:]


def _weighted(usage: dict) -> float:
    return (
        (usage.get("input_tokens") or usage.get("inputTokens") or 0) * _RATIO["input"]
        + (usage.get("cache_creation_input_tokens") or usage.get("cacheCreationInputTokens") or 0)
        * _RATIO["cache_creation"]
        + (usage.get("cache_read_input_tokens") or usage.get("cacheReadInputTokens") or 0)
        * _RATIO["cache_read"]
        + (usage.get("output_tokens") or usage.get("outputTokens") or 0) * _RATIO["output"]
    )


def calibrate_input_price(results: list[dict], model: str | None = None) -> float | None:
    """$/token of plain input, from the latest finished result that reports tokens and cost.

    Deliberately simple: a fancier fit (pooling streamed token counts across fresh and
    resumed sessions) was tried and was far less accurate. This one always lands below the
    real cost (by 7-27% on the sessions it was checked against), so the live figure is
    presented as a lower bound.
    """
    for ev in reversed(results):
        for name, mu in (ev.get("modelUsage") or {}).items():
            if model and name != model:
                continue
            w, cost = _weighted(mu), mu.get("costUSD") or 0
            if w > 0 and cost > 0:
                return cost / w
    return None


def _messages(events: list[dict]) -> dict[str, dict]:
    """Assistant messages by id. The stream repeats a message's usage on each content block,
    so keep one entry per id (the fullest) or tokens would be counted several times."""
    out: dict[str, dict] = {}
    for i, ev in enumerate(events):
        if ev.get("type") != "assistant":
            continue
        msg = ev.get("message") or {}
        mid = msg.get("id") or f"anon-{i}"
        usage = msg.get("usage") or {}
        if mid not in out or _weighted(usage) >= _weighted(out[mid].get("usage") or {}):
            out[mid] = msg
    return out


def session_model(events: list[dict]) -> str | None:
    models = Counter((m.get("model") or "") for m in _messages(events).values())
    models.pop("", None)
    return models.most_common(1)[0][0] if models else None


def estimate_cost(events: list[dict], input_price: float) -> float:
    return sum(_weighted(m.get("usage") or {}) for m in _messages(events).values()) * input_price


def summarize_session(events: list[dict], keep_actions: int = 6) -> dict:
    tools: Counter = Counter()
    actions: list[str] = []
    last_text = ""
    for ev in events:
        if ev.get("type") != "assistant":
            continue
        for block in (ev.get("message") or {}).get("content", []) or []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use":
                tools[block.get("name", "?")] += 1
                inp = block.get("input") or {}
                target = (inp.get("file_path") or inp.get("command") or inp.get("pattern")
                          or inp.get("path") or inp.get("description") or "")
                target = re.sub(r"\s+", " ", str(target)).strip()
                actions.append(f"{block.get('name', '?')}: {target[-90:]}" if target
                               else block.get("name", "?"))
            elif block.get("type") == "text" and block.get("text", "").strip():
                last_text = re.sub(r"\s+", " ", block["text"]).strip()
    return {
        "turns": len(_messages(events)),
        "tools": dict(tools),
        "actions": actions[-keep_actions:],
        "last_text": last_text[:280],
    }


def past_sessions(results: list[dict]) -> list[dict]:
    """Finished sessions with their NEW spend (a resumed session reports a running total)."""
    out, prev = [], 0.0
    for ev in results:
        cum = ev.get("total_cost_usd") or 0.0
        out.append({
            "new_cost": cum - prev if cum >= prev else cum,
            "turns": ev.get("num_turns") or 0,
            "outcome": ev.get("subtype") or ("error" if ev.get("is_error") else "success"),
        })
        prev = max(prev, cum)
    return out


# --- process table ------------------------------------------------------------------------
def _etime_seconds(etime: str) -> int:
    days = 0
    if "-" in etime:
        d, etime = etime.split("-", 1)
        days = int(d)
    parts = [int(p) for p in etime.split(":")]
    while len(parts) < 3:
        parts.insert(0, 0)
    h, m, s = parts
    return days * 86400 + h * 3600 + m * 60 + s


def process_table() -> list[dict]:
    """pid, ppid, age in seconds and command line of every process (works on Linux and macOS)."""
    try:
        out = subprocess.run(["ps", "-eo", "pid=,ppid=,etime=,args="], capture_output=True,
                             text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    rows = []
    for line in out.splitlines():
        parts = line.split(None, 3)
        if len(parts) < 4:
            continue
        try:
            rows.append({"pid": int(parts[0]), "ppid": int(parts[1]),
                         "age": _etime_seconds(parts[2]), "args": parts[3]})
        except ValueError:
            continue
    return rows


def _cwd(pid: int) -> str:
    try:
        return os.readlink(f"/proc/{pid}/cwd")
    except OSError:
        return ""


def find_processes(table: list[dict], slug: str, cwd_of=_cwd) -> dict:
    """Which of the task's processes are alive: worker, reviewer, and a test gate (and whose)."""
    by_pid = {r["pid"]: r for r in table}

    def ancestors(row):
        seen, cur = [], row
        while cur and cur["ppid"] in by_pid and cur["ppid"] not in seen and len(seen) < 12:
            seen.append(cur["ppid"])
            cur = by_pid[cur["ppid"]]
            yield cur

    def is_claude(r):
        a = r["args"]
        return ("claude" in a.split(None, 1)[0] or " claude " in a) and "--output-format" in a

    found: dict = {"worker": None, "reviewer": None, "gate": None}
    for r in table:
        if is_claude(r) and cwd_of(r["pid"]).rstrip("/").endswith(slug):
            kind = "reviewer" if "--permission-mode plan" in r["args"] else "worker"
            if found[kind] is None or r["age"] > found[kind]["age"]:
                found[kind] = r
    gates = [r for r in table if f"ntree run {slug} " in r["args"] and r["args"].startswith("bash")]
    if gates:
        g = max(gates, key=lambda r: r["age"])
        # A gate started by the worker's Stop hook has a `claude` process above it.
        hook = any(is_claude(a) for a in ancestors(g))
        found["gate"] = {"age": g["age"], "kind": "stop-hook" if hook else "dispatcher"}
    return found


# --- workspace ----------------------------------------------------------------------------
def workspace_summary(repo: Path, slug: str, base: str) -> dict | None:
    path = repo / ".ntree" / slug
    if not path.is_dir():
        return None
    env = clean_env("plain")

    def git(*args):
        cp = subprocess.run(["git", *args], cwd=path, capture_output=True, text=True,
                            timeout=30, env=env)
        return cp.stdout.strip() if cp.returncode == 0 else ""

    ref = f"origin/{base}" if git("rev-parse", "--verify", "--quiet", f"origin/{base}") else base
    commits = [ln for ln in git("log", "--format=%h %s", f"{ref}..HEAD").splitlines() if ln]
    dirty = [ln for ln in git("status", "--porcelain").splitlines() if ln]
    return {"path": str(path), "commits": commits, "uncommitted": dirty}


# --- phase --------------------------------------------------------------------------------
def _mins(seconds: int) -> str:
    return f"{seconds // 60}m{seconds % 60:02d}s"


def derive_phase(state: str, procs: dict, resumable: bool = False) -> str:
    gate = procs.get("gate")
    if state == "done":
        return "merged and cleaned up"
    if state == "pr_open":
        return "PR open - waiting for a human to review and merge"
    if state == "blocked":
        return ("blocked - resumable: drag the card back to Ready (or press r) to continue "
                "the same session" if resumable else "blocked - needs a decision")
    if state == "claimed":
        return "starting: creating the workspace"
    if procs.get("reviewer") or state == "reviewing":
        return "review (read-only reviewer)" + (
            f", running {_mins(procs['reviewer']['age'])}" if procs.get("reviewer") else ", starting")
    if gate and gate["kind"] == "stop-hook":
        return (f"worker finished its turns; its own Stop-hook test gate is running "
                f"({_mins(gate['age'])} so far, the session is held open until it ends)")
    if gate:
        return f"dispatcher test gate running ({_mins(gate['age'])} so far)"
    if procs.get("worker"):
        return f"worker session running ({_mins(procs['worker']['age'])} so far)"
    if state == "testing":
        return "waiting for a test-gate slot (queued behind another gate)"
    return "setup or baseline gate (no worker process yet)"


# --- assembly -----------------------------------------------------------------------------
def task_detail(cfg: Config, ledger: Ledger, task_id: int, logs_root: Path = Path("logs"),
                table: list[dict] | None = None, cwd_of=_cwd) -> dict | None:
    row = ledger.get(task_id)
    if row is None:
        return None
    row = dict(row)
    slug, state = row["slug"], row["state"]
    active = state in ACTIVE_STATES
    procs = find_processes(table if table is not None else process_table(), slug, cwd_of) \
        if active else {"worker": None, "reviewer": None, "gate": None}
    phase = derive_phase(state, procs, bool(row.get("resumable")))

    reviewing = state == "reviewing" or bool(procs.get("reviewer"))
    worker_events = read_events(logs_root / slug / "session.jsonl")
    review_events = read_events(logs_root / slug / "review.jsonl")
    events = review_events if reviewing else worker_events
    log = logs_root / slug / ("review.jsonl" if reviewing else "session.jsonl")
    groups = sessions(events)
    results = [e for e in events if e.get("type") == "result"]
    in_progress = bool(groups) and groups[-1][-1].get("type") != "result"
    latest = groups[-1] if groups else []           # the live session, else the one just ended
    session = summarize_session(latest) if (active and latest) else None
    age = int(time.time() - log.stat().st_mtime) if log.exists() else None

    est, calibrated = None, False
    if active and in_progress:
        model = session_model(latest)
        price = calibrate_input_price(
            [e for e in worker_events + review_events if e.get("type") == "result"], model)
        calibrated = price is not None
        est = estimate_cost(latest, price or _DEFAULT_INPUT_PRICE)

    return {
        "task_id": task_id, "slug": slug, "state": state, "phase": phase,
        "started_at": row["started_at"], "resumable": bool(row.get("resumable")),
        "test_retries": row["test_retries"], "review_retries": row["review_retries"],
        "max_turns": cfg.max_turns, "task_cap": cfg.task_cap_usd,
        "accounted_cost": row["cost_usd"], "lifetime_cost": row.get("lifetime_cost") or 0.0,
        "pr_url": row["pr_url"] or "",
        "session": session, "session_finished": bool(latest) and not in_progress,
        "log_age": age, "est_cost": est, "est_calibrated": calibrated,
        "past": past_sessions(results),
        # Sessions that already ended but are not in the ledger yet (the process is still
        # alive, e.g. held open by the Stop-hook gate) plus the live estimate.
        "unrecorded": (max(0.0, sum(p["new_cost"] for p in past_sessions(results))
                           - (row.get("lifetime_cost") or 0.0)) if active else 0.0),
        "procs": procs,
        "workspace": workspace_summary(cfg.repo_path, slug, cfg.base_branch),
    }


def format_detail(d: dict | None) -> str:
    if d is None:
        return "(no such task in the ledger)"
    L = [f"Task #{d['task_id']}   {d['state'].upper()}   ({d['slug']})", "",
         f"Phase      {d['phase']}"]
    if d["pr_url"]:
        L.append(f"PR         {d['pr_url']}")
    L.append(f"Retries    tests {d['test_retries']} / review {d['review_retries']}")
    spent = d["accounted_cost"]
    cost = f"${spent:.2f} recorded this attempt (task cap ${d['task_cap']:.0f}; "
    cost += f"lifetime ${d['lifetime_cost']:.2f})"
    L.append(f"Cost       {cost}")
    if d["state"] in ACTIVE_STATES and (d["unrecorded"] > 0.005 or d["est_cost"]):
        pending = d["unrecorded"] + (d["est_cost"] or 0.0)
        L.append(f"Spent      at least ~${d['accounted_cost'] + pending:.2f} this attempt, "
                 f"~${d['lifetime_cost'] + pending:.2f} over the task's life, counting sessions "
                 f"not yet recorded (${d['unrecorded']:.2f} finished"
                 + (f" + ~${d['est_cost']:.2f} live" if d["est_cost"] else "") + ")")
    s = d["session"]
    if s is not None:
        L.append("")
        if d["est_cost"] is not None:
            basis = ("estimated from your recent rates" if d["est_calibrated"]
                     else "rough, default rates")
            L.append(f"Live cost  at least ~${d['est_cost']:.2f} for the session in progress "
                     f"({basis}; the real figure is usually 10-25% higher and is recorded "
                     f"when the session ends)")
        L.append(f"Session    turn {s['turns']} of {d['max_turns']}   tools: "
                 + (", ".join(f"{k} {v}" for k, v in sorted(s['tools'].items(), key=lambda kv: -kv[1])) or "none yet"))
        if d["log_age"] is not None:
            L.append(f"Activity   last log write {d['log_age']}s ago"
                     + ("   (session has ended)" if d["session_finished"] else ""))
        if s["actions"]:
            L.append("Latest actions:")
            L += [f"   - {a}" for a in s["actions"]]
        if s["last_text"]:
            L.append(f"Last said  {s['last_text']}")
    if d["past"]:
        L.append("")
        L.append("Earlier sessions (new spend / turns / outcome):")
        L += [f"   {i}. ${p['new_cost']:.2f}  {p['turns']} turns  {p['outcome']}"
              for i, p in enumerate(d["past"], 1)]
    ws = d["workspace"]
    if ws:
        L.append("")
        L.append(f"Workspace  {len(ws['commits'])} commit(s), {len(ws['uncommitted'])} uncommitted file(s)")
        L += [f"   {c}" for c in ws["commits"][:8]]
        L += [f"   uncommitted: {u}" for u in ws["uncommitted"][:5]]
    return "\n".join(L)
