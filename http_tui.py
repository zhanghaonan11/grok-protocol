#!/usr/bin/env python3
"""HTTP 协议注册流程的全屏终端界面。

本界面会启动 ``grok_register_ttk.py http register`` 子进程。
主流程（邮箱/OTP/注册提交）始终走 HTTP 协议，不会全程开浏览器。
仅当 Turnstile provider=local 时，才会在求解阶段临时拉起 Chrome，拿完 token 后关闭。
模式2 在注册拿到 sso 后，会再调用同目录 ``sso_to_auth_json.py`` 做 Device Flow 凭证转换。
"""

from __future__ import annotations

import argparse
import curses
import json
import os
import queue
import shutil
import re
import subprocess
import sys
import threading
import time
import unicodedata
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Deque, Dict, List, Optional, Sequence, Tuple


ROOT_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG_PATH = ROOT_DIR / "config.json"
DEFAULT_OUTPUT_DIR = ROOT_DIR / "xai_credentials"
RUNS_DIR = ROOT_DIR / "http_runs"
MAX_COUNT = 1000
MAX_WORKERS = 32
MAX_LOCAL_TURNSTILE_WORKERS = 3
MAX_LOG_LINES = 700
DEFAULT_SSO_CONVERT_RETRIES = 5
DEFAULT_SSO_CONVERT_COOLDOWN = 3
MAX_SSO_CONVERT_RETRIES = 20
MAX_SSO_CONVERT_COOLDOWN = 120
ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")

STATUS_LABELS = {
    "queued": "排队中",
    "running": "运行中",
    "converting": "转换中",
    "succeeded": "成功",
    "failed": "失败",
    "stopped": "已停止",
}

PROXY_MODE_LABELS = {
    "auto": "自动",
    "none": "不使用",
    "direct": "直连代理",
    "pool": "代理池",
}

TURNSTILE_PROVIDER_ORDER = ["capsolver", "2captcha", "yescaptcha", "local"]
TURNSTILE_PROVIDER_LABELS = {
    "capsolver": "CapSolver",
    "2captcha": "2Captcha",
    "yescaptcha": "YesCaptcha",
    "local": "本地浏览器仅求解",
}

# 运行链路模式：模式1 是现有注册链路，模式2 仅做占位。
RUN_MODE_REGISTER_OTP = "register_otp"
RUN_MODE_REGISTER_SSO = "register_sso"
DEFAULT_RUN_MODE = RUN_MODE_REGISTER_OTP
RUN_MODE_ORDER = [RUN_MODE_REGISTER_OTP, RUN_MODE_REGISTER_SSO]
RUN_MODE_LABELS = {
    RUN_MODE_REGISTER_OTP: "模式1:注册+otp",
    RUN_MODE_REGISTER_SSO: "模式2:注册+sso转换",
}
RUN_MODE_ALIASES = {
    "1": RUN_MODE_REGISTER_OTP,
    "mode1": RUN_MODE_REGISTER_OTP,
    "otp": RUN_MODE_REGISTER_OTP,
    "register_otp": RUN_MODE_REGISTER_OTP,
    "register+otp": RUN_MODE_REGISTER_OTP,
    "2": RUN_MODE_REGISTER_SSO,
    "mode2": RUN_MODE_REGISTER_SSO,
    "sso": RUN_MODE_REGISTER_SSO,
    "register_sso": RUN_MODE_REGISTER_SSO,
    "register+sso": RUN_MODE_REGISTER_SSO,
    "sso_convert": RUN_MODE_REGISTER_SSO,
}


class TuiConfigError(RuntimeError):
    """批量任务启动前，启动参数不合法时抛出。"""


