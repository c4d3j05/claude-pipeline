"""Parse `claude -p --output-format stream-json` output.

Each line is one JSON event. We extract the fields the spec's Observability section
names: session id, a rolling last assistant message, tool-use count, permission
denials, and the final result (cost, duration, turns, summary, error state).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field


@dataclass
class SessionResult:
    session_id: str = ""
    last_message: str = ""          # rolling "last message" on the task
    tool_uses: int = 0              # cheap progress signal
    permission_denials: list[str] = field(default_factory=list)
    cost_usd: float = 0.0
    duration_ms: int = 0
    num_turns: int = 0
    is_error: bool = False
    result_text: str = ""           # final summary (used in the PR body)


def _text_from_content(content) -> str:
    if isinstance(content, str):
        return content
    parts = []
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
    return "\n".join(parts).strip()


def parse_event(ev: dict, out: SessionResult) -> None:
    etype = ev.get("type")

    if etype == "system" and ev.get("subtype") == "init":
        out.session_id = ev.get("session_id", out.session_id)

    elif etype == "assistant":
        msg = ev.get("message", {})
        for block in msg.get("content", []) or []:
            if isinstance(block, dict):
                if block.get("type") == "text" and block.get("text"):
                    out.last_message = block["text"].strip()
                elif block.get("type") == "tool_use":
                    out.tool_uses += 1

    elif etype == "user":
        # tool_result blocks with is_error surface permission denials and failures.
        msg = ev.get("message", {})
        for block in msg.get("content", []) or []:
            if isinstance(block, dict) and block.get("type") == "tool_result" and block.get("is_error"):
                text = _text_from_content(block.get("content"))
                if "permission" in text.lower() or "denied" in text.lower():
                    out.permission_denials.append(text[:200])

    elif etype == "result":
        out.cost_usd = ev.get("total_cost_usd", ev.get("cost_usd", out.cost_usd)) or 0.0
        out.duration_ms = ev.get("duration_ms", out.duration_ms) or 0
        out.num_turns = ev.get("num_turns", out.num_turns) or 0
        out.is_error = bool(ev.get("is_error", out.is_error))
        if ev.get("result"):
            out.result_text = str(ev["result"]).strip()
            if not out.last_message:
                out.last_message = out.result_text


def parse_lines(lines) -> SessionResult:
    out = SessionResult()
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(ev, dict):
            parse_event(ev, out)
    return out


def blocked_reason(result: SessionResult) -> str | None:
    """Return the reason if the worker declared BLOCKED in its final message."""
    msg = result.last_message or result.result_text
    for line in msg.splitlines():
        s = line.strip()
        if s.upper().startswith("BLOCKED:"):
            return s[len("BLOCKED:"):].strip()
    return None
