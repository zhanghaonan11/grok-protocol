from __future__ import annotations

import logging
import threading
import time
from copy import copy
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set
from urllib.parse import parse_qs, unquote, urlparse

try:
    import yaml
except ImportError:  # pragma: no cover - fallback for environments without PyYAML
    yaml = None  # type: ignore


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



logger = logging.getLogger(__name__)


def parse_vless_node(raw: str) -> Optional[dict]:
    """Parse a vless:// share link into structured fields for NodeSlot/mihomo."""
    line = str(raw or "").strip()
    if not line:
        return None
    if not line.lower().startswith("vless://"):
        return None

    try:
        # Keep fragment as name; urlparse handles userinfo/host/port/query.
        parsed = urlparse(line)
    except Exception:
        return None

    uuid = unquote(parsed.username or "")
    host = parsed.hostname or ""
    try:
        port = int(parsed.port or 0)
    except (TypeError, ValueError):
        port = 0
    if not uuid or not host or port <= 0:
        return None

    qs = parse_qs(parsed.query or "", keep_blank_values=True)
    params: Dict[str, str] = {}
    for key, values in qs.items():
        if not values:
            continue
        params[key] = unquote(values[0])

    name = unquote(parsed.fragment or "") or f"{host}:{port}"
    return {
        "protocol": "vless",
        "uuid": uuid,
        "server": host,
        "port": port,
        "name": name,
        "params": params,
        "raw": line,
    }


def _proxy_name(node: NodeSlot, used: Set[str]) -> str:
    base = (node.name or node.id or f"{node.server}:{node.port}").strip() or "node"
    # mihomo proxy names should be unique and reasonably safe
    safe = base.replace("\n", " ").replace("\r", " ").strip()
    candidate = safe
    n = 2
    while candidate in used:
        candidate = f"{safe}-{n}"
        n += 1
    used.add(candidate)
    return candidate


def _node_to_vless_proxy(node: NodeSlot, proxy_name: str) -> Optional[Dict[str, Any]]:
    protocol = (node.protocol or "").lower().strip()
    if protocol and protocol != "vless":
        logger.warning("skip unsupported protocol %r for node %s", node.protocol, node.id)
        return None
    if not node.server or not node.port or not node.uuid:
        logger.warning("skip incomplete vless node %s (server/port/uuid required)", node.id)
        return None

    params = dict(node.params or {})
    security = (params.get("security") or "").lower()
    network = (params.get("type") or params.get("network") or "tcp").lower()
    sni = params.get("sni") or params.get("servername") or ""
    fp = params.get("fp") or params.get("fingerprint") or ""
    flow = params.get("flow") or ""
    alpn_raw = params.get("alpn") or ""
    client_fp = fp

    proxy: Dict[str, Any] = {
        "name": proxy_name,
        "type": "vless",
        "server": node.server,
        "port": int(node.port),
        "uuid": node.uuid,
        "udp": True,
        "network": network if network else "tcp",
    }
    if flow:
        proxy["flow"] = flow

    if security in {"tls", "reality"}:
        proxy["tls"] = True
        if sni:
            proxy["servername"] = sni
        if client_fp:
            proxy["client-fingerprint"] = client_fp
        if alpn_raw:
            proxy["alpn"] = [x.strip() for x in alpn_raw.split(",") if x.strip()]
        if security == "reality":
            reality_opts: Dict[str, Any] = {}
            if params.get("pbk"):
                reality_opts["public-key"] = params["pbk"]
            if params.get("sid"):
                reality_opts["short-id"] = params["sid"]
            if params.get("spx"):
                reality_opts["spider-x"] = params["spx"]
            if reality_opts:
                proxy["reality-opts"] = reality_opts
    elif security in {"", "none"}:
        proxy["tls"] = False
    else:
        # Unknown security: still emit basic proxy, leave tls false
        proxy["tls"] = False
        logger.warning("node %s has unknown security=%r; tls disabled", node.id, security)

    # Transport opts
    if network == "ws":
        ws_opts: Dict[str, Any] = {}
        path = params.get("path") or "/"
        host_header = params.get("host") or sni or node.server
        ws_opts["path"] = path
        if host_header:
            ws_opts["headers"] = {"Host": host_header}
        proxy["ws-opts"] = ws_opts
    elif network == "grpc":
        grpc_opts: Dict[str, Any] = {}
        service_name = params.get("serviceName") or params.get("service-name") or ""
        if service_name:
            grpc_opts["grpc-service-name"] = service_name
        if grpc_opts:
            proxy["grpc-opts"] = grpc_opts
    elif network in {"h2", "http"}:
        h2_opts: Dict[str, Any] = {}
        path = params.get("path") or "/"
        host_header = params.get("host") or sni or node.server
        h2_opts["path"] = path
        if host_header:
            h2_opts["host"] = [host_header]
        proxy["h2-opts"] = h2_opts
    # tcp / raw / empty: no extra opts

    return proxy


def build_mihomo_config(
    nodes: List[NodeSlot],
    *,
    listen_host: str,
    base_port: int,
) -> dict:
    """Build a multi-port mihomo config: one HTTP listener per VLESS node."""
    host = (listen_host or "127.0.0.1").strip() or "127.0.0.1"
    port_base = int(base_port)

    proxies: List[Dict[str, Any]] = []
    listeners: List[Dict[str, Any]] = []
    used_names: Set[str] = set()
    mapped_index = 0

    for node in nodes:
        name = _proxy_name(node, used_names)
        proxy = _node_to_vless_proxy(node, name)
        if proxy is None:
            continue
        listen_port = port_base + mapped_index
        listener = {
            "name": f"http-in-{listen_port}",
            "type": "http",
            "port": listen_port,
            "proxy": proxy["name"],
        }
        # bind only when non-default; keep listen_host for local_http always
        if host not in {"", "0.0.0.0"}:
            listener["listen"] = host
        proxies.append(proxy)
        listeners.append(listener)
        node.local_http = f"http://{host}:{listen_port}"
        mapped_index += 1

    return {
        "allow-lan": False,
        "mode": "rule",
        "log-level": "warning",
        "proxies": proxies,
        "listeners": listeners,
        "rules": ["MATCH,DIRECT"],
    }


def render_mihomo_yaml(config: dict) -> str:
    """Serialize mihomo config dict to YAML string."""
    if yaml is None:
        raise RuntimeError("PyYAML is required to render mihomo config")
    return yaml.safe_dump(
        config,
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
    )


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
