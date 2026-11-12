"""The `Touches:` line and the files a PR really changed."""

from __future__ import annotations

import subprocess

from dispatcher import pr as prmod
from dispatcher import touches
from dispatcher.db import Ledger
from dispatcher.prompt import acceptance_criteria

DONE = "### Definition of Done\n- [ ] saving works\n\nSource: https://x.slack.com/archives/D1/p10 · investigation a1\n"


def test_touches_line_is_read_after_the_source_line():
    d = DONE + "Touches: backend/app/a.py, frontend/src/views/, backend/app/b/migrations/0002_x.py"
    assert touches.parse(d) == ["backend/app/a.py", "frontend/src/views/", "backend/app/b/migrations/0002_x.py"]


def test_touches_in_an_html_description():
    assert touches.parse("<p>Source: https://x</p><p>Touches: a.py, b/</p>") == ["a.py", "b/"]


def test_no_touches_line_means_nothing_planned():
    assert touches.parse(DONE) == [] and touches.parse(None) == [] and touches.parse("") == []
    assert touches.parse("mentions Touches: in a sentence") == []


def test_the_touches_line_is_not_an_acceptance_criterion():
    assert acceptance_criteria(DONE + "Touches: a.py") == ["[ ] saving works"]


def test_touches_round_trip_and_bad_data(tmp_path):
    assert touches.load(touches.dump(["a", "b/"])) == ["a", "b/"]
    assert touches.dump([]) is None and touches.load(None) == [] and touches.load("not json") == []
    led = Ledger(tmp_path / "p.db")
    led.upsert_claim(7, "s", "s")
    led.update(7, touches=touches.dump(["a.py"]), changed=touches.dump(["b.py"]))
    row = led.get(7)
    assert touches.load(row["touches"]) == ["a.py"] and touches.load(row["changed"]) == ["b.py"]
    led.upsert_claim(7, "s", "s")                                          # a requeued task starts over
    row = led.get(7)
    assert row["touches"] is None and row["changed"] is None


def test_changed_files_lists_what_the_branch_changed(tmp_path):
    def git(*a):
        subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", *a], cwd=tmp_path, check=True,
                       capture_output=True)
    git("init", "-q", "-b", "main")
    (tmp_path / "keep.txt").write_text("1")
    git("add", "."); git("commit", "-qm", "base")
    git("checkout", "-qb", "work")
    (tmp_path / "keep.txt").write_text("2")
    (tmp_path / "sub").mkdir(); (tmp_path / "sub/new.py").write_text("x")
    git("add", "."); git("commit", "-qm", "work")
    assert sorted(prmod.changed_files(tmp_path, "main")) == ["keep.txt", "sub/new.py"]
    assert prmod.changed_files(tmp_path, "no-such-ref") == []
