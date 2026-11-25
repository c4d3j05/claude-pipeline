Do the tasks in order. Run `uv run pytest tests/test_web.py` and `uv run ruff check dispatcher tests`
after each. The web tests need the `web` dependency group (`uv sync --group web`).

## 1. Helpers
- [x] 1.1 `dispatcher/web/views.py`: add `until(value, now=None)` directly after `ago()`, as in design.md.
      Test `tests/test_web.py::test_until_reads_future_times` with
      `now = dt.datetime(2026, 11, 25, 12, 0, tzinfo=dt.UTC)`:
      `"2026-11-25T15:00:00+00:00"` → `"in 3 h"`; `"2026-11-25T12:12:00+00:00"` → `"in 12 min"`;
      `"2026-11-27T12:00:00+00:00"` → `"in 2 d"`; `"2026-11-25T11:00:00+00:00"` → `"due"`; `None` → `"-"`; `"nope"` → `"-"`.
- [x] 1.2 Same file: add `lanes(ex)` after `findings()`, as in design.md.
      Test `test_lanes_rows`: with the two-lane example from design.md the result is two rows in that order;
      row 0 has `focus == "—"`, `spend == "$3.20 / $10.00"`, `over is False`, `open_findings == 4`;
      row 1 has `focus == "B-4 · 2/6"`, `spend == "$0.00 / $10.00"`.
      Test `test_lanes_tolerates_bad_data`: `lanes({}) == []`; `lanes({"lanes": "x"}) == []`;
      `lanes({"lanes": []}) == []`; `lanes({"lanes": [7, {"focus": None}, {"lane": "portal", "budget": 5, "spent_today": 5}]})`
      returns one row, lane `portal`, with `over is True`, `focus == "—"`, `open_findings == 0`.

## 2. The tab
- [x] 2.1 `dispatcher/web/app.py`: register the `until` filter and set `ctx["lanes"]` as in design.md ("Wiring").
- [x] 2.2 `dispatcher/web/templates/explorer.html`: add the Lanes card exactly where design.md says.
- [x] 2.3 Tests in `tests/test_web.py` (the module has an `EXPLORER` fixture dict and a `client(cfg, **override)`
      helper; pass `explorer=lambda: {...}` to override the explorer source):
      - `test_the_lanes_table_shows_each_lane`: explorer = `{**EXPLORER, "lanes": <the design.md example>}`;
        `GET /section/explorer` contains `<h2>Lanes`, `accounts-contacts`, `B-4 · 2/6`, `$3.20 / $10.00`, and
        the text `Lanes` appears before `<h2>Findings`.
      - `test_no_lanes_block_leaves_the_tab_as_it_was`: with the unchanged `EXPLORER` fixture the response
        does not contain `<h2>Lanes` and still contains `Export button does nothing`.
      - The existing `test_sections_render_the_sources` passes unchanged.

## 3. Docs and deploy
- [x] 3.1 `README.md`: in the dashboard tab table, add to the Explorer row "a Lanes table (focus, last and
      next run, today's spend against the lane's share, open findings) once explorer reports lanes".
- [ ] 3.2 Deploy (the owner): pull on the Debian host and `systemctl --user restart claude-pipeline-web`.
- [ ] 3.3 Before archiving: report `repowise risk main..HEAD` and `repowise impacted-tests main..HEAD`.
