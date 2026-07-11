from __future__ import annotations

from typing import Any, Dict, Optional

from .models import SolveRequest
from .service import SolverService

try:
    from fastapi import FastAPI
    from pydantic import BaseModel
except Exception:  # pragma: no cover - optional until deps installed
    FastAPI = None  # type: ignore
    BaseModel = object  # type: ignore


def create_app(service: Optional[SolverService] = None):
    if FastAPI is None:
        raise RuntimeError(
            "fastapi is not installed. Install turnstile_solver/requirements.txt first."
        )

    service = service or SolverService()
    app = FastAPI(title="Turnstile Solver", version="0.1.0")

    class SolveBody(BaseModel):  # type: ignore[misc,valid-type]
        proxy: str = ""
        parent_proxy: str = ""
        page_url: str = ""
        timeout_sec: int = 180
        headless: bool = False
        user_agent: str = ""

    @app.get("/health")
    def health() -> Dict[str, Any]:
        return service.health()

    @app.post("/v1/solve")
    def solve(body: SolveBody) -> Dict[str, Any]:
        result = service.solve(
            SolveRequest(
                proxy=body.proxy,
                page_url=body.page_url,
                timeout_sec=body.timeout_sec,
                headless=body.headless,
                user_agent=body.user_agent,
                metadata={"parent_proxy": body.parent_proxy},
            )
        )
        return result.to_dict()

    return app
