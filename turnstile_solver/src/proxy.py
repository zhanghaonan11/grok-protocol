from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlparse


@dataclass
class ProxySpec:
    raw: str = ""
    url: str = ""
    scheme: str = ""
    host: str = ""
    port: str = ""
    username: str = ""
    password: str = ""

    @property
    def enabled(self) -> bool:
        return bool(self.url)


def normalize_proxy(proxy: str) -> str:
    value = str(proxy or "").strip()
    if not value:
        return ""
    if "://" not in value:
        # host:port:user:pass -> http://user:pass@host:port
        parts = value.split(":")
        if len(parts) == 2:
            host, port = parts
            return f"http://{host}:{port}"
        if len(parts) == 4:
            host, port, user, password = parts
            return f"http://{user}:{password}@{host}:{port}"
    return value


def parse_proxy(proxy: str) -> ProxySpec:
    raw = str(proxy or "").strip()
    url = normalize_proxy(raw)
    if not url:
        return ProxySpec()
    parsed = urlparse(url)
    return ProxySpec(
        raw=raw,
        url=url,
        scheme=(parsed.scheme or "http").lower(),
        host=str(parsed.hostname or ""),
        port=str(parsed.port or ""),
        username=str(parsed.username or ""),
        password=str(parsed.password or ""),
    )
