from __future__ import annotations

import threading
from typing import Optional

from .browser_worker import BrowserWorker
from .config import SolverConfig
from .models import PoolStats, SolveRequest, SolveResult


class WorkerPool:
    """Simple concurrency gate for browser solves."""

    def __init__(self, config: SolverConfig, worker: Optional[BrowserWorker] = None):
        self.config = config
        self.worker = worker or BrowserWorker(config)
        self._sem = threading.BoundedSemaphore(value=max(1, int(config.max_concurrency)))
        self._lock = threading.Lock()
        self.stats = PoolStats(max_concurrency=max(1, int(config.max_concurrency)))

    def solve(self, request: SolveRequest) -> SolveResult:
        acquired = self._sem.acquire(timeout=max(1, int(request.timeout_sec or 180)))
        if not acquired:
            return SolveResult(ok=False, error="solver pool busy: acquire timeout")

        with self._lock:
            self.stats.active_workers += 1
        try:
            result = self.worker.solve(request)
            with self._lock:
                if result.ok:
                    self.stats.completed += 1
                else:
                    self.stats.failed += 1
                    self.stats.last_error = result.error
            return result
        except Exception as exc:  # pragma: no cover - defensive
            with self._lock:
                self.stats.failed += 1
                self.stats.last_error = str(exc)
            return SolveResult(ok=False, error=str(exc))
        finally:
            with self._lock:
                self.stats.active_workers = max(0, self.stats.active_workers - 1)
            self._sem.release()
