"""The FastAPI app. Pages are GET only; actions (when DASHBOARD_ACTIONS is on) are POST /action/...
A page is a shell whose section HTMX loads and refreshes.

  /, /board, /pipeline, /planner, /explorer, /services, /settings   full pages (tabs)
  /section/<tab>                              the tab's content, polled every DASHBOARD_REFRESH_SEC
  /api/<source>.json                          a source as JSON (pipeline, board, planner, explorer)
  /healthz                                    liveness, for a monitor
  POST /action/explorer/{run,answer,batch}, /action/pipeline/retry, /action/service/restart, /action/settings/save
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from ..config import Config
from . import settings, views
from .actions import Actions, Result
from .sources import Sources

HERE = Path(__file__).parent
TABS = ("overview", "board", "pipeline", "planner", "explorer", "services", "settings")
FINDING_FILTERS = ("open", "escalated", "fix_pending", "fix_merged", "resolved", "dismissed", "all")


def safe_url(value) -> str:
    """Only http(s) links from source data reach an href."""
    v = str(value or "")
    return v if v.startswith(("https://", "http://")) else ""


def create_app(cfg: Config, sources: Sources | None = None,
               actions: Actions | None = None) -> FastAPI:
    app = FastAPI(title="claude-pipeline dashboard", docs_url=None, redoc_url=None, openapi_url=None)
    src = sources or Sources(cfg)
    templates = Jinja2Templates(directory=str(HERE / "templates"))
    env = templates.env
    env.filters.update(ago=views.ago, until=views.until, money=views.money, url=safe_url,
                       short=lambda v: str(v or "")[:7] or "-", options=views.question_options)
    acts = actions or Actions(cfg, src)
    env.globals.update(refresh=cfg.dashboard_refresh_sec, tabs=TABS, actions=cfg.dashboard_actions)
    app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")

    def page(request: Request, tab: str) -> HTMLResponse:
        query = f"?{request.url.query}" if request.url.query else ""
        return templates.TemplateResponse(request, "page.html", {"tab": tab, "query": query})

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request):
        return page(request, "overview")

    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    @app.get("/api/{name}.json")
    def api(name: str):
        if name not in Sources.NAMES:
            raise HTTPException(404)
        r = src.get(name)
        return JSONResponse({"source": name, "configured": r.configured, "ok": r.ok, "error": r.error,
                             "fetched_at": r.fetched_at, "data": r.data})

    def settings_cards() -> list[dict]:
        cards = []
        for service, items in settings.ALLOWED.items():
            configured = settings.path_of(cfg, service) is not None
            current = settings.read(cfg, service) if configured else {}
            rows = [{"s": s, "value": current.get(s.key)} for s in items]
            cards.append({"service": service, "configured": configured, "rows": rows})
        return cards

    @app.get("/section/{tab}", response_class=HTMLResponse)
    def section(request: Request, tab: str, state: str = "open"):
        if tab not in TABS:
            raise HTTPException(404)
        ctx: dict = {"tab": tab, "now": dt.datetime.now(dt.UTC)}
        if tab == "overview":
            results = src.all()
            ctx.update(results=results, health=views.health(results), spend=views.spend(results),
                       alerts=views.alerts(results),
                       recent=acts.recent(20) if cfg.dashboard_actions else [])
        elif tab == "settings":
            ctx["cards"] = settings_cards()
        else:
            name = "board" if tab == "board" else tab
            ctx["res"] = src.get(name)
            if tab == "explorer" and ctx["res"].ok:
                state = state if state in FINDING_FILTERS else "open"
                rows, counts = views.findings(ctx["res"].data, state)
                ctx.update(findings=rows, counts=counts, state=state, filters=FINDING_FILTERS)
                ctx["lanes"] = views.lanes(ctx["res"].data)
        return templates.TemplateResponse(request, f"{tab}.html", ctx)

    # --- actions: POST only, 404 when DASHBOARD_ACTIONS is off ----------------------------------
    def act(request: Request, name: str, target: str, fn, extra: dict | None = None) -> HTMLResponse:
        if not cfg.dashboard_actions:
            raise HTTPException(404)
        try:
            result = fn()
        except Exception as e:  # noqa: BLE001 - an action never answers with a 500
            result = Result(False, False, f"failed: {e}"[:300])
        acts.log(request.client.host if request.client else "-", name, target, result)
        return templates.TemplateResponse(request, "_action_result.html", {"result": result, **(extra or {})})

    @app.post("/action/explorer/run", response_class=HTMLResponse)
    def explorer_run(request: Request, lane: str = Form("")):
        return act(request, "explorer/run", lane or "default", lambda: acts.explorer_run(lane or None))

    @app.post("/action/explorer/answer", response_class=HTMLResponse)
    def explorer_answer(request: Request, question: str = Form(""), letter: str = Form("")):
        return act(request, "explorer/answer", f"{question} {letter}".strip(),
                   lambda: acts.explorer_answer(question, letter))

    @app.post("/action/explorer/batch", response_class=HTMLResponse)
    def explorer_batch(request: Request, action: str = Form(""), batch: str = Form("")):
        return act(request, f"explorer/batch/{action}"[:60], batch,
                   lambda: acts.explorer_batch(action, batch))

    @app.post("/action/pipeline/retry", response_class=HTMLResponse)
    def pipeline_retry(request: Request, task: str = Form("")):
        def go() -> Result:
            if not task.isascii() or not task.isdigit():
                return Result(False, True, "not a task id")
            return acts.pipeline_retry(int(task))
        return act(request, "pipeline/retry", task, go)

    @app.post("/action/service/restart", response_class=HTMLResponse)
    def service_restart(request: Request, unit: str = Form("")):
        return act(request, "service/restart", unit, lambda: acts.restart(unit))

    @app.post("/action/settings/save", response_class=HTMLResponse)
    def settings_save(request: Request, service: str = Form(""), key: str = Form(""),
                      value: str = Form("")):
        known = settings.find(service, key)
        target = f"{service}:{key}" if known else "unknown"
        extra = {"restart": known.restart if known else None}
        return act(request, "settings/save", target,
                   lambda: acts.save_setting(service, key, value), extra)

    @app.get("/{tab}", response_class=HTMLResponse)
    def tab_page(request: Request, tab: str):
        if tab not in TABS:
            raise HTTPException(404)
        return page(request, tab)

    return app


def serve(cfg: Config) -> int:
    import uvicorn

    uvicorn.run(create_app(cfg), host=cfg.dashboard_host, port=cfg.dashboard_port,
                log_level="warning", access_log=False)
    return 0
