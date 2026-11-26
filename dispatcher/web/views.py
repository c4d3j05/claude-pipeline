"""What the overview says: each service's health, today's spend, and the alerts worth a look.

Pure functions over the sources' data (dicts in the shapes of `sources.py` and the integration
spec's "Status JSON"), so they are tested without a web server. Unknown fields are ignored and
missing ones are treated as absent, never as an error.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from .services import needs_attention
from .sources import Result

OPEN_FINDING_STATES = ("open", "held", "batched", "fix_pending", "fix_merged", "escalated")


@dataclass
class Alert:
    level: str          # "bad" needs the owner now; "warn" is worth a look
    service: str
    text: str
    tab: str = "overview"


def parse_ts(value) -> dt.datetime | None:
    if not value or not isinstance(value, str):
        return None
    try:
        t = dt.datetime.fromisoformat(value)
    except ValueError:
        return None
    return t if t.tzinfo else t.astimezone()      # the ledger writes the host's local time


def ago(value, now: dt.datetime | None = None) -> str:
    t = parse_ts(value)
    if t is None:
        return "-"
    secs = int(((now or dt.datetime.now(dt.UTC)) - t).total_seconds())
    if secs < 0:
        return "just now"
    for unit, size in (("d", 86400), ("h", 3600), ("min", 60)):
        if secs >= size:
            return f"{secs // size} {unit} ago"
    return f"{secs} s ago"


def until(value, now: dt.datetime | None = None) -> str:
    """A future time as "in 3 h"; "due" when it has passed; "-" when unreadable."""
    t = parse_ts(value)
    if t is None:
        return "-"
    secs = int((t - (now or dt.datetime.now(dt.UTC))).total_seconds())
    if secs <= 0:
        return "due"
    for unit, size in (("d", 86400), ("h", 3600), ("min", 60)):
        if secs >= size:
            return f"in {secs // size} {unit}"
    return f"in {secs} s"


def money(value) -> str:
    try:
        return f"${float(value):,.2f}"
    except (TypeError, ValueError):
        return "-"


def spend(results: dict[str, Result]) -> list[dict]:
    """Today's spend against its cap for each service that reports one."""
    rows = []
    p = results["pipeline"].data if results["pipeline"].ok else None
    if p:
        rows.append({"service": "pipeline", "spent": p.get("today_cost"), "cap": p.get("cap")})
    for name in ("planner", "explorer"):
        d = results[name].data if results[name].ok else None
        if d:
            rows.append({"service": name, "spent": d.get("spend_today_usd"), "cap": d.get("daily_cap_usd")})
    for r in rows:
        try:
            r["pct"] = max(0, min(100, round(100 * float(r["spent"]) / float(r["cap"]))))
        except (TypeError, ValueError, ZeroDivisionError):
            r["pct"] = None
    return rows


def health(results: dict[str, Result]) -> list[dict]:
    """One line per service: up, paused, down or not configured, and what it is doing."""
    out = []
    for name, res in results.items():
        if not res.configured:
            out.append({"service": name, "state": "off", "detail": "not configured"})
            continue
        if not res.ok:
            out.append({"service": name, "state": "down", "detail": res.error})
            continue
        d = res.data
        if name == "pipeline":
            out.append({"service": name, "state": "paused" if d.get("paused") else "up",
                        "detail": f"{d.get('active', 0)} of {d.get('max_workers', 0)} workers busy"})
        elif name == "board":
            n = sum(len(b["tasks"]) for b in d.get("buckets") or [])
            out.append({"service": name, "state": "up", "detail": f"{n} tasks on the board"})
        elif name == "planner":
            listener = (d.get("listener") or {}).get("running")
            open_n = sum(1 for i in d.get("investigations") or [] if not _closed(i.get("state")))
            out.append({"service": name, "state": "down" if listener is False else "up",
                        "detail": ("listener not running; " if listener is False else "")
                        + f"{open_n} open investigation(s)"})
        elif name == "services":
            if not d.get("available"):
                out.append({"service": name, "state": "off", "detail": "no systemd on this host"})
                continue
            units = d.get("units") or []
            up = sum(1 for u in units if u["active"] == "active")
            bad = [u for u in units if needs_attention(u)]
            out.append({"service": name, "state": "down" if bad else "up",
                        "detail": f"{up} of {len(units)} units active"})
        elif name == "explorer":
            run = d.get("active_run")
            uat = d.get("uat") or {}
            detail = (f"run {run.get('id')} ({run.get('stage') or run.get('reason') or 'running'})" if run
                      else f"idle; last tick {ago(uat.get('last_tick'))}")
            out.append({"service": name, "state": "paused" if d.get("paused") else "up", "detail": detail})
    return out


def _closed(state) -> bool:
    s = str(state or "").lower()
    return s in ("filed", "cancelled", "canceled", "expired", "rejected", "done", "closed", "failed_final")


