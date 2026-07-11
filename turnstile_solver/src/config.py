from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional, Union


DEFAULT_SIGNUP_URL = "https://accounts.x.ai/sign-up?redirect=grok-com"


@dataclass
class SolverConfig:
    host: str = "127.0.0.1"
    port: int = 8787
    max_concurrency: int = 2
    browser_timeout_sec: int = 180
    token_min_length: int = 80
    signup_url: str = DEFAULT_SIGNUP_URL
    headless: bool = False
    proxy: str = ""
    parent_proxy: str = ""
    proxy_file: str = ""
    local_proxy_port: int = 0
    user_agent: str = ""
    enable_metrics: bool = True

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "SolverConfig":
        return cls(
            host=str(data.get("host") or "127.0.0.1"),
            port=int(data.get("port") or 8787),
            max_concurrency=max(1, int(data.get("max_concurrency") or 2)),
            browser_timeout_sec=max(30, int(data.get("browser_timeout_sec") or 180)),
            token_min_length=max(20, int(data.get("token_min_length") or 80)),
            signup_url=str(data.get("signup_url") or DEFAULT_SIGNUP_URL),
            headless=bool(data.get("headless") or False),
            proxy=str(data.get("proxy") or ""),
            parent_proxy=str(data.get("parent_proxy") or data.get("proxy_parent") or ""),
            proxy_file=str(data.get("proxy_file") or ""),
            local_proxy_port=int(data.get("local_proxy_port") or 0),
            user_agent=str(data.get("user_agent") or ""),
            enable_metrics=bool(data.get("enable_metrics", True)),
        )


def load_config(path: Optional[Union[str, Path]] = None) -> SolverConfig:
    if not path:
        return SolverConfig()
    cfg_path = Path(path).expanduser().resolve()
    data = json.loads(cfg_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"config root must be object: {cfg_path}")
    return SolverConfig.from_dict(data)
