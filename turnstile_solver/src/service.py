from __future__ import annotations

from typing import Optional

from .config import SolverConfig, load_config
from .models import SolveRequest, SolveResult
from .pool import WorkerPool


class SolverService:
    def __init__(self, config: Optional[SolverConfig] = None):
        self.config = config or SolverConfig()
        self.pool = WorkerPool(self.config)

    @classmethod
    def from_config_path(cls, path: Optional[str] = None) -> "SolverService":
        return cls(load_config(path))

    def health(self) -> dict:
        return {
            "ok": True,
            "service": "turnstile_solver",
            "version": "0.1.0",
            "config": {
                "host": self.config.host,
                "port": self.config.port,
                "max_concurrency": self.config.max_concurrency,
                "headless": self.config.headless,
                "signup_url": self.config.signup_url,
            },
            "pool": self.pool.stats.to_dict(),
        }

    def solve(self, request: SolveRequest) -> SolveResult:
        if not request.page_url:
            request.page_url = self.config.signup_url
        if request.timeout_sec <= 0:
            request.timeout_sec = self.config.browser_timeout_sec
        return self.pool.solve(request)
