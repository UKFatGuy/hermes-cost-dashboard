"""
Hermes Cost Dashboard — live LLM spending tracker.

Usage:
    python -m hermes_cost_dashboard.main
    # or: hermes-cost-dashboard (after pip install -e .)
"""

import os
import sys
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from jinja2 import Environment, FileSystemLoader

from . import db

app = FastAPI(title="Hermes Cost Dashboard")

HERE = Path(__file__).parent
TEMPLATES = HERE / "templates"

env = Environment(
    loader=FileSystemLoader(str(TEMPLATES)),
    auto_reload=True,
)


@app.get("/api/summary")
def api_summary(days: int = Query(30, ge=1, le=365)):
    return db.get_summary(days)


@app.get("/api/sessions")
def api_sessions(limit: int = Query(20, ge=1, le=100)):
    return {"sessions": db.get_recent_sessions(limit)}


@app.get("/api/profiles")
def api_profiles(days: int = Query(30, ge=1, le=365)):
    return {"profiles": db.get_profile_cost_summary(days)}


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    template = env.get_template("dashboard.html")
    return HTMLResponse(template.render())


def run():
    port = int(os.environ.get("COST_DASHBOARD_PORT", "8080"))
    host = os.environ.get("COST_DASHBOARD_HOST", "127.0.0.1")
    print(
        f"\n  📊 Hermes Cost Dashboard\n"
        f"  ─────────────────────\n"
        f"  Open: http://{host}:{port}\n"
        f"  Ctrl+C to stop\n"
    )
    uvicorn.run(app, host=host, port=port, log_level="warning")


if __name__ == "__main__":
    run()
