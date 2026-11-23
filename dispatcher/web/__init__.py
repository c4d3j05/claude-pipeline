"""Read-only web dashboard (`claude-pipeline web`), OpenSpec change `web-dashboard`.

One page per area (overview, board, pipeline, planner, explorer), each section refreshed by HTMX.
Nothing here writes: the pipeline's ledger and Vikunja are only read, and claude-planner and
explorer are asked for `status --json` (shape: the integration spec, "Status JSON"). FastAPI,
Jinja2 and uvicorn come from the optional `web` dependency group.
"""
