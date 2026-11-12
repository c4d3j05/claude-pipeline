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


# --- overlap -----------------------------------------------------------------------------------
HOT = ("*/migrations/*", "*/router/*")


def test_same_file_and_directory_prefix_clash():
    assert touches.clashes(["a/b.py"], ["a/b.py"])
    assert touches.clashes(["a/"], ["a/b/c.py"]) and touches.clashes(["a/b/c.py"], ["a/"])
    assert touches.clashes(["./a/b.py"], ["a/b.py"])


def test_different_files_and_lookalike_prefixes_do_not_clash():
    assert not touches.clashes(["a/b.py"], ["a/c.py"])
    assert not touches.clashes(["app/"], ["application/x.py"])            # "app/" is not a prefix of it
    assert not touches.clashes(["a.py"], [])                              # no plan, no constraint
    assert not touches.clashes([], ["a.py"])


def test_two_migrations_clash_even_in_different_apps_files():
    a, b = ["backend/app/x/migrations/0002_a.py"], ["backend/app/x/migrations/0002_b.py"]
    assert touches.clashes(a, b, HOT) and not touches.clashes(a, b)
    assert touches.clashes(["frontend/src/router/index.ts"], ["frontend/src/router/index.ts"], HOT)
    assert not touches.clashes(["backend/app/x/migrations/0002_a.py"], ["backend/app/x/models.py"], HOT)


# --- the dispatcher rule -----------------------------------------------------------------------
def _ready(tid, *paths):
    from tests.test_gaps import _task
    t = _task(tid)
    t["description"] = DONE + (f"Touches: {', '.join(paths)}" if paths else "")
    return t


def _world(tmp_path, ready, held=()):
    from tests.test_gaps import _Board, _dispatcher
    d = _dispatcher(tmp_path)
    d.vk = _Board(ready)
    for tid, state, planned, changed in held:
        d.ledger.upsert_claim(tid, f"s{tid}", f"s{tid}")
        d.ledger.update(tid, state=state, touches=touches.dump(planned), changed=touches.dump(changed))
    return d


def ids(d, limit=5):
    return [t["id"] for t in d.ready_tasks(limit=limit)]


def test_a_task_sharing_files_with_work_in_progress_waits(tmp_path):
    d = _world(tmp_path, [_ready(2, "a/x.py")], held=[(1, "working", ["a/"], None)])
    assert ids(d) == []


def test_it_is_claimed_once_the_other_task_is_gone(tmp_path):
    d = _world(tmp_path, [_ready(2, "a/x.py")], held=[(1, "done", ["a/"], None)])
    assert ids(d) == [2]


def test_a_task_in_review_still_holds_its_files_and_actual_beats_planned(tmp_path):
    d = _world(tmp_path, [_ready(2, "a/x.py")], held=[(1, "pr_open", ["b/y.py"], ["a/x.py"])])
    assert ids(d) == []                                                   # the diff touched a/x.py
    d = _world(tmp_path, [_ready(2, "b/y.py")], held=[(1, "pr_open", ["b/y.py"], ["a/x.py"])])
    assert ids(d) == [2]                                                  # planned b/y.py was not changed


def test_the_next_ready_task_is_tried_when_the_first_waits(tmp_path):
    d = _world(tmp_path, [_ready(2, "a/x.py"), _ready(3, "c/z.py")], held=[(1, "working", ["a/x.py"], None)])
    assert ids(d) == [3]


def test_two_overlapping_ready_tasks_are_not_claimed_in_the_same_poll(tmp_path):
    d = _world(tmp_path, [_ready(2, "a/x.py"), _ready(3, "a/"), _ready(4, "d/w.py")])
    assert ids(d) == [2, 4]


def test_tasks_without_touches_are_never_held_back(tmp_path):
    d = _world(tmp_path, [_ready(2), _ready(3)], held=[(1, "working", ["a/"], None)])
    assert ids(d) == [2, 3]
    d = _world(tmp_path, [_ready(2, "a/x.py")], held=[(1, "working", [], None)])   # holder has no plan
    assert ids(d) == [2]


def test_migrations_serialise_tasks(tmp_path):
    d = _world(tmp_path, [_ready(2, "backend/app/x/migrations/0003_b.py")],
               held=[(1, "working", ["backend/app/y/migrations/0003_a.py"], None)])
    assert ids(d) == []
