from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from threading import Lock
from typing import Iterable
from uuid import uuid4

from cpa_inspector.models import JobResult


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(slots=True)
class Job:
    job_id: str
    type: str
    status: str = "queued"
    current: int = 0
    total: int = 0
    message: str = ""
    results: list[JobResult] = field(default_factory=list)
    download_path: str | None = None
    created_at: datetime = field(default_factory=_utc_now)
    finished_at: datetime | None = None


class JobManager:
    """进程内任务进度表，供后续 API 轮询。"""

    _RUNNING_STATUSES = frozenset({"queued", "running"})

    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}
        self._lock = Lock()

    def create(self, type: str, total: int) -> Job:
        job = Job(
            job_id=uuid4().hex,
            type=str(type or "").strip() or "unknown",
            status="queued",
            current=0,
            total=max(0, int(total)),
            message="",
            results=[],
            download_path=None,
            created_at=_utc_now(),
            finished_at=None,
        )
        with self._lock:
            self._jobs[job.job_id] = job
        return job

    def update(
        self,
        job_id: str,
        current: int,
        message: str,
        results: Iterable[JobResult] | None = None,
    ) -> Job | None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return None
            job.current = max(0, int(current))
            job.message = str(message or "")
            if results is not None:
                job.results = list(results)
            if job.status == "queued":
                job.status = "running"
            return job

    def finish(self, job_id: str, status: str = "success") -> Job | None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return None
            job.status = str(status or "success").strip() or "success"
            job.finished_at = _utc_now()
            return job

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def has_running(self, type: str | None = None) -> bool:
        with self._lock:
            for job in self._jobs.values():
                if job.status not in self._RUNNING_STATUSES:
                    continue
                if type is None or job.type == type:
                    return True
            return False
