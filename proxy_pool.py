# -*- coding: utf-8 -*-
"""Manual proxy pool: normalize / validate / weighted rotate / cooldown.

Cloned and adapted from GPT注册机-QQ-NB ProxyRotator, with grok-project
integration points (host validation, shared stats path, display helpers).
"""

from __future__ import annotations

import json
import os
import random
import re
import threading
import time
from typing import Any, Dict, List, Optional, Sequence
from urllib.parse import urlparse


_PROXY_STATS_DEFAULT = "proxy_stats.log"
_INVALID_HOSTS = {
    "",
    "null",
    "none",
    "undefined",
    "nil",
    "0.0.0.0",
    "localhost",
    "example.com",
    "example.org",
}


def project_root() -> str:
    return os.path.dirname(os.path.abspath(__file__))


def default_stats_path() -> str:
    return os.path.join(project_root(), _PROXY_STATS_DEFAULT)


def normalize_proxy_line(line: str) -> str:
    """Normalize free-form proxy text into http(s)/socks URL.

    Supports:
      - already-qualified URLs
      - socks5 / socks5h prefixes
      - host:port:user:pass
      - host:port
      - user:pass@host:port
    """
    text = str(line or "").strip()
    if not text:
        return ""
    if (text.startswith('"') and text.endswith('"')) or (text.startswith("'") and text.endswith("'")):
        text = text[1:-1].strip()
        if not text:
            return ""
    if re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", text):
        return text
    lower = text.lower()
    if lower.startswith("socks5h"):
        rest = text[7:].lstrip()
        if rest.startswith("//"):
            rest = rest[2:].lstrip()
        return f"socks5h://{rest}"
    if lower.startswith("socks5"):
        rest = text[6:].lstrip()
        if rest.startswith("//"):
            rest = rest[2:].lstrip()
        return f"socks5://{rest}"
    # host:port:user:pass
    if "://" not in text and text.count(":") >= 3 and "@" not in text:
        parts = text.split(":")
        host, port_s, user = parts[0], parts[1], parts[2]
        pwd = ":".join(parts[3:])
        if host and port_s.isdigit():
            return f"http://{user}:{pwd}@{host}:{port_s}"
    if "://" not in text and "@" in text:
        return f"http://{text}"
    if "://" not in text and text.count(":") == 1:
        return f"http://{text}"
    return f"http://{text}"


def proxy_host_of(proxy_str: str) -> str:
    text = str(proxy_str or "").strip()
    if not text:
        return ""
    try:
        # Prefer raw host:port:user:pass host segment before URL encoding surprises.
        if "://" not in text and text.count(":") >= 3 and "@" not in text:
            return text.split(":", 1)[0].strip().lower()
        parsed = urlparse(text if "://" in text else f"http://{text}")
        return str(parsed.hostname or "").strip().lower()
    except Exception:
        return ""


def is_valid_proxy_host(host: str) -> bool:
    h = str(host or "").strip().lower()
    if not h or h in _INVALID_HOSTS:
        return False
    if h.startswith("[") and h.endswith("]"):
        h = h[1:-1]
    # bare placeholder / non-resolvable literals
    if h in _INVALID_HOSTS:
        return False
    if re.fullmatch(r"\d+", h):
        return False
    return True


def validate_proxy_line(line: str) -> tuple[str, str]:
    """Return (normalized_url, error). error empty means ok."""
    raw = str(line or "").strip()
    if not raw:
        return "", "空代理"
    normalized = normalize_proxy_line(raw)
    if not normalized:
        return "", "无法规范化代理"
    host = proxy_host_of(normalized) or proxy_host_of(raw)
    if not is_valid_proxy_host(host):
        return "", f"无效代理主机: {host or '(empty)'}"
    # port sanity when parseable
    try:
        parsed = urlparse(normalized)
        port = parsed.port
        if port is not None and not (1 <= int(port) <= 65535):
            return "", f"无效代理端口: {port}"
    except Exception as exc:
        return "", f"代理解析失败: {exc}"
    return normalized, ""


