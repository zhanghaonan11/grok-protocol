from __future__ import annotations

import threading
import time
from copy import copy
from dataclasses import dataclass, field
from typing import Dict, Optional, Set


@dataclass
class NodeSlot:
    id: str
    name: str
    server: str
    port: int
    protocol: str
    local_http: str
    raw: str = ""
    params: Dict[str, str] = field(default_factory=dict)
    uuid: str = ""
    healthy: bool = False
    ref_count: int = 0
    success_count: int = 0
    fail_count: int = 0
    last_latency_ms: Optional[float] = None
    cooldown_until: float = 0.0
    last_error: str = ""


@dataclass
class EmbeddedProxyConfig:
    """Placeholder config for later process lifecycle tasks."""

    binary_path: str = ""
    base_port: int = 28000
    max_nodes: int = 0
    health_interval_sec: float = 30.0
    fail_cooldown_sec: float = 30.0


class EmbeddedProxyManager:
    """In-memory node lease scheduler (no process start, no network)."""

    def __init__(self, config: Optional[EmbeddedProxyConfig] = None) -> None:
        self.config = config or EmbeddedProxyConfig()
        self._lock = threading.RLock()
        self._nodes: Dict[str, NodeSlot] = {}
        self._running = False

    def acquire(self, exclude_ids: Optional[Set[str]] = None) -> Optional[NodeSlot]:
        exclude = exclude_ids or set()
        now = time.time()
        with self._lock:
            candidates = [
                node
                for node in self._nodes.values()
                if node.healthy
                and now >= node.cooldown_until
                and node.id not in exclude
            ]
            if not candidates:
                return None

            def sort_key(node: NodeSlot):
                latency = node.last_latency_ms
                # None latency sorts last
                latency_key = (latency is None, latency if latency is not None else 0.0)
                return (node.ref_count, latency_key)

            selected = sorted(candidates, key=sort_key)[0]
            selected.ref_count += 1
            return copy(selected)

    def release(self, node_id: str, *, failed: bool = False) -> None:
        with self._lock:
            node = self._nodes.get(node_id)
            if node is None:
                return
            node.ref_count = max(0, node.ref_count - 1)
            if failed:
                node.fail_count += 1
                node.healthy = False
                cooldown = getattr(self, "config", None)
                seconds = 30.0
                if cooldown is not None and hasattr(cooldown, "fail_cooldown_sec"):
                    seconds = float(cooldown.fail_cooldown_sec or 30.0)
                node.cooldown_until = time.time() + seconds

    def status(self) -> dict:
        with self._lock:
            nodes = list(self._nodes.values())
            healthy = sum(1 for n in nodes if n.healthy)
            leases = sum(n.ref_count for n in nodes)
            sample = [
                {
                    "id": n.id,
                    "name": n.name,
                    "healthy": n.healthy,
                    "ref_count": n.ref_count,
                    "fail_count": n.fail_count,
                    "last_latency_ms": n.last_latency_ms,
                    "cooldown_until": n.cooldown_until,
                    "local_http": n.local_http,
                }
                for n in nodes[:20]
            ]
            return {
                "running": bool(getattr(self, "_running", False)),
                "total": len(nodes),
                "healthy": healthy,
                "leases": leases,
                "nodes": sample,
            }
