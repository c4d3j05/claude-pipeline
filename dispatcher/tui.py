"""Textual control-panel UI. Imported lazily by dashboard.run() (requires `textual`)."""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Grid
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Footer, Input, Label, Static

from . import dashboard as dash
from .config import Config
from .db import Ledger

_STATE_COLOR = {
    "claimed": "yellow", "working": "cyan", "testing": "blue",
    "reviewing": "magenta", "pr_open": "green", "blocked": "red", "done": "grey58",
}

# Config knobs the panel exposes (env key -> label).
_CONFIG_FIELDS = [
    ("MAX_WORKERS", "Max workers"),
    ("DAILY_CAP_USD", "Daily cap $"),
    ("POLL_INTERVAL", "Poll interval s"),
    ("WORKER_MODEL", "Worker model"),
    ("REVIEW_MODEL", "Review model"),
    ("PERMISSION_MODE", "Permission mode"),
]


class ConfigScreen(ModalScreen):
    """Modal to edit the hot-reloadable knobs; saves to .env on confirm."""

    def __init__(self, cfg: Config):
        super().__init__()
        self._cfg = cfg

    def compose(self) -> ComposeResult:
        rows = [Label("Edit configuration (saved to .env, applied live)", id="cfg-title")]
        current = {
            "MAX_WORKERS": str(self._cfg.max_workers),
            "DAILY_CAP_USD": str(self._cfg.daily_cap_usd),
            "POLL_INTERVAL": str(self._cfg.poll_interval),
            "WORKER_MODEL": self._cfg.worker_model,
            "REVIEW_MODEL": self._cfg.review_model,
            "PERMISSION_MODE": self._cfg.permission_mode,
        }
        for key, label in _CONFIG_FIELDS:
            rows.append(Label(label))
            rows.append(Input(value=current[key], id=f"in-{key}"))
        rows.append(Button("Save", variant="success", id="save"))
        rows.append(Button("Cancel", variant="default", id="cancel"))
        yield Grid(*rows, id="cfg-grid")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel":
            self.dismiss(None)
            return
        updates = {}
        for key, _ in _CONFIG_FIELDS:
            val = self.query_one(f"#in-{key}", Input).value.strip()
            if val:
                updates[key] = val
        self.dismiss(updates)


class DashboardApp(App):
    CSS = """
    #summary { height: 3; padding: 1 2; background: $panel; }
    DataTable { height: 1fr; }
    #cfg-grid { grid-size: 1; padding: 1 2; width: 60; height: auto; background: $surface; border: thick $primary; }
    #cfg-title { padding-bottom: 1; }
    """
    BINDINGS: ClassVar = [
        Binding("q", "quit", "Quit"),
        Binding("p", "toggle_pause", "Pause/Resume"),
        Binding("r", "retry", "Retry"),
        Binding("c", "cancel", "Cancel"),
        Binding("o", "open_pr", "Open PR"),
        Binding("e", "config", "Config"),
    ]

    def __init__(self, cfg: Config, env_file: str):
        super().__init__()
        self.cfg = cfg
        self.env_file = env_file
        self.ledger = Ledger(cfg.db_path)
        self.logs_root = Path("logs")
        self._row_ids: list[int] = []

    def compose(self) -> ComposeResult:
        yield Static("", id="summary")
        yield DataTable(id="tasks", cursor_type="row", zebra_stripes=True)
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#tasks", DataTable)
        table.add_columns("Task", "State", "Elapsed", "Cost", "T/R", "PR", "Activity")
        self.refresh_data()
        self.set_interval(1.5, self.refresh_data)

    # --- refresh --------------------------------------------------------
    def refresh_data(self) -> None:
        # Re-read config so edits (ours or external) show live.
        from . import config as cfgmod
        self.cfg = cfgmod.load(self.env_file)
        st = dash.gather_status(self.cfg, self.ledger, self.logs_root)

        pause = "[b red]PAUSED[/]" if st["paused"] else "[b green]running[/]"
        cap_color = "red" if st["today_cost"] >= st["cap"] else "white"
        self.query_one("#summary", Static).update(
            f"[b]claude-pipeline[/]   workers [b]{st['active']}/{st['max_workers']}[/]   "
            f"spend [{cap_color}]${st['today_cost']:.2f}[/]/${st['cap']:.0f} today   {pause}\n"
            f"[dim]p pause · r retry · c cancel · o open PR · e config · q quit[/]"
        )

        table = self.query_one("#tasks", DataTable)
        prev = table.cursor_row
        table.clear()
        self._row_ids = []
        for t in st["tasks"]:
            state = t["state"] + (" ⊘" if t["cancel"] else "")
            pr = ("#" + t["pr_url"].rsplit("/", 1)[-1]) if t["pr_url"] else ""
            table.add_row(
                str(t["task_id"]),
                Text(state, style=_STATE_COLOR.get(t["state"], "white")),
                t["elapsed"],
                f"${t['cost']:.2f}",
                t["retries"],
                pr,
                t["activity"],
            )
            self._row_ids.append(t["task_id"])
        if self._row_ids:
            table.move_cursor(row=min(prev, len(self._row_ids) - 1))

    def _selected(self) -> dict | None:
        if not self._row_ids:
            return None
        idx = self.query_one("#tasks", DataTable).cursor_row
        if idx is None or idx >= len(self._row_ids):
            return None
        tid = self._row_ids[idx]
        row = self.ledger.get(tid)
        return dict(row) if row else None

    # --- actions --------------------------------------------------------
    def action_toggle_pause(self) -> None:
        paused = dash.toggle_pause(self.ledger)
        self.notify("Dispatching paused" if paused else "Dispatching resumed")
        self.refresh_data()

    def action_open_pr(self) -> None:
        sel = self._selected()
        if sel and sel.get("pr_url"):
            dash.open_pr(sel["pr_url"])
            self.notify(f"Opening {sel['pr_url']}")
        else:
            self.notify("No PR for the selected task", severity="warning")

    def action_cancel(self) -> None:
        sel = self._selected()
        if not sel:
            return
        if sel["state"] not in ("claimed", "working", "testing", "reviewing"):
            self.notify("Only an in-flight task can be cancelled", severity="warning")
            return
        dash.request_cancel(self.ledger, sel["task_id"])
        self.notify(f"Cancel requested for task {sel['task_id']}")
        self.refresh_data()

    def action_retry(self) -> None:
        sel = self._selected()
        if not sel:
            return
        if sel["state"] != "blocked":
            self.notify("Only a blocked task can be retried", severity="warning")
            return
        tid = sel["task_id"]
        try:
            msg = dash.retry_task(self.cfg, self.ledger, tid)
            self.notify(f"Task {tid}: {msg}")
        except Exception as e:  # noqa: BLE001
            self.notify(f"Retry failed: {e}", severity="error")
        self.refresh_data()

    def action_config(self) -> None:
        def _apply(updates: dict | None) -> None:
            if updates:
                dash.set_config(self.env_file, updates)
                self.notify(f"Updated {', '.join(updates)} (applied live)")
                self.refresh_data()

        self.push_screen(ConfigScreen(self.cfg), _apply)