def load_proxy_lines(filepath: str) -> List[str]:
    """Load proxies from file; skip blanks/comments; normalize; drop invalids."""
    path = str(filepath or "").strip()
    if not path or not os.path.exists(path):
        return []
    out: List[str] = []
    seen = set()
    try:
        with open(path, "r", encoding="utf-8") as handle:
            for raw in handle:
                line = str(raw or "").strip()
                if not line or line.startswith("#"):
                    continue
                normalized, err = validate_proxy_line(line)
                if err or not normalized:
                    continue
                if normalized in seen:
                    continue
                seen.add(normalized)
                out.append(normalized)
    except OSError:
        return []
    return out


def split_proxy_text(raw: str) -> List[str]:
    text = str(raw or "").strip()
    if not text:
        return []
    if text.startswith("[") and text.endswith("]"):
        try:
            parsed = json.loads(text)
            if isinstance(parsed, list):
                items = [str(x) for x in parsed]
                return normalize_proxy_pool(items)
        except Exception:
            pass
    parts = re.split(r"[\n\r,;|]+", text)
    return normalize_proxy_pool(parts)


def normalize_proxy_pool(raw: Sequence[str] | str) -> List[str]:
    if isinstance(raw, str):
        lines = re.split(r"[\n\r,;|]+", raw)
    else:
        lines = list(raw or [])
    out: List[str] = []
    seen = set()
    for part in lines:
        normalized, err = validate_proxy_line(str(part or ""))
        if err or not normalized or normalized in seen:
            continue
        seen.add(normalized)
        out.append(normalized)
    return out


def extract_country(proxy_str: str) -> str:
    """Best-effort country code from zone/user/host.

    Examples:
      _zone_JP / zone-JP / region-US / us.swiftproxy.net
    """
    if not proxy_str:
        return "??"
    text = str(proxy_str)
    for pattern in (
        r"(?:_zone_|zone[-_]|region[-_]|country[-_])([A-Za-z]{2})(?:\b|[_-])",
        r"[_-]([A-Za-z]{2})[_-](?:sid|session|sess)",
    ):
        m = re.search(pattern, text, re.IGNORECASE)
        if m:
            return m.group(1).upper()
    try:
        host = proxy_host_of(text)
        parts = host.split(".")
        if parts and len(parts[0]) == 2 and parts[0].isalpha():
            return parts[0].upper()
    except Exception:
        pass
    return "??"


def extract_session_seconds(proxy: str) -> Optional[int]:
    """sessTime-N is minutes in common residential formats → seconds."""
    match = re.search(r"sessTime-(\d+)", str(proxy or ""), re.IGNORECASE)
    if not match:
        match = re.search(r"time[_-]?(\d+)", str(proxy or ""), re.IGNORECASE)
        if not match:
            return None
    try:
        return int(match.group(1)) * 60
    except Exception:
        return None


def mask_proxy(proxy_str: str) -> str:
    try:
        u = urlparse(proxy_str if "://" in proxy_str else f"http://{proxy_str}")
        if u.username or u.password:
            host = u.hostname or ""
            port = u.port or ""
            return f"{u.scheme}://***@{host}:{port}"
    except Exception:
        pass
    return str(proxy_str or "")


