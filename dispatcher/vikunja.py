"""Vikunja REST client.

Only the endpoints the dispatcher needs, hitting the API directly (the daemon runs
without the MCP server). Verified against Vikunja's API:

  GET  /projects/{id}/views                              -> find the kanban view
  GET  /projects/{id}/views/{view}/buckets              -> buckets, each embeds `tasks`
  POST /projects/{id}/views/{view}/buckets/{b}/tasks    -> move a task to a bucket
  GET  /tasks/{id}                                       -> task detail
  GET  /tasks/{id}/comments                              -> read comments
  PUT  /tasks/{id}/comments        {comment}             -> add a comment
  GET  /labels ; PUT /labels {title}                     -> list / create labels
  PUT  /tasks/{id}/labels          {label_id}            -> apply a label
"""

from __future__ import annotations

import requests

from .log import get

log = get("vikunja")


class VikunjaError(RuntimeError):
    pass


class Vikunja:
    def __init__(self, base_url: str, token: str, timeout: int = 30):
        self.base = base_url.rstrip("/")
        self.timeout = timeout
        self.s = requests.Session()
        self.s.headers.update(
            {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        )

    def _req(self, method: str, path: str, **kw):
        url = f"{self.base}{path}"
        r = self.s.request(method, url, timeout=self.timeout, **kw)
        if not r.ok:
            raise VikunjaError(f"{method} {path} -> {r.status_code}: {r.text[:300]}")
        if r.text.strip():
            return r.json()
        return None

    # --- views & buckets -------------------------------------------------
    def kanban_view_id(self, project_id: int) -> int:
        views = self._req("GET", f"/projects/{project_id}/views")
        for v in views:
            if v.get("view_kind") == "kanban":
                return v["id"]
        raise VikunjaError(f"project {project_id} has no kanban view")

    def buckets(self, project_id: int, view_id: int) -> list[dict]:
        """Bucket metadata (id/title/limit) for a view. Does NOT embed tasks."""
        return self._req("GET", f"/projects/{project_id}/views/{view_id}/buckets") or []

    def board(self, project_id: int, view_id: int) -> list[dict]:
        """The kanban board: a list of buckets, each with an embedded `tasks` array.

        The `/buckets` endpoint returns bucket metadata only; the per-view
        `/tasks` endpoint is what carries tasks grouped into their buckets.
        """
        return self._req("GET", f"/projects/{project_id}/views/{view_id}/tasks") or []

    def bucket_map(self, project_id: int, view_id: int) -> dict[str, dict]:
        """Map lowercased bucket title -> bucket dict."""
        return {b["title"].strip().lower(): b for b in self.buckets(project_id, view_id)}

    def tasks_in_bucket(self, bucket: dict) -> list[dict]:
        return bucket.get("tasks") or []

    def move_task(self, project_id: int, view_id: int, bucket_id: int, task_id: int) -> None:
        self._req(
            "POST",
            f"/projects/{project_id}/views/{view_id}/buckets/{bucket_id}/tasks",
            json={"task_id": task_id, "bucket_id": bucket_id, "project_view_id": view_id},
        )

    # --- tasks -----------------------------------------------------------
    def task(self, task_id: int) -> dict:
        return self._req("GET", f"/tasks/{task_id}")

    def task_labels(self, task: dict) -> set[str]:
        return {l["title"].strip().lower() for l in (task.get("labels") or [])}

    def comments(self, task_id: int) -> list[dict]:
        return self._req("GET", f"/tasks/{task_id}/comments") or []

    def has_comment_prefix(self, task_id: int, prefix: str) -> bool:
        """True if any comment starts with `prefix` (used to detect an existing claim)."""
        for c in self.comments(task_id):
            body = (c.get("comment") or "").strip()
            # Vikunja stores comments as HTML; strip a leading <p> for the check.
            if body.lower().replace("<p>", "").startswith(prefix.lower()):
                return True
        return False

    def comment(self, task_id: int, text: str) -> None:
        self._req("PUT", f"/tasks/{task_id}/comments", json={"comment": text})

    # --- labels ----------------------------------------------------------
    def labels(self) -> list[dict]:
        return self._req("GET", "/labels") or []

    def ensure_label(self, title: str) -> int:
        for l in self.labels():
            if l["title"].strip().lower() == title.strip().lower():
                return l["id"]
        created = self._req("PUT", "/labels", json={"title": title})
        return created["id"]

    def apply_label(self, task_id: int, label_id: int) -> None:
        try:
            self._req("PUT", f"/tasks/{task_id}/labels", json={"label_id": label_id})
        except VikunjaError as e:
            # Already-applied is fine.
            log.debug("apply_label(%s) ignored: %s", task_id, e)
