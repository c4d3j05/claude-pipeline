"""The FastAPI app: GET routes only. A page is a shell whose section HTMX loads and refreshes.

  /, /board, /pipeline, /planner, /explorer, /services   full pages (tabs)
  /section/<tab>                              the tab's content, polled every DASHBOARD_REFRESH_SEC
  /api/<source>.json                          a source as JSON (pipeline, board, planner, explorer)
  /healthz                                    liveness, for a monitor
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from ..config import Config
from . import views
from .sources import Sources

HERE = Path(__file__).parent
TABS = ("overview", "board", "pipeline", "planner", "explorer", "services")
FINDING_FILTERS = ("open", "escalated", "fix_pending", "fix_merged", "resolved", "dismissed", "all")


def safe_url(value) -> str:
    """Only http(s) links from source data reach an href."""
    v = str(value or "")
    return v if v.startswith(("https://", "http://")) else ""


def create_app(cfg: Config, sources: Sources | None = None) -> FastAPI:
    app = FastAPI(title="claude-pipeline dashboard", docs_url=None, redoc_url=None, openapi_url=None)
    src = sources or Sources(cfg)
    templates = Jinja2Templates(directory=str(HERE / "templates"))
    env = templates.env
    env.filters.update(ago=views.ago, money=views.money, url=safe_url,
                       short=lambda v: str(v or "")[:7] or "-")
    env.globals.update(refresh=cfg.dashboard_refresh_sec, tabs=TABS)
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

    @app.get("/section/{tab}", response_class=HTMLResponse)
    def section(request: Request, tab: str, state: str = "open"):
        if tab not in TABS:
            raise HTTPException(404)
        ctx: dict = {"tab": tab, "now": dt.datetime.now(dt.UTC)}
        if tab == "overview":
            results = src.all()
            ctx.update(results=results, health=views.health(results), spend=views.spend(results),
                       alerts=views.alerts(results))
        else:
            name = "board" if tab == "board" else tab
            ctx["res"] = src.get(name)
            if tab == "explorer" and ctx["res"].ok:
                state = state if state in FINDING_FILTERS else "open"
                rows, counts = views.findings(ctx["res"].data, state)
                ctx.update(findings=rows, counts=counts, state=state, filters=FINDING_FILTERS)
        return templates.TemplateResponse(request, f"{tab}.html", ctx)

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
