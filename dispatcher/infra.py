"""Telling "the environment is broken" apart from "the code is broken".

A red test gate is normally the worker's problem: it is resumed with the failure and asked to
fix it. But if the output shows the gate could not reach a service it needs (Postgres down
after a reboot, Docker not running, ...), no amount of code change can help, and every worker
retry is money spent on a problem the worker has no way to fix. This module recognises that
case from the gate's output so the dispatcher can stop (or just re-run the gate) instead.
"""

from __future__ import annotations

import os
import re
import socket
import subprocess
from dataclasses import dataclass

# (regex, service label). A regex may capture `host` and `port` so the service can be probed.
_SIGNATURES: list[tuple[re.Pattern, str]] = [
    # psycopg / django-tenants:  connection to server at "localhost" (::1), port 5432 failed: Connection refused
    (re.compile(r'connection to server at "?(?P<host>[\w.\-]+)"?[^\n]*?port (?P<port>\d+) failed:'
                r'[^\n]*?(?:Connection refused|timeout expired|Network is unreachable|'
                r'No route to host)', re.IGNORECASE), "database"),
    (re.compile(r'could not connect to server[^\n]*?(?:"(?P<host>[\w.\-]+)")?[^\n]*?port (?P<port>\d+)',
                re.IGNORECASE), "database"),
    (re.compile(r"Cannot connect to the Docker daemon", re.IGNORECASE), "Docker"),
    # generic sockets / HTTP clients: capture host:port when the message carries it
    (re.compile(r"(?:ConnectionRefusedError|Errno 111|ECONNREFUSED)[^\n]*?(?P<host>[\w.\-]+):(?P<port>\d{2,5})",
                re.IGNORECASE), "service"),
    (re.compile(r"Max retries exceeded with url[^\n]*?host='?(?P<host>[\w.\-]+)'?, port=(?P<port>\d+)[^\n]*?"
                r"(?:Connection refused|Failed to establish a new connection)", re.IGNORECASE), "service"),
    (re.compile(r"(?:Temporary failure in name resolution|Name or service not known)", re.IGNORECASE), "network/DNS"),
]


@dataclass(frozen=True)
class InfraFailure:
    service: str            # "database", "Docker", ...
    host: str | None = None
    port: int | None = None

    @property
    def where(self) -> str:
        return f"{self.host}:{self.port}" if self.host and self.port else self.service

    @property
    def summary(self) -> str:
        return f"cannot reach the {self.service}" + (f" at {self.where}" if self.host and self.port else "")

    def reachable(self, timeout: float = 3.0) -> bool | None:
        """True/False if the endpoint is known, None if there is nothing to probe."""
        if not (self.host and self.port):
            return None
        try:
            with socket.create_connection((self.host, self.port), timeout=timeout):
                return True
        except OSError:
            return False


def detect(output: str) -> InfraFailure | None:
    """The infrastructure failure shown in a gate's output, or None if it looks like a real
    test failure. Deliberately narrow: only connection-level errors count, never an assertion
    that merely mentions a word like "refused"."""
    for pattern, service in _SIGNATURES:
        m = pattern.search(output or "")
        if m:
            groups = m.groupdict()
            host = groups.get("host")
            port = int(groups["port"]) if groups.get("port") else None
            return InfraFailure(service, host, port)
    return None


def boot_id() -> str:
    """Identifies the current boot of the machine ('' if it cannot be determined).

    Cached gate results are only valid within one boot: a reboot can change the environment
    (a container that does not restart, a service that is down) without the code changing.
    """
    try:
        with open("/proc/sys/kernel/random/boot_id") as f:
            return f.read().strip()
    except OSError:
        pass
    try:  # macOS
        return subprocess.run(["sysctl", "-n", "kern.boottime"], capture_output=True, text=True,
                              timeout=5).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return os.environ.get("BOOT_ID", "")