class ProxyRotator:
    """Thread-safe weighted proxy rotator with dynamic cooldown + JSONL stats."""

    def __init__(self, proxy_list: Sequence[str], stats_file: str = ""):
        normalized = normalize_proxy_pool(list(proxy_list or []))
        self._proxies = list(normalized)
        self._lock = threading.Lock()
        self._bad_proxies: Dict[str, float] = {}
        self._country_stats: Dict[str, Dict[str, Any]] = {}
        self._proxy_country: Dict[str, str] = {}
        self._stats_file = stats_file or default_stats_path()
        for proxy in self._proxies:
            country = extract_country(proxy)
            self._proxy_country[proxy] = country
            if country not in self._country_stats:
                self._country_stats[country] = {
                    "success": 0,
                    "fail": 0,
                    "consecutive_fail": 0,
                    "last_fail_time": 0.0,
                }
        self._load_history()

    def __len__(self) -> int:
        return len(self._proxies)

    def proxies(self) -> List[str]:
        return list(self._proxies)

    def _load_history(self) -> None:
        if not os.path.exists(self._stats_file):
            return
        try:
            with open(self._stats_file, "r", encoding="utf-8") as handle:
                lines = handle.readlines()[-2000:]
            for line in lines:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                country = str(rec.get("country") or "??")
                result = str(rec.get("result") or "")
                if country not in self._country_stats:
                    self._country_stats[country] = {
                        "success": 0,
                        "fail": 0,
                        "consecutive_fail": 0,
                        "last_fail_time": 0.0,
                    }
                if result == "success":
                    self._country_stats[country]["success"] += 1
                elif result == "fail":
                    self._country_stats[country]["fail"] += 1
        except Exception:
            pass

    def _append_log(self, country: str, proxy_str: str, result: str, reason: str = "") -> None:
        try:
            rec = {
                "time": time.strftime("%Y-%m-%d %H:%M:%S"),
                "country": country,
                "proxy": mask_proxy(proxy_str),
                "result": result,
            }
            if reason:
                rec["reason"] = str(reason)[:200]
            parent = os.path.dirname(self._stats_file)
            if parent:
                os.makedirs(parent, exist_ok=True)
            with open(self._stats_file, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except Exception:
            pass

    def record_result(self, proxy_str: str, success: bool, reason: str = "") -> None:
        if not proxy_str:
            return
        country = self._proxy_country.get(proxy_str) or extract_country(proxy_str)
        self._proxy_country[proxy_str] = country
        with self._lock:
            if country not in self._country_stats:
                self._country_stats[country] = {
                    "success": 0,
                    "fail": 0,
                    "consecutive_fail": 0,
                    "last_fail_time": 0.0,
                }
            st = self._country_stats[country]
            if success:
                st["success"] += 1
                st["consecutive_fail"] = 0
            else:
                st["fail"] += 1
                st["consecutive_fail"] += 1
                st["last_fail_time"] = time.time()
        self._append_log(country, proxy_str, "success" if success else "fail", reason)

    def mark_bad(self, proxy_str: str, cooldown_seconds: int = 0) -> None:
        if not proxy_str:
            return
        country = self._proxy_country.get(proxy_str) or extract_country(proxy_str)
        self._proxy_country[proxy_str] = country
        with self._lock:
            if country not in self._country_stats:
                self._country_stats[country] = {
                    "success": 0,
                    "fail": 0,
                    "consecutive_fail": 0,
                    "last_fail_time": 0.0,
                }
            if cooldown_seconds > 0:
                cd = int(cooldown_seconds)
            else:
                cf = int(self._country_stats[country]["consecutive_fail"] or 0)
                cd = min(60 * (2 ** max(cf, 0)), 600)
            self._bad_proxies[proxy_str] = time.time() + cd

    def mark_good(self, proxy_str: str) -> None:
        if not proxy_str:
            return
        with self._lock:
            self._bad_proxies.pop(proxy_str, None)

    def _is_available(self, proxy_str: str) -> bool:
        deadline = self._bad_proxies.get(proxy_str)
        if deadline is None:
            return True
        if time.time() >= deadline:
            del self._bad_proxies[proxy_str]
            return True
        return False

    def _country_weight(self, country: str) -> float:
        st = self._country_stats.get(country)
        if not st:
            return 1.0
        total = int(st["success"]) + int(st["fail"])
        if total <= 0:
            return 1.0
        rate = float(st["success"]) / float(total)
        return max(rate * 10.0, 0.1)

    def next(self) -> Optional[str]:
        if not self._proxies:
            return None
        with self._lock:
            available: List[str] = []
            first_bad: Optional[str] = None
            for proxy in self._proxies:
                if self._is_available(proxy):
                    available.append(proxy)
                elif first_bad is None:
                    first_bad = proxy
            if not available:
                return first_bad
            if len(available) == 1:
                return available[0]
            weights = [self._country_weight(self._proxy_country.get(p, "??")) for p in available]
            return random.choices(available, weights=weights, k=1)[0]

    def next_batch(self, n: int) -> List[str]:
        """Pick up to n distinct proxies (best-effort, may repeat if pool tiny)."""
        count = max(0, int(n or 0))
        if count <= 0 or not self._proxies:
            return []
        picked: List[str] = []
        seen = set()
        for _ in range(count * 3):
            item = self.next()
            if not item:
                break
            if item in seen:
                continue
            seen.add(item)
            picked.append(item)
            if len(picked) >= count:
                break
        while len(picked) < count and self._proxies:
            picked.append(random.choice(self._proxies))
        return picked[:count]

    def get_status(self) -> List[Dict[str, Any]]:
        now = time.time()
        with self._lock:
            rows: List[Dict[str, Any]] = []
            for proxy_str in self._proxies:
                deadline = self._bad_proxies.get(proxy_str)
                if deadline is None or now >= deadline:
                    status = "ok"
                    cooldown_left = 0
                else:
                    status = "bad"
                    cooldown_left = int(deadline - now)
                rows.append(
                    {
                        "proxy": mask_proxy(proxy_str),
                        "status": status,
                        "cooldown_left": cooldown_left,
                        "country": self._proxy_country.get(proxy_str, "??"),
                    }
                )
            return rows

    def get_country_stats(self) -> List[Dict[str, Any]]:
        with self._lock:
            rows: List[Dict[str, Any]] = []
            for country, st in sorted(self._country_stats.items()):
                total = int(st["success"]) + int(st["fail"])
                rate = (float(st["success"]) / float(total) * 100.0) if total > 0 else 0.0
                active = 0
                cooldown = 0
                for proxy, c in self._proxy_country.items():
                    if c != country:
                        continue
                    if self._is_available(proxy):
                        active += 1
                    else:
                        cooldown += 1
                rows.append(
                    {
                        "country": country,
                        "success": int(st["success"]),
                        "fail": int(st["fail"]),
                        "rate": round(rate, 1),
                        "weight": round(self._country_weight(country), 1),
                        "consecutive_fail": int(st["consecutive_fail"]),
                        "active_proxies": active,
                        "cooldown_proxies": cooldown,
                    }
                )
            return rows


# Process-level rotator used by registration workers / web tests.
_global_rotator: Optional[ProxyRotator] = None
_global_rotator_lock = threading.Lock()
_global_rotator_key = ""


def configure_global_rotator(
    proxy_list: Sequence[str],
    *,
    stats_file: str = "",
    force: bool = False,
) -> ProxyRotator:
    global _global_rotator, _global_rotator_key
    normalized = normalize_proxy_pool(list(proxy_list or []))
    key = f"{stats_file or default_stats_path()}|{len(normalized)}|{hash(tuple(normalized[:50]))}"
    with _global_rotator_lock:
        if (not force) and _global_rotator is not None and key == _global_rotator_key:
            return _global_rotator
        _global_rotator = ProxyRotator(normalized, stats_file=stats_file or default_stats_path())
        _global_rotator_key = key
        return _global_rotator


def get_global_rotator() -> Optional[ProxyRotator]:
    return _global_rotator


def ensure_rotator_from_file(filepath: str, *, stats_file: str = "") -> ProxyRotator:
    proxies = load_proxy_lines(filepath)
    return configure_global_rotator(proxies, stats_file=stats_file)


def pick_proxy(
    proxy_list: Sequence[str] | None = None,
    *,
    stats_file: str = "",
    prefer_rotator: bool = True,
) -> str:
    """Pick one proxy URL; empty string if none."""
    if prefer_rotator:
        rotator = get_global_rotator()
        if rotator is None and proxy_list is not None:
            rotator = configure_global_rotator(proxy_list, stats_file=stats_file)
        if rotator is not None and len(rotator) > 0:
            return str(rotator.next() or "")
    pool = normalize_proxy_pool(list(proxy_list or []))
    if not pool:
        return ""
    return random.choice(pool)


def report_outcome(proxy_str: str, success: bool, reason: str = "") -> None:
    """Record success/failure on the process-level rotator if present."""
    target = str(proxy_str or "").strip()
    if not target:
        return
    rotator = get_global_rotator()
    if rotator is None:
        return
    try:
        pool = list(rotator.proxies())
        key = target
        if target not in pool:
            norm, _err = validate_proxy_line(target)
            if norm and norm in pool:
                key = norm
            else:
                host = proxy_host_of(target)
                if host:
                    for item in pool:
                        if host in item:
                            key = item
                            break
        rotator.record_result(key, bool(success), reason=str(reason or "")[:120])
        if success:
            rotator.mark_good(key)
        else:
            rotator.mark_bad(key)
    except Exception:
        pass
