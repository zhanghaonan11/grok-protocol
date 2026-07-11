from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass
from typing import Dict, Optional


class SubmitPermitError(RuntimeError):
    pass


@dataclass(frozen=True)
class SubmitPermit:
    permit_id: str
    acquired_at_ms: int
    expires_at_ms: int
    lease_sec: int

    def to_dict(self) -> dict:
        return {
            "ok": True,
            "permit_id": self.permit_id,
            "acquired_at_ms": self.acquired_at_ms,
            "expires_at_ms": self.expires_at_ms,
            "lease_sec": self.lease_sec,
        }


class SubmitPermitRegistry:
    """Cross-process submission gate with expiring, fail-closed permits."""

    def __init__(self, workers: int, *, lease_sec: int = 120):
        self.workers = max(1, int(workers))
        self.default_lease_sec = max(1, int(lease_sec or 120))
        self._semaphore = threading.BoundedSemaphore(self.workers)
        self._lock = threading.Lock()
        self._active: Dict[str, SubmitPermit] = {}
        self._closed = False
        self._expired_total = 0
        self._reaped_total = 0

    def reap_expired(self, now_ms: Optional[int] = None) -> int:
        """Reclaim expired slots. ``now_ms`` exists for deterministic callers/tests."""
        now = int(now_ms if now_ms is not None else time.time() * 1000)
        with self._lock:
            expired_ids = [
                permit_id
                for permit_id, permit in self._active.items()
                if now >= permit.expires_at_ms
            ]
            for permit_id in expired_ids:
                self._active.pop(permit_id, None)
            self._expired_total += len(expired_ids)

        reaped = 0
        for _ in expired_ids:
            try:
                self._semaphore.release()
                reaped += 1
            except ValueError:
                break
        if reaped:
            with self._lock:
                self._reaped_total += reaped
        return reaped

    def _next_expiry_delay(self, now_ms: int) -> Optional[float]:
        with self._lock:
            if not self._active:
                return None
            expires_at_ms = min(permit.expires_at_ms for permit in self._active.values())
        return max(0.0, (expires_at_ms - now_ms) / 1000.0)

    def acquire(self, timeout_sec: int, lease_sec: int = 0) -> SubmitPermit:
        timeout = max(0.0, float(timeout_sec or 0))
        effective_lease_sec = max(1, int(lease_sec or self.default_lease_sec))
        deadline = time.monotonic() + timeout

        while True:
            now_ms = int(time.time() * 1000)
            self.reap_expired(now_ms)
            with self._lock:
                if self._closed:
                    raise SubmitPermitError("submit permit registry is closed")

            if timeout <= 0:
                acquired = self._semaphore.acquire(blocking=False)
            else:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise SubmitPermitError("submit permit acquire timeout")
                next_expiry = self._next_expiry_delay(now_ms)
                wait_sec = min(0.25, remaining)
                if next_expiry is not None:
                    wait_sec = min(wait_sec, max(0.001, next_expiry))
                acquired = self._semaphore.acquire(timeout=wait_sec)

            if not acquired:
                if timeout <= 0:
                    raise SubmitPermitError("submit permit acquire timeout")
                continue

            acquired_at_ms = int(time.time() * 1000)
            permit = SubmitPermit(
                permit_id=uuid.uuid4().hex,
                acquired_at_ms=acquired_at_ms,
                expires_at_ms=acquired_at_ms + effective_lease_sec * 1000,
                lease_sec=effective_lease_sec,
            )
            with self._lock:
                if self._closed:
                    self._semaphore.release()
                    raise SubmitPermitError("submit permit registry is closed")
                self._active[permit.permit_id] = permit
            return permit

    def release(self, permit_id: str) -> dict:
        self.reap_expired()
        key = str(permit_id or "").strip()
        with self._lock:
            permit = self._active.pop(key, None)
            if permit is None:
                raise SubmitPermitError("submit permit not found, expired, or already released")
        self._semaphore.release()
        return {
            "ok": True,
            "permit_id": key,
            "released_at_ms": int(time.time() * 1000),
        }

    def stats(self) -> dict:
        self.reap_expired()
        with self._lock:
            return {
                "workers": self.workers,
                "active": len(self._active),
                "available": max(0, self.workers - len(self._active)),
                "default_lease_sec": self.default_lease_sec,
                "expired": self._expired_total,
                "reaped": self._reaped_total,
                "closed": self._closed,
            }

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            active_count = len(self._active)
            self._active.clear()
        for _ in range(active_count):
            try:
                self._semaphore.release()
            except ValueError:
                break


# Backward-compatible name for callers created before permit leases were added.
SubmitPermitPool = SubmitPermitRegistry
