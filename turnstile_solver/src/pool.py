from __future__ import annotations

from typing import Optional

from .browser_runtime import PersistentBrowserPool
from .browser_worker import BrowserWorker
from .config import SolverConfig


class WorkerPool(PersistentBrowserPool):
    """Backward-compatible name for the persistent Chromium pool."""

    def __init__(self, config: SolverConfig, worker: Optional[BrowserWorker] = None):
        super().__init__(config, worker=worker)