def alerts(results: dict[str, Result], now: dt.datetime | None = None) -> list[Alert]:
    now = now or dt.datetime.now(dt.UTC)
    out: list[Alert] = []
    for name, res in results.items():
        if res.configured and not res.ok:
            out.append(Alert("bad", name, f"{name} could not be read: {res.error}", name))
    p = results["pipeline"].data if results["pipeline"].ok else None
    if p:
        if p.get("paused"):
            out.append(Alert("warn", "pipeline", "dispatching is paused", "pipeline"))
        blocked = [t for t in p.get("tasks") or [] if t.get("state") == "blocked"]
        if blocked:
            ids = ", ".join(f"#{t['task_id']}" for t in blocked[:6])
            out.append(Alert("warn", "pipeline", f"{len(blocked)} task(s) blocked: {ids}", "pipeline"))
        try:
            if float(p.get("today_cost") or 0) >= float(p.get("cap") or 0) > 0:
                out.append(Alert("bad", "pipeline", "daily cost cap reached; nothing new is claimed",
                                 "pipeline"))
        except (TypeError, ValueError):
            pass
    pl = results["planner"].data if results["planner"].ok else None
    if pl and (pl.get("listener") or {}).get("running") is False:
        out.append(Alert("bad", "planner", "the Slack listener is not running", "planner"))
    sv = results["services"].data if "services" in results and results["services"].ok else None
    for u in (sv or {}).get("units") or []:
        why = needs_attention(u)
        if why:
            out.append(Alert("bad", "services", f"{u['unit']} {why}", "services"))
    ex = results["explorer"].data if results["explorer"].ok else None
    if ex:
        uat = ex.get("uat") or {}
        since = parse_ts(uat.get("ahead_since"))
        if since and uat.get("main_head") and uat.get("main_head") != uat.get("commit"):
            mins = int((now - since).total_seconds() // 60)
            out.append(Alert("bad" if mins >= 20 else "warn", "explorer",
                             f"main ({str(uat['main_head'])[:7]}) is ahead of UAT "
                             f"({str(uat.get('commit') or '?')[:7]}) for {mins} min", "explorer"))
        if ex.get("paused"):
            out.append(Alert("warn", "explorer", f"runs are paused: {ex['paused']}", "explorer"))
        for b in ex.get("batches") or []:
            if b.get("state") == "sent" and int(b.get("resends") or 0) > 0:
                out.append(Alert("warn", "explorer", f"{b.get('id')} sent but not picked up by the planner; "
                                 f"resent {b.get('resends')} time(s)", "explorer"))
        escalated = [f for f in (ex.get("findings") or {}).get("items") or [] if f.get("state") == "escalated"]
        if escalated:
            out.append(Alert("warn", "explorer", f"{len(escalated)} finding(s) wait for your answer",
                             "explorer"))
    return out


def findings(ex: dict, state: str = "open") -> tuple[list[dict], dict[str, int]]:
    """Explorer findings filtered by state ('open' = every open state, 'all', or one state)."""
    items = (ex.get("findings") or {}).get("items") or []
    counts = dict((ex.get("findings") or {}).get("counts") or {})
    if state == "all":
        rows = items
    elif state == "open":
        rows = [f for f in items if f.get("state") in OPEN_FINDING_STATES]
    else:
        rows = [f for f in items if f.get("state") == state]
    return sorted(rows, key=lambda f: f.get("last_seen") or "", reverse=True), counts


def _num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _count(v) -> int:
    return v if isinstance(v, int) and not isinstance(v, bool) else 0


def question_options(q: dict) -> list[tuple[str, str]]:
    """(letter, text) for an escalation question's `options`: A, B and C only. Accepts a list of
    texts (in order) or of {letter, text} objects; anything else gives no buttons."""
    items = q.get("options") if isinstance(q, dict) else None
    if not isinstance(items, list):
        return []
    out = []
    for i, o in enumerate(items):
        if isinstance(o, dict):
            letter, text = str(o.get("letter") or "").upper(), o.get("text") or o.get("label")
        else:
            letter, text = "ABC"[i] if i < 3 else "", o
        if letter in ("A", "B", "C") and isinstance(text, str) and text.strip():
            out.append((letter, text.strip()[:300]))
    return out


def lanes(ex: dict) -> list[dict]:
    """Rows for the Lanes table, in the order explorer gives them. [] hides the table."""
    items = ex.get("lanes")
    if not isinstance(items, list):
        return []
    rows = []
    for e in items:
        if not isinstance(e, dict) or not isinstance(e.get("lane"), str) or not e["lane"]:
            continue
        f = e.get("focus")
        if isinstance(f, dict) and f.get("batch") and isinstance(f.get("batch"), str):
            focus = f"{f['batch']} · {_count(f.get('settled'))}/{_count(f.get('total'))}"
        else:
            focus = "\u2014"
        spent, budget = e.get("spent_today"), e.get("budget")
        rows.append({
            "lane": e["lane"], "focus": focus,
            "last_run": e.get("last_run"), "next_run": e.get("next_run"),
            "spend": f"{money(spent)} / {money(budget)}",
            "over": _num(spent) and _num(budget) and budget > 0 and spent >= budget,
            "open_findings": _count(e.get("open_findings")),
        })
    return rows
