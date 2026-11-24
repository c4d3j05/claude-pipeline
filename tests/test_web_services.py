"""The dashboard's Services tab: what `systemctl --user status` shows, read-only."""

from __future__ import annotations

import subprocess

from fastapi.testclient import TestClient

from dispatcher.web import services as svc
from dispatcher.web import views
from dispatcher.web.app import create_app
from dispatcher.web.sources import Result

from .test_web import cfg, fake_sources, results  # noqa: F401 - cfg is a fixture

SHOW = """Id=claude-pipeline-dispatcher.service
Description=claude-pipeline dispatcher
LoadState=loaded
ActiveState=active
SubState=running
UnitFileState=enabled
Result=success
ActiveEnterTimestamp=Sun 2026-11-22 15:45:46 EST
InactiveEnterTimestamp=
MainPID=3085994
MemoryCurrent=1932735283
CPUUsageNSec=14216000000000
NRestarts=0
ControlGroup=/user.slice/user-1000.slice/user@1000.service/app.slice/claude-pipeline-dispatcher.service
LastTriggerUSec=
NextElapseUSecRealtime=

Id=claude-pipeline-nightly.timer
Description=nightly
LoadState=loaded
ActiveState=active
SubState=waiting
UnitFileState=enabled
Result=success
MainPID=0
MemoryCurrent=[not set]
CPUUsageNSec=[not set]
NRestarts=0
ControlGroup=
LastTriggerUSec=Sun 2026-11-22 03:00:01 EST
NextElapseUSecRealtime=Mon 2026-11-23 03:00:00 EST

Id=explorer-serve.service
Description=explorer daemon
LoadState=loaded
ActiveState=failed
SubState=failed
UnitFileState=enabled
Result=exit-code
ActiveEnterTimestamp=
InactiveEnterTimestamp=Sun 2026-11-22 20:00:00 EST
MainPID=0
MemoryCurrent=[not set]
CPUUsageNSec=[not set]
NRestarts=4
ControlGroup=

Id=nope.service
Description=nope.service
LoadState=not-found
ActiveState=inactive
SubState=dead
UnitFileState=
MainPID=0
"""

UNITS = ["claude-pipeline-dispatcher", "claude-pipeline-nightly.timer", "explorer-serve", "nope"]


def run_show(calls):
    def run(cmd, **kw):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, SHOW, "")
    return run


def test_read_services_reads_each_unit_in_order_without_touching_it():
    calls = []
    procs = lambda cg: [{"pid": 3235921, "args": "bash scripts/worker-test.sh"}] if cg else []
    out = svc.read_services(UNITS, run=run_show(calls), which=lambda _: "/bin/systemctl", procs=procs)
    assert calls[0][:4] == ["systemctl", "--user", "show", "--no-pager"] and calls[0][-4:] == UNITS
    assert not any(word in calls[0] for word in ("start", "stop", "restart", "reload", "kill"))
    d, t, e, n = out["units"]
    assert d["unit"] == "claude-pipeline-dispatcher.service" and d["active"] == "active" and d["sub"] == "running"
    assert d["main_pid"] == 3085994 and d["memory_mb"] == 1843.2 and d["cpu_sec"] == 14216
    assert d["since"] == "Sun 2026-11-22 15:45:46 EST" and d["processes"][0]["args"] == "bash scripts/worker-test.sh"
    assert t["is_timer"] and t["next_run"] == "Mon 2026-11-23 03:00:00 EST" and t["processes"] == []
    assert t["memory_mb"] is None and t["main_pid"] is None
    assert e["active"] == "failed" and e["restarts"] == 4 and e["since"] == "Sun 2026-11-22 20:00:00 EST"
    assert not n["installed"]


def test_no_systemd_is_not_an_error():
    out = svc.read_services(UNITS, run=lambda *a, **k: 1 / 0, which=lambda _: None)
    assert out == {"available": False, "units": []}


def test_processes_come_from_the_units_cgroup(tmp_path):
    cg = tmp_path / "cg" / "app.slice" / "x.service"
    (cg / "sub").mkdir(parents=True)
    (cg / "cgroup.procs").write_text("11\n12\n")
    (cg / "sub" / "cgroup.procs").write_text("13\n")
    for pid, args in ((11, b"uv\0run\0claude-pipeline\0run"), (13, b"python\0manage.py\0test\0" + b"x" * (svc.MAX_ARGS + 100))):
        (tmp_path / "proc" / str(pid)).mkdir(parents=True)
        (tmp_path / "proc" / str(pid) / "cmdline").write_bytes(args)
    procs = svc.processes("/app.slice/x.service", root=tmp_path / "cg", proc=tmp_path / "proc")
    assert [p["pid"] for p in procs] == [11, 13]                # 12 has gone: skipped
    assert procs[0]["args"] == "uv run claude-pipeline run"
    assert len(procs[1]["args"]) == svc.MAX_ARGS + 1 and procs[1]["args"].endswith("…")


def test_overview_alerts_and_health_for_units():
    data = svc.read_services(UNITS, run=run_show([]), which=lambda _: "x", procs=lambda cg: [])
    res = results(services=Result("services", data=data))
    texts = [a.text for a in views.alerts(res)]
    assert "explorer-serve.service has failed (exit-code)" in texts
    assert "nope.service is not installed" in texts
    assert not any("dispatcher" in t for t in texts)
    line = next(h for h in views.health(res) if h["service"] == "services")
    assert line["state"] == "down" and line["detail"] == "2 of 4 units active"


def test_services_tab_renders(cfg):  # noqa: F811
    data = svc.read_services(UNITS, run=run_show([]), which=lambda _: "x",
                             procs=lambda cg: [{"pid": 7, "args": "ntree run release-r-1 -- scripts/worker-test.sh"}])
    c = TestClient(create_app(cfg, fake_sources(cfg, services=lambda: data)))
    page = c.get("/services")
    assert page.status_code == 200 and 'hx-get="/section/services"' in page.text
    html = c.get("/section/services").text
    assert "claude-pipeline-dispatcher.service" in html and "ntree run release-r-1" in html
    assert "1843 MB" in html and "next Mon 2026-11-23 03:00:00 EST" in html and "not installed" in html
    off = TestClient(create_app(cfg, fake_sources(cfg, services=lambda: {"available": False, "units": []})))
    assert "No systemd on this host" in off.get("/section/services").text
