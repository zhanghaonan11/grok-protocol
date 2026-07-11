from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict


@dataclass
class SolveRequest:
    proxy: str = ""
    page_url: str = ""
    timeout_sec: int = 180
    headless: bool = False
    user_agent: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class SolveResult:
    ok: bool
    token: str = ""
    proxy: str = ""
    page_url: str = ""
    user_agent: str = ""
    elapsed_ms: int = 0
    error: str = ""
    extras: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "ok": self.ok,
            "token": self.token,
            "proxy": self.proxy,
            "page_url": self.page_url,
            "user_agent": self.user_agent,
            "elapsed_ms": self.elapsed_ms,
        }
        if self.error:
            payload["error"] = self.error
        if self.extras:
            payload["extras"] = self.extras
        return payload


@dataclass
class PoolStats:
    max_concurrency: int = 1
    active_workers: int = 0
    completed: int = 0
    failed: int = 0
    last_error: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "max_concurrency": self.max_concurrency,
            "active_workers": self.active_workers,
            "completed": self.completed,
            "failed": self.failed,
            "last_error": self.last_error,
        }
