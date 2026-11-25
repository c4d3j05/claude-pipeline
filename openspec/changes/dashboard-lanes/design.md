# Design

## The data (produced by explorer; this is the contract the dashboard reads)

`explorer status --json` → top-level key `lanes`, a list in display order:

```json
"lanes": [
  {"lane": "accounts-contacts",
   "focus": null,
   "last_run": "2026-11-25T06:02:11+00:00",
   "next_run": "2026-11-26T06:00:00+00:00",
   "spent_today": 3.2,
   "budget": 10.0,
   "open_findings": 4},
  {"lane": "billing",
   "focus": {"batch": "B-4", "settled": 2, "total": 6},
   "last_run": null,
   "next_run": null,
   "spent_today": 0,
   "budget": 10.0,
   "open_findings": 9}
]
```

| Key | Type | Meaning |
|---|---|---|
| `lane` | string, slug `^[a-z][a-z0-9-]{0,39}$` | the lane's id; shown as is |
| `focus` | `null` or object `{batch: string, settled: int, total: int}` | the lane's batch in flight and how many of its findings are settled |
| `last_run` | ISO-8601 string with offset, or `null` | when the lane's last run ended; `null` = never |
| `next_run` | ISO-8601 string with offset, or `null` | the lane's next scheduled daily run; `null` = none scheduled |
| `spent_today` | number (USD) | spent by this lane today |
| `budget` | number (USD) | the lane's daily share |
| `open_findings` | int | findings of this lane that are not resolved or dismissed |

Unknown keys are ignored.

## The helper

```python
def lanes(ex: dict) -> list[dict]:
    """Rows for the Lanes table, in the order explorer gives them. [] hides the table."""
```

- `ex` is the explorer status dict (`res.data`). Returns `[]` when `ex.get("lanes")` is missing, is not a
  list, or is empty.
- An entry that is not a dict, or whose `lane` is not a non-empty string, is skipped.
- Each row is a dict with exactly these keys:
  - `lane`: the string;
  - `focus`: `"B-4 · 2/6"` when `focus` is a dict with a non-empty `batch` (missing or non-int `settled` /
    `total` count as 0), else `"—"` (an em dash);
  - `last_run`, `next_run`: the raw values (rendered by filters);
  - `spend`: `f"{money(spent_today)} / {money(budget)}"`, for example `"$3.20 / $10.00"`; `money` is the
    existing helper in this file and already prints `-` for a non-number;
  - `over`: `True` when both are numbers, `budget > 0` and `spent_today >= budget`, else `False`;
  - `open_findings`: the int, or `0` when it is not an int.

```python
def until(value, now: dt.datetime | None = None) -> str:
    """A future time as "in 3 h"; "due" when it has passed; "-" when unreadable."""
```

It mirrors the existing `ago()` just above it (same `parse_ts`, same units `d`, `h`, `min`, else seconds):
`in 2 d`, `in 3 h`, `in 12 min`, `in 40 s`; a time at or before `now` gives `due`; `None` or an unparsable
value gives `-`. (`ago()` cannot be reused for `next_run`: it prints `just now` for any future time.)

## Wiring

- `dispatcher/web/app.py`, `create_app`: add `until=views.until` to the `env.filters.update(...)` call.
- Same file, `section()`: inside the existing `if tab == "explorer" and ctx["res"].ok:` block add
  `ctx["lanes"] = views.lanes(ctx["res"].data)`.
- `dispatcher/web/templates/explorer.html`: directly after the first `</section>` (the stat cards, line 14)
  and before the `<section class="card">` that holds `<h2>Findings</h2>`, add:

```html
{% if lanes %}
<section class="card">
  <h2>Lanes <span class="count">{{ lanes|length }}</span></h2>
  <table>
    <thead><tr><th>Lane</th><th>Focus</th><th>Last run</th><th>Next run</th><th>Today</th><th>Open findings</th></tr></thead>
    <tbody>
    {% for l in lanes %}
    <tr><td class="mono">{{ l.lane }}</td><td class="mono">{{ l.focus }}</td>
        <td class="muted small">{{ l.last_run|ago }}</td><td class="muted small">{{ l.next_run|until }}</td>
        <td class="mono{{ ' alert-inline' if l.over }}">{{ l.spend }}</td><td class="mono">{{ l.open_findings }}</td></tr>
    {% endfor %}
    </tbody>
  </table>
</section>
{% endif %}
```

Jinja autoescaping is already on for these templates, so the lane text needs no extra escaping. The
`card`, `mono`, `muted`, `small`, `count` and `alert-inline` classes already exist in
`dispatcher/web/static/style.css`; no CSS change.
