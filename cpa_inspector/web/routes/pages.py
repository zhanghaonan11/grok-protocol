from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from cpa_inspector.constants import APP_SUBTITLE, APP_TITLE

router = APIRouter()

_TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))


@router.get("/cpa", response_class=HTMLResponse)
def index(request: Request) -> HTMLResponse:
    """单页工作台入口（合并进 grok 后的统一路径）。"""
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "app_title": APP_TITLE,
            "app_subtitle": APP_SUBTITLE,
            "active": "cpa",
        },
    )