def _pgrep_count(pattern: str) -> int:
    """Count processes matching pattern via pgrep -c -f."""
    try:
        result = subprocess.run(
            ["pgrep", "-c", "-f", pattern],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return 0
    raw = (result.stdout or "").strip()
    if result.returncode not in {0, 1}:
        return 0
    try:
        return max(0, int(raw or "0"))
    except ValueError:
        return 0


def browser_health_status() -> Dict[str, int]:
    """Snapshot local browser residue pressure for Turnstile headed launches."""
    return {
        "chrome_count": _pgrep_count("chrome"),
        "playwright_count": _pgrep_count("ms-playwright/chromium"),
    }


def format_browser_health(status: Optional[Dict[str, int]] = None) -> str:
    data = status or browser_health_status()
    chrome = int(data.get("chrome_count") or 0)
    playwright = int(data.get("playwright_count") or 0)
    level = "正常"
    if chrome >= 200 or playwright >= 100:
        level = "高风险"
    elif chrome >= 80 or playwright >= 30:
        level = "偏高"
    return f"{level} | chrome={chrome} | playwright={playwright}"


_TEMP_DIR_GLOBS = (
    "xai-ts-chrome-*",
    "xai-ts-probe*",
    "xai-chrome-raw-*",
    "playwright_chromiumdev_profile-*",
)


def cleanup_browser_residues(
    *,
    temp_root: Optional[Path] = None,
    kill_playwright: bool = True,
    kill_all_chrome: bool = False,
    pkill_fn=None,
) -> Dict[str, int]:
    """Clean Playwright/Chrome residues that commonly block headed Turnstile launches.

    Default is conservative:
      - kill Playwright Chromium leftovers
      - remove this project's temp Chrome profile dirs
      - do NOT kill every Chrome process unless explicitly requested
    """
    if pkill_fn is None:
        def pkill_fn(pattern: str) -> int:
            before = _pgrep_count(pattern)
            if before <= 0:
                return 0
            subprocess.run(
                ["pkill", "-f", pattern],
                capture_output=True,
                text=True,
                check=False,
            )
            # Give the kernel a moment to reap.
            time.sleep(0.2)
            after = _pgrep_count(pattern)
            return max(0, before - after)

    killed_playwright = int(pkill_fn("ms-playwright/chromium") if kill_playwright else 0)
    killed_chrome = int(pkill_fn("chrome") if kill_all_chrome else 0)

    root = Path(temp_root or "/tmp")
    removed = 0
    for pattern in _TEMP_DIR_GLOBS:
        for path in root.glob(pattern):
            try:
                if path.is_dir():
                    shutil.rmtree(path, ignore_errors=True)
                    removed += 1
                elif path.exists():
                    path.unlink(missing_ok=True)
                    removed += 1
            except OSError:
                continue

    return {
        "killed_playwright": killed_playwright,
        "killed_chrome": killed_chrome,
        "removed_temp_dirs": removed,
        "chrome_count": _pgrep_count("chrome"),
        "playwright_count": _pgrep_count("ms-playwright/chromium"),
    }


def format_cleanup_result(result: Dict[str, int]) -> str:
    return (
        f"已清理 Playwright={int(result.get('killed_playwright') or 0)}，"
        f"临时目录={int(result.get('removed_temp_dirs') or 0)}；"
        f"当前 chrome={int(result.get('chrome_count') or 0)}，"
        f"playwright={int(result.get('playwright_count') or 0)}"
    )




@dataclass
class Settings:
    config_path: Path
    count: int
    workers: int
    output_dir: Path
    run_mode: str = DEFAULT_RUN_MODE
    proxy_mode: str = "auto"
    no_proxy: bool = False
    turnstile_provider: str = "capsolver"
    turnstile_headless: bool = False
    sso_convert_retries: int = DEFAULT_SSO_CONVERT_RETRIES
    sso_convert_cooldown: int = DEFAULT_SSO_CONVERT_COOLDOWN
    config: Dict[str, object] = field(default_factory=dict)


@dataclass
class RunPlan:
    config_path: Path
    run_mode: str
    count: int
    workers: int
    output_dir: Path
    provider: str
    email_provider: str
    proxy_mode: str
    proxy_args: List[str]
    turnstile_headless: bool = False
    sso_convert_retries: int = DEFAULT_SSO_CONVERT_RETRIES
    sso_convert_cooldown: int = DEFAULT_SSO_CONVERT_COOLDOWN
    warnings: List[str] = field(default_factory=list)


@dataclass
class WorkerState:
    index: int
    status: str = "queued"
    process: Optional[subprocess.Popen[str]] = None
    convert_thread: Optional[threading.Thread] = None
    log_path: Optional[Path] = None
    accounts_path: Optional[Path] = None
    last_log: str = "等待空闲槽位"
    return_code: Optional[int] = None


def _char_width(char: str) -> int:
    if char in {"\n", "\r"}:
        return 0
    if unicodedata.east_asian_width(char) in {"F", "W"}:
        return 2
    if unicodedata.combining(char):
        return 0
    return 1


def _display_width(text: str) -> int:
    return sum(_char_width(char) for char in text)


def _clip_display(text: str, width: int) -> str:
    if width <= 0:
        return ""
    result: List[str] = []
    used = 0
    for char in text:
        char_width = _char_width(char)
        if used + char_width > width:
            break
        result.append(char)
        used += char_width
    return "".join(result)


def _pad_display(text: str, width: int) -> str:
    clipped = _clip_display(text, width)
    padding = max(0, width - _display_width(clipped))
    return clipped + (" " * padding)


def _safe_text(value: object, limit: int = 400) -> str:
    text = ANSI_ESCAPE_RE.sub("", str(value or "")).replace("\r", "").replace("\t", "    ")
    text = " ".join(text.split())
    return text[:limit]


def _status_label(status: str) -> str:
    return STATUS_LABELS.get(status, status)


def _proxy_mode_label(mode: str) -> str:
    return PROXY_MODE_LABELS.get(mode, mode)


def _normalize_turnstile_provider(value: object) -> str:
    raw = str(value or "capsolver").strip().lower().replace(" ", "")
    aliases = {
        "cap": "capsolver",
        "cap-solver": "capsolver",
        "cap_solver": "capsolver",
        "2cap": "2captcha",
        "two": "2captcha",
        "yes": "yescaptcha",
        "browser": "local",
        "chrome": "local",
        "headless": "local",
        "local-browser": "local",
    }
    provider = aliases.get(raw, raw)
    if provider not in TURNSTILE_PROVIDER_LABELS:
        raise TuiConfigError(f"不支持的 Turnstile 求解方式: {value}")
    return provider


def _turnstile_provider_label(provider: str, *, headless: bool = False) -> str:
    base = TURNSTILE_PROVIDER_LABELS.get(provider, provider)
    if provider == "local" and headless:
        return f"{base}(无头,仅Turnstile阶段)"
    if provider == "local":
        return f"{base}(有界面,仅Turnstile阶段)"
    return base


def _run_mode_label(mode: str) -> str:
    return RUN_MODE_LABELS.get(mode, mode)


def _normalize_run_mode(value: object) -> str:
    raw = str(value or DEFAULT_RUN_MODE).strip().lower().replace(" ", "")
    mode = RUN_MODE_ALIASES.get(raw, raw)
    if mode not in RUN_MODE_LABELS:
        raise TuiConfigError(f"不支持的运行模式: {value}")
    return mode


def _sso_converter_path() -> Path:
    return ROOT_DIR / "sso_to_auth_json.py"


def _ensure_mode2_ready() -> None:
    converter = _sso_converter_path()
    if not converter.is_file():
        raise TuiConfigError(f"模式2 缺少转换脚本: {converter}")
    try:
        import curl_cffi  # noqa: F401
    except Exception as exc:  # pragma: no cover - depends on local env
        raise TuiConfigError(
            "模式2 需要 curl_cffi（sso_to_auth_json Device Flow）。请先安装 requirements.txt"
        ) from exc


def _parse_account_row(line: str) -> Optional[Tuple[str, str, str]]:
    raw = str(line or "").strip()
    if not raw or raw.startswith("#"):
        return None
    parts = [part.strip() for part in raw.split("----")]
    if len(parts) < 3:
        return None
    email, password, sso = parts[0], parts[1], parts[-1]
    if not email or not sso:
        return None
    return email, password, sso


def _read_config(path: Path) -> Dict[str, object]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise TuiConfigError(f"找不到配置文件: {path}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise TuiConfigError(f"无法读取配置 JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise TuiConfigError("配置 JSON 根节点必须是对象")
    return data


def _write_config(path: Path, data: Dict[str, object]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
        payload = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
        tmp.write_text(payload, encoding="utf-8")
        os.replace(tmp, path)
    except OSError as exc:
        raise TuiConfigError(f"无法写入配置文件: {exc}") from exc


def _config_path_value(path: Path, base: Path) -> str:
    """尽量把路径写成相对配置目录，方便迁移。"""
    try:
        return str(path.resolve().relative_to(base.resolve()))
    except Exception:
        return str(path)


def _positive_int(value: object, label: str, maximum: int) -> int:
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise TuiConfigError(f"{label} 必须是整数") from exc
    if not 1 <= number <= maximum:
        raise TuiConfigError(f"{label} 必须介于 1 到 {maximum} 之间")
    return number


def _bounded_int(value: object, label: str, *, minimum: int, maximum: int, default: int) -> int:
    if value is None or str(value).strip() == "":
        return default
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise TuiConfigError(f"{label} 必须是整数") from exc
    if not minimum <= number <= maximum:
        raise TuiConfigError(f"{label} 必须介于 {minimum} 到 {maximum} 之间")
    return number


def _absolute_path(value: str, base: Path = ROOT_DIR) -> Path:
    candidate = Path(str(value or "")).expanduser()
    if not candidate.is_absolute():
        candidate = base / candidate
    return candidate.resolve()


def _as_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _load_runtime_fields(settings: Settings) -> None:
    """从配置文件填充 TUI 运行设置。"""
    config = settings.config
    settings.count = _positive_int(config.get("register_count", 1), "注册数量", MAX_COUNT)
    settings.workers = _positive_int(config.get("concurrent_workers", 1), "并发数", MAX_WORKERS)

    output_raw = str(
        config.get("xai_oauth_output_dir")
        or config.get("tui_output_dir")
        or ""
    ).strip()
    if output_raw:
        settings.output_dir = _absolute_path(output_raw, settings.config_path.parent)
    else:
        settings.output_dir = DEFAULT_OUTPUT_DIR.resolve()

    settings.run_mode = _normalize_run_mode(
        config.get("tui_run_mode") or config.get("run_mode") or DEFAULT_RUN_MODE
    )

    proxy_mode = str(config.get("tui_proxy_mode") or config.get("proxy_mode") or "auto").strip().lower()
    if proxy_mode not in PROXY_MODE_LABELS:
        proxy_mode = "auto"
    settings.no_proxy = proxy_mode == "none"
    settings.proxy_mode = "auto" if proxy_mode == "none" else proxy_mode
    settings.turnstile_provider = _normalize_turnstile_provider(
        config.get("turnstile_provider") or "capsolver"
    )
    settings.turnstile_headless = _as_bool(config.get("turnstile_headless", False))
    settings.sso_convert_retries = _bounded_int(
        config.get("tui_sso_convert_retries", config.get("sso_convert_retries")),
        "SSO转换重试次数",
        minimum=1,
        maximum=MAX_SSO_CONVERT_RETRIES,
        default=DEFAULT_SSO_CONVERT_RETRIES,
    )
    settings.sso_convert_cooldown = _bounded_int(
        config.get("tui_sso_convert_cooldown", config.get("sso_convert_cooldown")),
        "SSO转换冷却秒数",
        minimum=0,
        maximum=MAX_SSO_CONVERT_COOLDOWN,
        default=DEFAULT_SSO_CONVERT_COOLDOWN,
    )


def persist_settings(settings: Settings) -> None:
    """把当前 TUI 运行设置写回配置文件。"""
    config = dict(settings.config or {})
    config["register_count"] = int(settings.count)
    config["concurrent_workers"] = int(settings.workers)
    config["tui_run_mode"] = _normalize_run_mode(settings.run_mode)
    config["tui_proxy_mode"] = "none" if settings.no_proxy else str(settings.proxy_mode or "auto")
    config["xai_oauth_output_dir"] = _config_path_value(
        settings.output_dir, settings.config_path.parent
    )
    config["tui_sso_convert_retries"] = int(settings.sso_convert_retries)
    config["tui_sso_convert_cooldown"] = int(settings.sso_convert_cooldown)
    config["turnstile_provider"] = _normalize_turnstile_provider(settings.turnstile_provider)
    config["turnstile_headless"] = bool(settings.turnstile_headless)
    _write_config(settings.config_path, config)
    settings.config = config


def settings_from_args(args: argparse.Namespace) -> Settings:
    config_path = _absolute_path(args.config or str(DEFAULT_CONFIG_PATH))
    config = _read_config(config_path)
    settings = Settings(
        config_path=config_path,
        count=1,
        workers=1,
        output_dir=DEFAULT_OUTPUT_DIR.resolve(),
        config=config,
    )
    _load_runtime_fields(settings)

    if getattr(args, "count", None) is not None:
        settings.count = _positive_int(args.count, "注册数量", MAX_COUNT)
    if getattr(args, "workers", None) is not None:
        settings.workers = _positive_int(args.workers, "并发数", MAX_WORKERS)
    # argparse 默认值可能已给出 mode/output-dir；仅在显式传入时覆盖。
    explicit = set(getattr(args, "_explicit_cli", set()) or set())
    if "mode" in explicit and getattr(args, "mode", None):
        settings.run_mode = _normalize_run_mode(args.mode)
    elif getattr(args, "mode", None):
        settings.run_mode = _normalize_run_mode(args.mode)
    if "output_dir" in explicit and getattr(args, "output_dir", None):
        settings.output_dir = _absolute_path(args.output_dir)
    elif getattr(args, "output_dir", None):
        settings.output_dir = _absolute_path(args.output_dir)
    if bool(getattr(args, "no_proxy", False)):
        settings.no_proxy = True
        settings.proxy_mode = "none"
    return settings


def refresh_settings_config(settings: Settings, *, reset_defaults: bool = True) -> None:
    settings.config = _read_config(settings.config_path)
    if reset_defaults:
        _load_runtime_fields(settings)


def _resolve_proxy_args(settings: Settings) -> Tuple[str, List[str]]:
    if settings.no_proxy or settings.proxy_mode == "none":
        return "none", []

    config = settings.config
    mode = settings.proxy_mode
    direct_proxy = str(config.get("proxy") or "").strip()
    proxy_file_value = str(config.get("proxy_file") or "").strip()
    proxy_file = _absolute_path(proxy_file_value, settings.config_path.parent) if proxy_file_value else None
    if mode == "auto":
        mode = "direct" if direct_proxy else "pool" if proxy_file and proxy_file.is_file() else "none"

    args: List[str] = []
    if mode == "direct":
        if not direct_proxy:
            raise TuiConfigError("代理模式为直连，但 config.proxy 为空")
        args.extend(["--proxy", direct_proxy])
    elif mode == "pool":
        if not proxy_file or not proxy_file.is_file():
            raise TuiConfigError("代理模式为代理池，但 config.proxy_file 不可用")
        args.extend(["--proxy-file", str(proxy_file)])
        if _as_bool(config.get("proxy_random")):
            args.append("--proxy-random")
    elif mode != "none":
        raise TuiConfigError(f"不支持的代理模式: {mode}")

    parent_proxy = str(config.get("proxy_parent") or "").strip()
    if mode != "none" and parent_proxy:
        args.extend(["--proxy-parent", parent_proxy])
    return mode, args


def build_plan(settings: Settings) -> RunPlan:
    config = settings.config
    count = _positive_int(settings.count, "注册数量", MAX_COUNT)
    workers = min(_positive_int(settings.workers, "并发数", MAX_WORKERS), count)
    provider = _normalize_turnstile_provider(
        settings.turnstile_provider or config.get("turnstile_provider") or "capsolver"
    )
    turnstile_headless = bool(settings.turnstile_headless)
    email_provider = str(config.get("email_provider") or "cloudflare").strip().lower() or "cloudflare"
    warnings: List[str] = []

    if provider == "capsolver":
        has_key = bool(
            str(config.get("turnstile_api_key") or "").strip()
            or os.environ.get("CAPSOLVER_API_KEY")
            or os.environ.get("XAI_TURNSTILE_API_KEY")
        )
        if not has_key:
            raise TuiConfigError(
                "缺少 CapSolver API 密钥。请设置 config.turnstile_api_key 或 CAPSOLVER_API_KEY。"
            )
    elif provider in {"2captcha", "yescaptcha"}:
        env_names = {
            "2captcha": ("TWOCAPTCHA_API_KEY", "TWO_CAPTCHA_API_KEY", "XAI_TURNSTILE_API_KEY"),
            "yescaptcha": ("YESCAPTCHA_API_KEY", "XAI_TURNSTILE_API_KEY"),
        }[provider]
        has_key = bool(
            str(config.get("turnstile_api_key") or "").strip()
            or any(os.environ.get(name) for name in env_names)
        )
        if not has_key:
            raise TuiConfigError(
                f"缺少 {TURNSTILE_PROVIDER_LABELS[provider]} API 密钥。请设置 config.turnstile_api_key 或对应环境变量。"
            )
    elif provider == "local":
        warnings.append(
            "主流程仍是 HTTP 协议；仅在 Turnstile 求解阶段临时打开本地浏览器"
            + ("（无头）" if turnstile_headless else "（有界面）")
            + "，拿完 token 立即关闭。"
        )
        if turnstile_headless:
            warnings.append(
                "本地无头会映射为 virtual-headed（Xvfb）；"
                "每个 worker 都会起浏览器，建议并发 <= "
                f"{MAX_LOCAL_TURNSTILE_WORKERS}。"
            )
        if workers > MAX_LOCAL_TURNSTILE_WORKERS:
            workers = MAX_LOCAL_TURNSTILE_WORKERS
            warnings.append(
                f"本地浏览器 Turnstile 已将并发限制为 {MAX_LOCAL_TURNSTILE_WORKERS}，"
                "避免 YYDS 建邮限流和本机浏览器资源打满。"
            )

    is_graph = email_provider in {"msgraph", "microsoft", "hotmail", "outlook"}
    has_mail_file = bool(str(config.get("ms_mail_file") or "").strip())
    if workers > 1 and (is_graph or has_mail_file):
        workers = 1
        warnings.append("Outlook/Graph 邮箱池会强制单并发，避免重复领取邮箱。")
    if workers > 1 and (
        os.environ.get("XAI_CASTLE_EMAIL_TOKEN") or os.environ.get("XAI_CASTLE_REGISTER_TOKEN")
    ):
        workers = 1
        warnings.append("绑定会话的 Castle token 会强制单并发执行。")
    if str(email_provider or "").strip().lower() == "yyds" and workers > 1:
        warnings.append(
            "YYDS 建邮有全局限流（跨进程文件锁 + 默认 1.5s 间隔）；"
            "并发越高排队越久，429 会自动退避重试。"
        )

    proxy_mode, proxy_args = _resolve_proxy_args(settings)
    run_mode = _normalize_run_mode(settings.run_mode)
    sso_convert_retries = _bounded_int(
        settings.sso_convert_retries,
        "SSO转换重试次数",
        minimum=1,
        maximum=MAX_SSO_CONVERT_RETRIES,
        default=DEFAULT_SSO_CONVERT_RETRIES,
    )
    sso_convert_cooldown = _bounded_int(
        settings.sso_convert_cooldown,
        "SSO转换冷却秒数",
        minimum=0,
        maximum=MAX_SSO_CONVERT_COOLDOWN,
        default=DEFAULT_SSO_CONVERT_COOLDOWN,
    )
    if run_mode == RUN_MODE_REGISTER_SSO:
        _ensure_mode2_ready()
        warnings.append(
            "模式2：注册成功后用 sso_to_auth_json 转换；"
            f"失败最多重试 {sso_convert_retries} 次，冷却 {sso_convert_cooldown}s。"
        )
    return RunPlan(
        config_path=settings.config_path,
        run_mode=run_mode,
        count=count,
        workers=workers,
        output_dir=settings.output_dir,
        provider=provider,
        email_provider=email_provider,
        proxy_mode=proxy_mode,
        proxy_args=proxy_args,
        turnstile_headless=turnstile_headless,
        sso_convert_retries=sso_convert_retries,
        sso_convert_cooldown=sso_convert_cooldown,
        warnings=warnings,
    )


def describe_plan(plan: RunPlan, *, dry_run: bool = False) -> str:
    lines = [
        "HTTP 协议 TUI",
        f"运行模式: {_run_mode_label(plan.run_mode)}",
        f"配置文件: {plan.config_path}",
        f"邮箱: {plan.email_provider}",
        f"Turnstile: {_turnstile_provider_label(plan.provider, headless=plan.turnstile_headless)}",
        f"注册数量: {plan.count}",
        f"并发数: {plan.workers}",
        f"代理: {_proxy_mode_label(plan.proxy_mode)}",
        f"OAuth 输出: {plan.output_dir}",
    ]
    if plan.run_mode == RUN_MODE_REGISTER_SSO:
        lines.append(
            f"SSO转换重试: {plan.sso_convert_retries} 次 / 冷却 {plan.sso_convert_cooldown}s"
        )
    lines.extend(f"警告: {warning}" for warning in plan.warnings)
    if dry_run:
        if plan.run_mode == RUN_MODE_REGISTER_SSO:
            lines.append(
                f"[dry-run] 将启动 {plan.count} 个注册任务；成功后用 sso_to_auth_json 转换凭证，最大并发 {plan.workers}。"
            )
        else:
            lines.append(
                f"[dry-run] 将启动 {plan.count} 个 HTTP 协议任务，最大并发 {plan.workers}。"
            )
    return "\n".join(lines)


class BatchRunner:
    """调度 HTTP 协议子进程，并把每一行输出流式送到 TUI。"""

    def __init__(self, plan: RunPlan):
        self.plan = plan
        self.run_id = f"{time.strftime('%Y%m%d_%H%M%S')}_{os.getpid()}"
        self.run_dir = RUNS_DIR / self.run_id
        self.workers = [WorkerState(index=index) for index in range(1, plan.count + 1)]
        self.events: queue.Queue[Tuple[int, str]] = queue.Queue()
        self.logs: Deque[str] = deque(maxlen=MAX_LOG_LINES)
        self.started = False
        self.done = False
        self.stopping = False
        self.next_index = 0
        self.summary_path: Optional[Path] = None
        self.account_count = 0

    @property
    def active(self) -> List[WorkerState]:
        # 注册中 + 后台 SSO 转换中都占并发槽，保证每个 worker 一对一跟随转换。
        return [worker for worker in self.workers if worker.status in {"running", "converting"}]

    @property
    def completed(self) -> int:
        return sum(worker.status in {"succeeded", "failed", "stopped"} for worker in self.workers)

    @property
    def succeeded(self) -> int:
        return sum(worker.status == "succeeded" for worker in self.workers)

    @property
    def failed(self) -> int:
        return sum(worker.status in {"failed", "stopped"} for worker in self.workers)

    def _log(self, source: str, message: str) -> None:
        message = _safe_text(message)
        if message:
            self.logs.append(f"[{source}] {message}")

    def start(self) -> None:
        if self.started:
            return
        self.plan.output_dir.mkdir(parents=True, exist_ok=True)
        RUNS_DIR.mkdir(parents=True, exist_ok=True)
        self.run_dir.mkdir(parents=True, exist_ok=True)
        for directory in (RUNS_DIR, self.run_dir):
            try:
                os.chmod(directory, 0o700)
            except OSError:
                pass
        self.started = True
        self._log("SYSTEM", "HTTP 协议批量任务已启动（邮箱/OTP/注册走协议；仅 local Turnstile 会临时开浏览器）")
        for warning in self.plan.warnings:
            self._log("SYSTEM", warning)
        self._spawn_available()

    def _command_for(self, worker: WorkerState) -> List[str]:
        assert worker.accounts_path is not None
        command = [
            sys.executable,
            str(ROOT_DIR / "grok_register_ttk.py"),
            "http",
            "register",
            "--mail-config",
            str(self.plan.config_path),
            "--turnstile-provider",
            self.plan.provider,
            "--accounts-output",
            str(worker.accounts_path),
        ]
        if self.plan.provider == "local" and self.plan.turnstile_headless:
            command.append("--turnstile-headless")
        # 模式1：仓库内 PKCE OAuth。
        # 模式2：显式传空 output-dir，关闭注册内置换票，改由 sso_to_auth_json 一对一转换。
        if self.plan.run_mode == RUN_MODE_REGISTER_OTP:
            command.extend(["--output-dir", str(self.plan.output_dir)])
        else:
            command.extend(["--output-dir", ""])
        command.extend(self.plan.proxy_args)
        return command

    def _append_worker_log(self, worker: WorkerState, message: str) -> None:
        text_line = _safe_text(message)
        if not text_line:
            return
        self.events.put((worker.index, text_line))
        if worker.log_path is not None:
            try:
                with worker.log_path.open("a", encoding="utf-8") as handle:
                    handle.write(text_line + "\n")
            except OSError:
                pass

    def _convert_sso_accounts(self, worker: WorkerState) -> Tuple[bool, str]:
        """注册成功后，用 sso_to_auth_json 把 sso 转成 xai-*.json。"""
        if not worker.accounts_path or not worker.accounts_path.is_file():
            return False, "注册成功但没有账号文件，无法做 SSO 转换"
        try:
            rows = [
                row
                for row in (
                    _parse_account_row(line)
                    for line in worker.accounts_path.read_text(encoding="utf-8").splitlines()
                )
                if row is not None
            ]
        except OSError as exc:
            return False, f"读取账号文件失败: {exc}"
        if not rows:
            return False, "注册成功但账号行为空，无法做 SSO 转换"

        converter = _sso_converter_path()
        retries = max(1, int(self.plan.sso_convert_retries or DEFAULT_SSO_CONVERT_RETRIES))
        cooldown = max(0, int(self.plan.sso_convert_cooldown or 0))
        ok_count = 0
        fail_messages: List[str] = []
        self.plan.output_dir.mkdir(parents=True, exist_ok=True)
        for email, _password, sso in rows:
            if self.stopping:
                fail_messages.append(f"{email}: 批次停止，跳过")
                continue
            last_error = "未知错误"
            success = False
            for attempt in range(1, retries + 1):
                if self.stopping:
                    last_error = "批次停止，跳过"
                    break
                command = [
                    sys.executable,
                    str(converter),
                    "--mode",
                    "auth",
                    "--sso-cookie",
                    sso,
                    "--out-dir",
                    str(self.plan.output_dir),
                    "--workers",
                    "1",
                ]
                if email:
                    command.extend(["--email", email])
                self._append_worker_log(
                    worker,
                    f"开始 SSO→凭证转换 | email={email} | 尝试 {attempt}/{retries}",
                )
                output_lines: List[str] = []
                try:
                    process = subprocess.Popen(
                        command,
                        cwd=str(ROOT_DIR),
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                        text=True,
                        encoding="utf-8",
                        errors="replace",
                        bufsize=1,
                    )
                except OSError as exc:
                    last_error = f"无法启动转换器 {exc}"
                    self._append_worker_log(worker, last_error)
                else:
                    assert process.stdout is not None
                    try:
                        for line in iter(process.stdout.readline, ""):
                            output_lines.append(line)
                            self._append_worker_log(worker, line)
                    finally:
                        try:
                            process.stdout.close()
                        except OSError:
                            pass
                    return_code = process.wait()
                    if return_code == 0:
                        success = True
                        break
                    joined = " ".join(_safe_text(x, 200) for x in output_lines[-8:])
                    last_error = f"转换退出码 {return_code}"
                    if "429" in joined or "slow_down" in joined.lower():
                        last_error += " (限流/slow_down)"
                    self._append_worker_log(
                        worker,
                        f"SSO 转换失败 | email={email} | 尝试 {attempt}/{retries} | {last_error}",
                    )
                if attempt < retries and not self.stopping:
                    wait_s = cooldown
                    # 限流时至少冷却配置秒数，避免连打 device/code
                    self._append_worker_log(
                        worker,
                        f"SSO 转换冷却 {wait_s}s 后重试 | email={email} | 下一轮 {attempt + 1}/{retries}",
                    )
                    # 分段 sleep，便于 stop 时尽快退出
                    deadline = time.monotonic() + wait_s
                    while time.monotonic() < deadline:
                        if self.stopping:
                            break
                        time.sleep(min(0.2, max(0.0, deadline - time.monotonic())))
            if success:
                ok_count += 1
            else:
                fail_messages.append(f"{email}: {last_error}")

        if ok_count == len(rows):
            return True, f"SSO 转换完成 {ok_count}/{len(rows)}"
        if ok_count == 0:
            return False, "SSO 转换全部失败: " + "; ".join(fail_messages[:3])
        return False, f"SSO 转换部分失败 {ok_count}/{len(rows)}: " + "; ".join(fail_messages[:3])

    def _start_sso_convert(self, worker: WorkerState) -> None:
        """每个 worker 注册成功后立即后台转换，互不阻塞主循环。"""
        worker.status = "converting"
        worker.last_log = "注册完成，后台 SSO 转换中"
        self._log(f"W{worker.index:02d}", worker.last_log)

        def runner() -> None:
            try:
                if self.stopping:
                    ok, msg = False, "批次停止，跳过 SSO 转换"
                else:
                    ok, msg = self._convert_sso_accounts(worker)
            except Exception as exc:  # pragma: no cover - defensive
                ok, msg = False, f"SSO 转换异常: {exc}"
            self.events.put((worker.index, f"__CONVERT_DONE__|{1 if ok else 0}|{msg}"))

        worker.convert_thread = threading.Thread(
            target=runner,
            name=f"sso-convert-{worker.index}",
            daemon=True,
        )
        worker.convert_thread.start()

    def _spawn_one(self, worker: WorkerState) -> None:
        worker.accounts_path = self.run_dir / f"accounts_{worker.index:03d}.txt"
        worker.log_path = self.run_dir / f"worker_{worker.index:03d}.log"
        command = self._command_for(worker)
        try:
            log_handle = worker.log_path.open("w", encoding="utf-8", buffering=1)
            process = subprocess.Popen(
                command,
                cwd=str(ROOT_DIR),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
            )
        except OSError as exc:
            worker.status = "failed"
            worker.last_log = f"无法启动进程: {exc}"
            self._log(f"W{worker.index:02d}", worker.last_log)
            try:
                log_handle.close()
            except UnboundLocalError:
                pass
            return

        worker.process = process
        worker.status = "running"
        worker.last_log = "协议进程已启动"
        self._log(f"W{worker.index:02d}", worker.last_log)

        def copy_and_queue() -> None:
            assert process.stdout is not None
            try:
                for line in iter(process.stdout.readline, ""):
                    try:
                        log_handle.write(line)
                    except OSError:
                        pass
                    self.events.put((worker.index, line))
            finally:
                try:
                    process.stdout.close()
                except OSError:
                    pass
                log_handle.close()

        threading.Thread(target=copy_and_queue, daemon=True).start()

    def _spawn_available(self) -> None:
        if self.stopping:
            return
        while len(self.active) < self.plan.workers and self.next_index < len(self.workers):
            worker = self.workers[self.next_index]
            self.next_index += 1
            self._spawn_one(worker)

    def _drain_events(self) -> None:
        while True:
            try:
                worker_index, message = self.events.get_nowait()
            except queue.Empty:
                return
            worker = self.workers[worker_index - 1]
            raw = str(message or "")
            if raw.startswith("__CONVERT_DONE__|"):
                parts = raw.split("|", 2)
                ok = len(parts) >= 2 and parts[1] == "1"
                msg = parts[2] if len(parts) >= 3 else "SSO 转换结束"
                worker.convert_thread = None
                if self.stopping and not ok:
                    worker.status = "stopped"
                else:
                    worker.status = "succeeded" if ok else "failed"
                worker.last_log = msg
                self._log(f"W{worker_index:02d}", worker.last_log)
                continue
            worker.last_log = _safe_text(raw)
            self._log(f"W{worker_index:02d}", raw)

    def _check_processes(self) -> None:
        for worker in list(self.workers):
            if worker.status != "running" or worker.process is None:
                continue
            return_code = worker.process.poll()
            if return_code is None:
                continue
            worker.return_code = return_code
            worker.process = None
            if self.stopping:
                worker.status = "stopped"
                worker.last_log = "已被操作者停止"
                self._log(f"W{worker.index:02d}", worker.last_log)
            elif return_code == 0:
                if self.plan.run_mode == RUN_MODE_REGISTER_SSO:
                    self._start_sso_convert(worker)
                else:
                    worker.status = "succeeded"
                    worker.last_log = "协议任务已完成"
                    self._log(f"W{worker.index:02d}", worker.last_log)
            else:
                worker.status = "failed"
                worker.last_log = f"协议任务退出，退出码 {return_code}"
                self._log(f"W{worker.index:02d}", worker.last_log)

    def _finalize(self) -> None:
        if self.done:
            return
        summary = ROOT_DIR / f"accounts_http_{self.run_id}.txt"
        lines: List[str] = []
        for worker in self.workers:
            if not worker.accounts_path or not worker.accounts_path.is_file():
                continue
            try:
                lines.extend(
                    line for line in worker.accounts_path.read_text(encoding="utf-8").splitlines() if line.strip()
                )
            except OSError as exc:
                self._log("SYSTEM", f"无法读取工作线程账号文件: {exc}")
        if lines:
            summary.write_text("\n".join(lines) + "\n", encoding="utf-8")
            try:
                os.chmod(summary, 0o600)
            except OSError:
                pass
            self.summary_path = summary
            self.account_count = len(lines)
        self.done = True
        self._log(
            "SYSTEM",
            f"批量完成: 成功={self.succeeded}, 失败={self.failed}, 账号数={self.account_count}",
        )

    def tick(self) -> None:
        if not self.started:
            return
        self._drain_events()
        self._check_processes()
        self._spawn_available()
        if self.completed == len(self.workers) and not self.active:
            self._drain_events()
            self._finalize()

    def stop(self) -> None:
        if self.done:
            return
        self.stopping = True
        self._log("SYSTEM", "正在停止活动中的协议任务")
        for worker in self.workers:
            if worker.status == "queued":
                worker.status = "stopped"
                worker.last_log = "因批次被停止而未启动"
                self._log(f"W{worker.index:02d}", worker.last_log)
        for worker in self.workers:
            if worker.status == "running" and worker.process is not None:
                try:
                    worker.process.terminate()
                except OSError:
                    pass
            elif worker.status == "converting":
                worker.last_log = "停止中：等待当前 SSO 转换收尾"
                self._log(f"W{worker.index:02d}", worker.last_log)

    def exit_code(self) -> int:
        return 0 if self.done and self.failed == 0 else 2


class ProtocolTui:
    """可编辑运行表单 + 实时批量仪表盘的 curses 界面。"""

    def __init__(self, screen: "curses._CursesWindow", settings: Settings, *, auto_start: bool = False):
        self.screen = screen
        self.settings = settings
        self.auto_start = auto_start
        self.selected = 0
        self.mode = "form"
        self.runner: Optional[BatchRunner] = None
        self.message = "方向键选择，回车确认。Turnstile=local 时会启动本机浏览器。"
        self.log_scroll = 0
        self._configure_screen()

    def _configure_screen(self) -> None:
        try:
            curses.curs_set(0)
        except curses.error:
            pass
        self.screen.keypad(True)
        self.screen.timeout(100)
        if curses.has_colors():
            curses.start_color()
            curses.use_default_colors()
            curses.init_pair(1, curses.COLOR_CYAN, -1)
            curses.init_pair(2, curses.COLOR_BLACK, curses.COLOR_CYAN)
            curses.init_pair(3, curses.COLOR_GREEN, -1)
            curses.init_pair(4, curses.COLOR_RED, -1)
            curses.init_pair(5, curses.COLOR_YELLOW, -1)
            curses.init_pair(6, curses.COLOR_MAGENTA, -1)

    @staticmethod
    def _clip(text: object, width: int) -> str:
        value = _safe_text(text, max(1, width * 2))
        return _clip_display(value, max(0, width))

    def _add(self, y: int, x: int, text: object, attr: int = 0, width: Optional[int] = None) -> None:
        height, screen_width = self.screen.getmaxyx()
        if y < 0 or y >= height or x < 0 or x >= screen_width:
            return
        available = screen_width - x if width is None else min(width, screen_width - x)
        if available <= 0:
            return
        try:
            self.screen.addnstr(y, x, self._clip(text, available), available, attr)
        except curses.error:
            pass

    def _box(self, y: int, x: int, height: int, width: int, title: str = "") -> None:
        if height < 3 or width < 4:
            return
        self._add(y, x, "+" + "-" * (width - 2) + "+")
        for line in range(y + 1, y + height - 1):
            self._add(line, x, "|")
            self._add(line, x + width - 1, "|")
        self._add(y + height - 1, x, "+" + "-" * (width - 2) + "+")
        if title:
            self._add(y, x + 2, f" {title} ", curses.color_pair(1) | curses.A_BOLD, width - 4)

    def _modal(self, title: str, prompt: str, value: str = "", *, secret: bool = False) -> Optional[str]:
        height, width = self.screen.getmaxyx()
        modal_width = min(max(54, _display_width(prompt) + 12), max(54, width - 8))
        modal_height = 7
        y = max(0, (height - modal_height) // 2)
        x = max(0, (width - modal_width) // 2)
        buffer = value
        curses.curs_set(1)
        self.screen.timeout(-1)
        while True:
            self.screen.erase()
            self._draw_frame("HTTP 协议 TUI")
            self._box(y, x, modal_height, modal_width, title)
            self._add(y + 2, x + 2, prompt, width=modal_width - 4)
            shown = "*" * len(buffer) if secret else buffer
            self._add(y + 4, x + 2, shown, curses.color_pair(2), modal_width - 4)
            self._add(y + 5, x + 2, "回车确认 | Esc 取消", curses.color_pair(5), modal_width - 4)
            self.screen.refresh()
            key = self.screen.get_wch()
            if key in ("\n", "\r", curses.KEY_ENTER):
                self.screen.timeout(100)
                curses.curs_set(0)
                return buffer
            if key == "\x1b":
                self.screen.timeout(100)
                curses.curs_set(0)
                return None
            if key in (curses.KEY_BACKSPACE, "\b", "\x7f"):
                buffer = buffer[:-1]
            elif isinstance(key, str) and key.isprintable():
                buffer += key

    def _confirm(self, question: str) -> bool:
        answer = self._modal("确认", f"{question} [y/N]", "")
        return bool(answer and answer.strip().lower() in {"y", "yes"})

    def _draw_frame(self, title: str) -> None:
        height, width = self.screen.getmaxyx()
        self.screen.erase()
        self._add(0, 0, "=" * max(1, width))
        text = f" {title} "
        left = max(0, (width - _display_width(text)) // 2)
        self._add(0, left, text, curses.color_pair(6) | curses.A_BOLD)
        self._add(height - 2, 0, "-" * max(1, width))

    def _draw_form(self) -> None:
        height, width = self.screen.getmaxyx()
        self._draw_frame("HTTP 协议 TUI - 协议批量启动器")
        self._add(2, 2, "主流程走 HTTP 协议。仅 Turnstile=本地浏览器时，会在求解阶段临时开 Chrome。", curses.color_pair(1))
        proxy_value = "none" if self.settings.no_proxy else self.settings.proxy_mode
        rows = [
            ("配置文件", str(self.settings.config_path)),
            ("运行模式", _run_mode_label(self.settings.run_mode)),
            ("Turnstile", _turnstile_provider_label(self.settings.turnstile_provider, headless=self.settings.turnstile_headless)),
            ("注册数量", str(self.settings.count)),
            ("并发数", str(self.settings.workers)),
            ("OAuth 输出", str(self.settings.output_dir)),
            ("代理", _proxy_mode_label(proxy_value)),
            ("SSO重试", f"{self.settings.sso_convert_retries} 次（模式2转换）"),
            ("SSO冷却", f"{self.settings.sso_convert_cooldown} 秒（模式2转换）"),
            ("浏览器状态", format_browser_health()),
            ("清理残留", "清 Playwright + /tmp 临时浏览器目录"),
            ("重载配置", "重新读取服务商、邮箱和默认值"),
            ("保存配置", "把上方运行设置写回配置文件"),
            ("开始", "启动当前模式任务"),
            ("退出", "不启动并离开"),
        ]
        panel_width = min(width - 8, 104)
        panel_x = max(2, (width - panel_width) // 2)
        panel_y = 4
        panel_height = min(height - 8, len(rows) + 4)
        self._box(panel_y, panel_x, panel_height, panel_width, "运行设置")
        for index, (label, value) in enumerate(rows):
            y = panel_y + 2 + index
            if y >= panel_y + panel_height - 1:
                break
            selected = index == self.selected
            attr = curses.color_pair(2) | curses.A_BOLD if selected else 0
            self._add(y, panel_x + 2, _pad_display(label, 12), attr, 14)
            self._add(y, panel_x + 17, value, attr, panel_width - 19)
        self._add(height - 1, 2, "上下键选择 | 回车编辑/执行 | q 退出", curses.color_pair(5))
        self._add(
            height - 3,
            2,
            self.message,
            curses.color_pair(4) if self.message.startswith("错误:") else 0,
            width - 4,
        )
        self.screen.refresh()

    def _status_attr(self, status: str) -> int:
        if status == "succeeded":
            return curses.color_pair(3) | curses.A_BOLD
        if status in {"failed", "stopped"}:
            return curses.color_pair(4) | curses.A_BOLD
        if status in {"running", "converting"}:
            return curses.color_pair(1) | curses.A_BOLD
        return curses.color_pair(5) if status == "queued" else 0

    def _draw_dashboard(self) -> None:
        assert self.runner is not None
        runner = self.runner
        height, width = self.screen.getmaxyx()
        self._draw_frame("HTTP 协议 TUI - 实时协议运行")
        if width < 80 or height < 20:
            self._add(2, 2, "终端太小。请调整到至少 80x20。", curses.color_pair(4) | curses.A_BOLD)
            self.screen.refresh()
            return

        left_width = max(32, min(width // 2, int(width * 0.37)))
        right_width = width - left_width - 3
        panel_top = 2
        panel_height = height - 5
        self._box(panel_top, 1, panel_height, left_width, "进度")
        self._box(panel_top, left_width + 2, panel_height, right_width, "后端日志")

        if runner.stopping and not runner.done:
            state = "停止中"
        elif runner.done:
            state = "已完成"
        else:
            state = "运行中"
        progress = runner.completed / max(1, runner.plan.count)
        bar_width = max(10, left_width - 6)
        filled = min(bar_width, int(progress * bar_width))
        bar = "[" + "#" * filled + "-" * (bar_width - filled) + "]"
        details = [
            f"状态: {state}",
            f"模式: {_run_mode_label(runner.plan.run_mode)}",
            f"邮箱: {runner.plan.email_provider}",
            f"Turnstile: {_turnstile_provider_label(runner.plan.provider, headless=runner.plan.turnstile_headless)}",
            f"代理: {_proxy_mode_label(runner.plan.proxy_mode)}",
            f"任务: {runner.completed}/{runner.plan.count}",
            f"活动: {len(runner.active)} / {runner.plan.workers}",
            f"成功: {runner.succeeded}",
            f"失败: {runner.failed}",
            bar,
        ]
        for index, line in enumerate(details):
            self._add(
                panel_top + 2 + index,
                3,
                line,
                self._status_attr("running") if index == 0 else 0,
                left_width - 4,
            )

        worker_top = panel_top + 14
        self._add(worker_top, 3, "工作线程", curses.color_pair(1) | curses.A_BOLD, left_width - 4)
        visible_workers = max(1, panel_height - (worker_top - panel_top) - 3)
        for offset, worker in enumerate(runner.workers[:visible_workers]):
            y = worker_top + 1 + offset
            status_text = _pad_display(_status_label(worker.status), 6)
            line = f"W{worker.index:02d} {status_text} {worker.last_log}"
            self._add(y, 3, line, self._status_attr(worker.status), left_width - 4)

        log_height = panel_height - 2
        log_width = right_width - 4
        logs = list(runner.logs)
        max_start = max(0, len(logs) - log_height)
        start = max(0, max_start - self.log_scroll)
        visible_logs = logs[start : start + log_height]
        for offset, log_line in enumerate(visible_logs):
            attr = curses.color_pair(4) if "[!]" in log_line or "失败" in log_line or "failed" in log_line.lower() else 0
            self._add(panel_top + 1 + offset, left_width + 4, log_line, attr, log_width)

        if runner.done:
            summary = str(runner.summary_path) if runner.summary_path else "没有成功的账号记录"
            footer = f"已完成。账号数={runner.account_count} | {summary} | q 退出"
        else:
            footer = "q 停止批次 | 上下键滚动日志 | l 跟随最新"
        self._add(height - 1, 2, footer, curses.color_pair(5), width - 4)
        self.screen.refresh()

    def _start_run(self) -> None:
        try:
            plan = build_plan(self.settings)
        except TuiConfigError as exc:
            self.message = f"错误: {exc}"
            return
        self.runner = BatchRunner(plan)
        self.runner.start()
        self.mode = "dashboard"
        self.log_scroll = 0
        self.message = ""

    def _persist_runtime_settings(self, note: str) -> None:
        persist_settings(self.settings)
        self.message = note

    def _edit_field(self, index: int) -> None:
        try:
            if index == 0:
                value = self._modal("配置路径", "路径", str(self.settings.config_path))
                if value:
                    self.settings.config_path = _absolute_path(value)
                    refresh_settings_config(self.settings)
                    self.message = "配置已重新加载。"
            elif index == 1:
                order = list(RUN_MODE_ORDER)
                current = self.settings.run_mode if self.settings.run_mode in order else DEFAULT_RUN_MODE
                next_mode = order[(order.index(current) + 1) % len(order)]
                self.settings.run_mode = next_mode
                self._persist_runtime_settings(f"运行模式已保存: {_run_mode_label(next_mode)}")
            elif index == 2:
                # 循环：capsolver -> 2captcha -> yescaptcha -> local有界面 -> local无头
                current = _normalize_turnstile_provider(self.settings.turnstile_provider)
                if current == "local" and not self.settings.turnstile_headless:
                    self.settings.turnstile_provider = "local"
                    self.settings.turnstile_headless = True
                elif current == "local" and self.settings.turnstile_headless:
                    self.settings.turnstile_provider = "capsolver"
                    self.settings.turnstile_headless = False
                else:
                    order = list(TURNSTILE_PROVIDER_ORDER)
                    idx = order.index(current) if current in order else 0
                    nxt = order[(idx + 1) % len(order)]
                    self.settings.turnstile_provider = nxt
                    self.settings.turnstile_headless = False
                self._persist_runtime_settings(
                    "Turnstile 已保存: "
                    + _turnstile_provider_label(
                        self.settings.turnstile_provider,
                        headless=self.settings.turnstile_headless,
                    )
                )
            elif index == 3:
                value = self._modal("注册数量", "数量", str(self.settings.count))
                if value is not None:
                    self.settings.count = _positive_int(value, "注册数量", MAX_COUNT)
                    self._persist_runtime_settings(f"注册数量已保存: {self.settings.count}")
            elif index == 4:
                value = self._modal("并发工作线程", "并发数", str(self.settings.workers))
                if value is not None:
                    self.settings.workers = _positive_int(value, "并发数", MAX_WORKERS)
                    self._persist_runtime_settings(f"并发数已保存: {self.settings.workers}")
            elif index == 5:
                value = self._modal("OAuth 输出", "目录", str(self.settings.output_dir))
                if value:
                    self.settings.output_dir = _absolute_path(value)
                    self._persist_runtime_settings(f"OAuth 输出已保存: {self.settings.output_dir}")
            elif index == 6:
                order = ["auto", "none", "direct", "pool"]
                current = "none" if self.settings.no_proxy else self.settings.proxy_mode
                next_mode = order[(order.index(current) + 1) % len(order)] if current in order else "auto"
                self.settings.no_proxy = next_mode == "none"
                self.settings.proxy_mode = next_mode
                self._persist_runtime_settings(f"代理模式已保存: {_proxy_mode_label(next_mode)}")
            elif index == 7:
                value = self._modal(
                    "SSO转换重试",
                    "次数(1-20)",
                    str(self.settings.sso_convert_retries),
                )
                if value is not None:
                    self.settings.sso_convert_retries = _bounded_int(
                        value,
                        "SSO转换重试次数",
                        minimum=1,
                        maximum=MAX_SSO_CONVERT_RETRIES,
                        default=DEFAULT_SSO_CONVERT_RETRIES,
                    )
                    self._persist_runtime_settings(
                        f"SSO转换重试已保存: {self.settings.sso_convert_retries} 次"
                    )
            elif index == 8:
                value = self._modal(
                    "SSO转换冷却",
                    "秒数(0-120)",
                    str(self.settings.sso_convert_cooldown),
                )
                if value is not None:
                    self.settings.sso_convert_cooldown = _bounded_int(
                        value,
                        "SSO转换冷却秒数",
                        minimum=0,
                        maximum=MAX_SSO_CONVERT_COOLDOWN,
                        default=DEFAULT_SSO_CONVERT_COOLDOWN,
                    )
                    self._persist_runtime_settings(
                        f"SSO转换冷却已保存: {self.settings.sso_convert_cooldown}s"
                    )
            elif index == 9:
                self.message = "浏览器状态: " + format_browser_health()
            elif index == 10:
                if self._confirm("清理 Playwright 残留和 /tmp 临时浏览器目录？"):
                    result = cleanup_browser_residues(kill_playwright=True, kill_all_chrome=False)
                    self.message = format_cleanup_result(result)
                else:
                    self.message = "已取消清理。"
            elif index == 11:
                refresh_settings_config(self.settings, reset_defaults=False)
                self.message = "配置已重新加载；当前运行设置保持不变。"
            elif index == 12:
                self._persist_runtime_settings(f"配置已写入: {self.settings.config_path}")
            elif index == 13:
                try:
                    persist_settings(self.settings)
                except TuiConfigError as exc:
                    self.message = f"错误: {exc}"
                    return
                # Soft preflight for local Turnstile.
                if _normalize_turnstile_provider(self.settings.turnstile_provider) == "local":
                    health = browser_health_status()
                    if int(health.get("chrome_count") or 0) >= 80 or int(health.get("playwright_count") or 0) >= 30:
                        self.message = (
                            "警告: 浏览器残留偏高（"
                            + format_browser_health(health)
                            + "）。建议先执行“清理残留”再开始。"
                        )
                self._start_run()
            elif index == 14:
                self.mode = "exit"
        except TuiConfigError as exc:
            self.message = f"错误: {exc}"


    def _handle_form_key(self, key: object) -> None:
        rows = 15
        if key in (curses.KEY_UP, "k"):
            self.selected = (self.selected - 1) % rows
        elif key in (curses.KEY_DOWN, "j", "\t"):
            self.selected = (self.selected + 1) % rows
        elif key in ("\n", "\r", curses.KEY_ENTER, " "):
            self._edit_field(self.selected)
        elif key in ("s", "S"):
            self._start_run()
        elif key in ("q", "Q", "\x1b"):
            self.mode = "exit"

    def _handle_dashboard_key(self, key: object) -> None:
        assert self.runner is not None
        if key in (curses.KEY_UP, "k"):
            self.log_scroll = min(self.log_scroll + 1, max(0, len(self.runner.logs) - 1))
        elif key in (curses.KEY_DOWN, "j"):
            self.log_scroll = max(0, self.log_scroll - 1)
        elif key in ("l", "L"):
            self.log_scroll = 0
        elif key in ("q", "Q", "\x1b"):
            if self.runner.done:
                self.mode = "exit"
            elif self._confirm("停止所有活动中的协议任务？"):
                self.runner.stop()

    def run(self) -> int:
        if self.auto_start:
            self._start_run()
        while self.mode != "exit":
            if self.mode == "dashboard":
                assert self.runner is not None
                self.runner.tick()
                self._draw_dashboard()
            else:
                self._draw_form()
            try:
                key = self.screen.get_wch()
            except curses.error:
                continue
            if self.mode == "dashboard":
                self._handle_dashboard_key(key)
            else:
                self._handle_form_key(key)
        if self.runner is not None and not self.runner.done:
            self.runner.stop()
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline and not self.runner.done:
                self.runner.tick()
                time.sleep(0.05)
        return self.runner.exit_code() if self.runner else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="全屏 HTTP 协议注册 TUI（仅 local Turnstile 临时开浏览器）")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH), help="邮箱和验证码 JSON 配置")
    parser.add_argument(
        "--mode",
        default=None,
        choices=list(RUN_MODE_ORDER),
        help="运行模式：register_otp=模式1注册+otp；register_sso=模式2注册+sso转换(Device Flow)；默认读配置",
    )
    parser.add_argument("--count", type=int, default=None, help="注册任务数量")
    parser.add_argument("--workers", "--concurrency", dest="workers", type=int, default=None, help="最大并发数")
    parser.add_argument("--output-dir", default=None, help="OAuth 凭证输出目录；默认读配置 xai_oauth_output_dir")
    parser.add_argument("--no-proxy", action="store_true", help="不使用已配置的代理设置")
    parser.add_argument("--dry-run", action="store_true", help="只打印计划，不发送请求")
    parser.add_argument("--yes", action="store_true", help="打开仪表盘并立即开始")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    argv_list = list(argv) if argv is not None else None
    parser = build_parser()
    args = parser.parse_args(argv_list)
    # 记录哪些 CLI 参数是显式传入的，避免覆盖配置文件默认值。
    explicit: set[str] = set()
    tokens = list(argv_list if argv_list is not None else sys.argv[1:])
    i = 0
    while i < len(tokens):
        token = tokens[i]
        if token in {"--mode"}:
            explicit.add("mode")
        elif token in {"--output-dir"}:
            explicit.add("output_dir")
        elif token in {"--count"}:
            explicit.add("count")
        elif token in {"--workers", "--concurrency"}:
            explicit.add("workers")
        elif token in {"--no-proxy"}:
            explicit.add("no_proxy")
        i += 1
    args._explicit_cli = explicit  # type: ignore[attr-defined]
    if "mode" in explicit and args.mode is None:
        args.mode = DEFAULT_RUN_MODE
    try:
        settings = settings_from_args(args)
        plan = build_plan(settings)
    except TuiConfigError as exc:
        print(f"[!] {exc}", file=sys.stderr)
        return 2

    if args.dry_run:
        print(describe_plan(plan, dry_run=True))
        return 0
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        print(
            "[!] HTTP TUI 需要交互式终端。非交互校验请使用 --dry-run。",
            file=sys.stderr,
        )
        return 2
    try:
        return int(curses.wrapper(lambda screen: ProtocolTui(screen, settings, auto_start=args.yes).run()))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
