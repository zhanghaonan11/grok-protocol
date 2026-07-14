from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from cpa_inspector.constants import APP_SUBTITLE, APP_TITLE
from cpa_inspector.services.jobs import JobManager
from cpa_inspector.services.profile_store import ProfileStore
from cpa_inspector.services.workspace import WorkspaceService
from cpa_inspector.state import AppState
from cpa_inspector.web.routes import api as api_routes
from cpa_inspector.web.routes import pages as page_routes


def _static_dir() -> Path:
    path = Path(__file__).resolve().parent / "static"
    path.mkdir(parents=True, exist_ok=True)
    return path


def attach_cpa(
    app: FastAPI,
    *,
    profile_store: ProfileStore | None = None,
    state: AppState | None = None,
    job_manager: JobManager | None = None,
) -> FastAPI:
    """把 CPA 巡检路由/静态资源挂到已有 FastAPI 应用上。"""
    store = profile_store or ProfileStore()
    app_state = state or AppState()
    if state is None:
        app_state.settings = store.load_app_settings()
    jobs = job_manager or JobManager()
    workspace = WorkspaceService(state=app_state, store=store)

    # 与独立运行时相同的 state 键，供 cpa 路由读取
    app.state.profile_store = store
    app.state.app_state = app_state
    app.state.job_manager = jobs
    app.state.workspace = workspace

    # 避免与主 WebUI /static 冲突
    app.mount("/static/cpa", StaticFiles(directory=str(_static_dir())), name="cpa_static")
    app.include_router(api_routes.router)
    app.include_router(page_routes.router)
    return app


def create_app(
    *,
    profile_store: ProfileStore | None = None,
    state: AppState | None = None,
    job_manager: JobManager | None = None,
) -> FastAPI:
    """独立启动 CPA 巡检台时使用。"""
    app = FastAPI(title=APP_TITLE, description=APP_SUBTITLE)
    attach_cpa(
        app,
        profile_store=profile_store,
        state=state,
        job_manager=job_manager,
    )

    @app.get("/", include_in_schema=False)
    def _root_redirect() -> RedirectResponse:
        return RedirectResponse(url="/cpa", status_code=307)

    @app.get("/favicon.ico", include_in_schema=False)
    def _favicon() -> FileResponse:
        return FileResponse(_static_dir() / "favicon.ico", media_type="image/x-icon")

    return app
