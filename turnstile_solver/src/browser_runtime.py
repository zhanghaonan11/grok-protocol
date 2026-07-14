from __future__ import annotations

import hashlib
import os
import signal
import re
import shutil
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Dict, Optional, Tuple

from .browser_worker import BrowserWorker, prepare_browser_proxy, stop_browser_proxy
from .config import SolverConfig
from .models import PoolStats, SolveRequest, SolveResult
from .proxy import normalize_proxy


_DEFAULT_BROWSER_CONTEXT_IDS = frozenset(
    {"default", "default-context", "default_context", "defaultcontext"}
)


def _digest_secret(value: str) -> str:
    normalized = normalize_proxy(value)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16] if normalized else "direct"


def _normalize_browser_path(value: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    path = Path(raw).expanduser()
    try:
        path = path.resolve(strict=False)
    except OSError:
        pass
    return os.path.normcase(os.path.normpath(str(path)))


def _validate_root_sandbox_policy(no_sandbox: bool) -> None:
    get_euid = getattr(os, "geteuid", None)
    if callable(get_euid) and int(get_euid()) == 0 and not no_sandbox:
        raise RuntimeError(
            "检测到 root 运行 Chrome，但 no_sandbox 未显式启用；"
            "请在配置设置 no_sandbox=true 或设置 TURNSTILE_NO_SANDBOX=true"
        )


def _read_browser_full_version(browser_path: str) -> str:
    path = str(browser_path or "").strip()
    if not path:
        raise RuntimeError("browser_path is required to determine the Chrome version")
    try:
        completed = subprocess.run(
            [path, "--version"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
            shell=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError(f"无法执行 browser_path --version: {exc}") from exc
    output = f"{completed.stdout or ''} {completed.stderr or ''}".strip()
    match = re.search(r"\b(\d+\.\d+\.\d+\.\d+)\b", output)
    if not match:
        raise RuntimeError(f"无法从 browser_path --version 解析完整 Chrome 版本: {output!r}")
    return match.group(1)


def _require_browser_version(browser_path: str, expected_major: int) -> str:
    expected = max(0, int(expected_major or 0))
    if expected <= 0:
        raise RuntimeError("严格指纹模式缺少 expected_browser_major，拒绝启动 Chrome")
    version = _read_browser_full_version(browser_path)
    actual_major = int(version.split(".", 1)[0])
    if actual_major != expected:
        raise RuntimeError(
            "browser_path Chrome 主版本不一致: "
            f"expected={expected}, actual={actual_major}, full_version={version}"
        )
    return version


def _browser_pid(browser) -> int:
    """Best-effort extract Chromium root pid from DrissionPage browser object."""
    if browser is None:
        return 0
    for name in ("process_id", "pid", "_process_id"):
        value = getattr(browser, name, 0)
        try:
            value = value() if callable(value) else value
            pid = int(value or 0)
        except (TypeError, ValueError):
            pid = 0
        if pid > 1:
            return pid
    # Some wrappers keep a Popen/process object.
    for name in ("process", "_process", "browser_process"):
        proc = getattr(browser, name, None)
        if proc is None:
            continue
        try:
            pid = int(getattr(proc, "pid", 0) or 0)
        except (TypeError, ValueError):
            pid = 0
        if pid > 1:
            return pid
    return 0


def _reap_zombie_children() -> int:
    """Reap any already-dead direct children of this process (non-blocking).

    Chrome launched via DrissionPage is often a direct child of turnstile_solver.
    If quit()/kill races, those children become zombies until the parent wait()s.
    """
    if os.name == "nt":
        return 0
    reaped = 0
    while True:
        try:
            pid, _status = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            break
        except OSError:
            break
        if pid <= 0:
            break
        reaped += 1
    return reaped


_SIGCHLD_REAPER_INSTALLED = False


def _install_sigchld_reaper() -> None:
    """Install a non-blocking SIGCHLD handler so exited chrome children are reaped promptly."""
    global _SIGCHLD_REAPER_INSTALLED
    if _SIGCHLD_REAPER_INSTALLED or os.name == "nt":
        return
    try:
        def _handler(signum, frame):  # noqa: ARG001
            try:
                _reap_zombie_children()
            except Exception:
                pass

        signal.signal(signal.SIGCHLD, _handler)
        _SIGCHLD_REAPER_INSTALLED = True
    except Exception:
        # Some environments disallow custom SIGCHLD handlers; periodic reap still works.
        pass


def _reap_chrome_process_tree(pid: int, *, timeout_sec: float = 2.0) -> None:
    """Terminate a Chrome process tree and wait so children do not become zombies.

    Parent solvers historically called browser.quit() without waiting long enough,
    leaving dozens of `[chrome] <defunct>` entries under turnstile_solver.
    """
    pid = int(pid or 0)
    if pid <= 1:
        return
    try:
        import psutil
    except Exception:
        # Fallback: best-effort kill/wait without psutil.
        try:
            import os
            import signal
            import time as _time

            os.kill(pid, signal.SIGTERM)
            deadline = _time.time() + max(0.2, float(timeout_sec))
            while _time.time() < deadline:
                try:
                    waited_pid, _status = os.waitpid(pid, os.WNOHANG)
                except ChildProcessError:
                    return
                if waited_pid == pid:
                    return
                _time.sleep(0.05)
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                return
            try:
                os.waitpid(pid, 0)
            except ChildProcessError:
                pass
        except Exception:
            return
        return

    try:
        root = psutil.Process(pid)
    except (psutil.Error, OSError):
        return
    procs = []
    try:
        procs = root.children(recursive=True)
    except (psutil.Error, OSError):
        procs = []
    procs.append(root)
    # Graceful first.
    for proc in procs:
        try:
            proc.terminate()
        except (psutil.Error, OSError):
            pass
    try:
        psutil.wait_procs(procs, timeout=max(0.2, float(timeout_sec)))
    except Exception:
        pass
    # Force remaining.
    survivors = []
    for proc in procs:
        try:
            if proc.is_running():
                proc.kill()
                survivors.append(proc)
        except (psutil.Error, OSError):
            pass
    if survivors:
        try:
            psutil.wait_procs(survivors, timeout=max(0.2, float(timeout_sec)))
        except Exception:
            pass
    # Always drain any direct zombie children left behind by Chrome.
    try:
        _reap_zombie_children()
    except Exception:
        pass


@dataclass(frozen=True)
class BrowserAffinity:
    """Only process-level browser settings belong in this immutable key."""

    proxy_digest: str
    parent_proxy_digest: str
    user_agent_digest: str
    accept_language_digest: str
    ua_policy: str
    headless: bool
    locale: str
    browser_path: str
    expected_platform: str
    expected_client_hint_platform: str
    expected_browser_major: int
    no_sandbox: bool

    @classmethod
    def build(
        cls,
        *,
        proxy: str,
        parent_proxy: str,
        user_agent: str,
        headless: bool,
        locale: str,
        accept_language: str = "",
        browser_path: str = "",
        expected_platform: str = "",
        expected_client_hint_platform: str = "",
        expected_browser_major: int = 0,
        no_sandbox: bool = False,
    ) -> "BrowserAffinity":
        ua = str(user_agent or "").strip()
        language = str(accept_language or "").strip()
        return cls(
            proxy_digest=_digest_secret(proxy),
            parent_proxy_digest=_digest_secret(parent_proxy),
            user_agent_digest=hashlib.sha256(ua.encode("utf-8")).hexdigest()[:16] if ua else "native",
            accept_language_digest=(
                hashlib.sha256(language.encode("utf-8")).hexdigest()[:16] if language else "native"
            ),
            ua_policy="forced" if ua else "native",
            headless=bool(headless),
            locale=str(locale or "").strip(),
            browser_path=_normalize_browser_path(browser_path),
            expected_platform=str(expected_platform or "").strip(),
            expected_client_hint_platform=str(expected_client_hint_platform or "").strip(),
            expected_browser_major=max(0, int(expected_browser_major or 0)),
            no_sandbox=bool(no_sandbox),
        )

    @property
    def affinity_id(self) -> str:
        raw = "|".join(
            (
                self.proxy_digest,
                self.parent_proxy_digest,
                self.user_agent_digest,
                self.accept_language_digest,
                self.ua_policy,
                "headless" if self.headless else "headed",
                self.locale,
                self.browser_path,
                self.expected_platform,
                self.expected_client_hint_platform,
                str(self.expected_browser_major),
                "no-sandbox" if self.no_sandbox else "sandboxed",
            )
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]


class BrowserSlot:
    """One persistent Chromium process, serialized to one solve at a time."""

    def __init__(
        self,
        config: SolverConfig,
        worker: BrowserWorker,
        *,
        affinity: BrowserAffinity,
        upstream_proxy: str,
        parent_proxy: str,
        user_agent: str,
    ):
        self.config = config
        self.worker = worker
        self.affinity = affinity
        self.upstream_proxy_raw = str(upstream_proxy or "")
        self.parent_proxy_raw = str(parent_proxy or "")
        self.user_agent = str(user_agent or "").strip()
        self.slot_id = uuid.uuid4().hex[:12]
        self.browser = None
        self.options = None
        self.browser_proxy = ""
        self.upstream_proxy = ""
        self.forwarder_instance = ""
        self.profile_dir = ""
        self.browser_version = ""
        self.created_monotonic = 0.0
        self.last_used_monotonic = 0.0
        self.completed_tasks = 0
        self.consecutive_failures = 0
        self.context_close_failed = False
        self._seen_context_ids: set[str] = set()
        self._dispose_attempted_context_ids: set[str] = set()
        self._closed = False
        self._virtual_display = None
        self._browser_mode = "headed"
        self._launch_display = ""

    @staticmethod
    def _run_browser_cdp(browser, method: str, **params):
        """Run CDP on the browser session, never on a page target session."""

        runner = getattr(browser, "_run_cdp", None)
        if not callable(runner):
            runner = getattr(browser, "run_cdp", None)
        if not callable(runner):
            raise RuntimeError("browser-level CDP is unavailable")
        response = runner(method, **params)
        if isinstance(response, dict) and response.get("error"):
            raise RuntimeError(f"browser-level CDP {method} failed: {response['error']}")
        return response

    @staticmethod
    def _validate_browser_context_id(value) -> str:
        context_id = str(value or "").strip()
        if not context_id:
            raise RuntimeError("fresh browser context id is missing")
        if context_id.lower() in _DEFAULT_BROWSER_CONTEXT_IDS:
            raise RuntimeError("refusing to use or dispose the default browser context")
        return context_id

    def _browser_context_id_for_page(self, page) -> str:
        target_id = ""
        for name in ("tab_id", "target_id", "targetId"):
            try:
                value = getattr(page, name, "")
                value = value() if callable(value) else value
            except Exception:
                continue
            target_id = str(value or "").strip()
            if target_id:
                break

        lookup_error = ""
        if target_id:
            try:
                response = self._run_browser_cdp(
                    self.browser,
                    "Target.getTargetInfo",
                    targetId=target_id,
                )
                target_info = response.get("targetInfo", {}) if isinstance(response, dict) else {}
                if "browserContextId" in target_info:
                    context_id = self._validate_browser_context_id(
                        target_info.get("browserContextId")
                    )
                    return self._register_browser_context_id(context_id)
            except Exception as exc:
                lookup_error = str(exc)

        for name in (
            "browser_context_id",
            "browserContextId",
            "_browser_context_id",
            "context_id",
            "_context_id",
        ):
            try:
                value = getattr(page, name, "")
                value = value() if callable(value) else value
            except Exception:
                continue
            if value is None or not str(value).strip():
                continue
            context_id = self._validate_browser_context_id(value)
            return self._register_browser_context_id(context_id)

        suffix = f": {lookup_error}" if lookup_error else ""
        raise RuntimeError(f"unable to identify the fresh browser context{suffix}")

    def _register_browser_context_id(self, context_id: str) -> str:
        context_id = self._validate_browser_context_id(context_id)
        if context_id in self._seen_context_ids:
            raise RuntimeError("fresh browser context id was reused")
        self._seen_context_ids.add(context_id)
        return context_id

    def _close_page_and_dispose_context(self, page, context_id: str) -> str:
        """Close the target first, then dispose its non-default context exactly once."""

        errors = []
        try:
            page.close()
        except Exception as exc:
            errors.append(f"page close failed: {exc}")

        try:
            context_id = self._validate_browser_context_id(context_id)
        except Exception as exc:
            errors.append(str(exc))
            return "; ".join(errors)

        if context_id in self._dispose_attempted_context_ids:
            errors.append("browser context disposal was already attempted")
            return "; ".join(errors)
        self._dispose_attempted_context_ids.add(context_id)
        try:
            self._run_browser_cdp(
                self.browser,
                "Target.disposeBrowserContext",
                browserContextId=context_id,
            )
        except Exception as exc:
            errors.append(f"browser context disposal failed: {exc}")
        return "; ".join(errors)

    @staticmethod
    def _record_context_cleanup(result: SolveResult, cleanup_error: str) -> None:
        extras = dict(result.extras or {})
        extras["browser_context_cleanup"] = "failed" if cleanup_error else "disposed"
        if cleanup_error:
            extras["browser_context_cleanup_error"] = cleanup_error
        result.extras = extras


    def _resolve_launch_headless(self) -> bool:
        """Map requested headless affinity to an actual Chrome launch mode.

        accounts.x.ai consistently hard-blocks pure headless Chrome. When the
        caller asks for headless, prefer Xvfb virtual-headed (or real headed if
        a display exists) so local Turnstile capture stays viable.
        """
        want_headless = bool(self.affinity.headless)
        if not want_headless:
            self._browser_mode = "headed"
            self._launch_display = str(os.environ.get("DISPLAY") or "")
            return False
        try:
            from xai_http_flow import (
                _resolve_local_browser_mode,
                _VirtualDisplaySession,
            )
        except Exception:
            self._browser_mode = "headless-new"
            self._launch_display = str(os.environ.get("DISPLAY") or "")
            return True

        mode, use_headless = _resolve_local_browser_mode(want_headless=True)
        if mode == "virtual-headed":
            virtual = _VirtualDisplaySession(log_callback=self.worker.log_callback)
            if virtual.start():
                self._virtual_display = virtual
                self._browser_mode = "virtual-headed"
                self._launch_display = str(os.environ.get("DISPLAY") or "")
                return False
            self._browser_mode = "headless-new"
            self._launch_display = str(os.environ.get("DISPLAY") or "")
            return True
        if mode == "headed":
            self._browser_mode = "headed-fallback"
            self._launch_display = str(os.environ.get("DISPLAY") or "")
            return False
        self._browser_mode = "headless-new"
        self._launch_display = str(os.environ.get("DISPLAY") or "")
        return bool(use_headless)

    def _stop_virtual_display(self) -> None:
        virtual = self._virtual_display
        self._virtual_display = None
        if virtual is None:
            return
        try:
            virtual.stop()
        except Exception:
            pass

    def start(self) -> None:
        if self.browser is not None:
            return
        _validate_root_sandbox_policy(self.affinity.no_sandbox)
        if self.config.strict_fingerprint:
            self.browser_version = _require_browser_version(
                self.affinity.browser_path,
                self.affinity.expected_browser_major,
            )
        elif self.affinity.browser_path:
            self.browser_version = _read_browser_full_version(self.affinity.browser_path)
            if (
                self.affinity.expected_browser_major > 0
                and int(self.browser_version.split(".", 1)[0])
                != self.affinity.expected_browser_major
            ):
                raise RuntimeError(
                    "browser_path Chrome 主版本不一致: "
                    f"expected={self.affinity.expected_browser_major}, "
                    f"actual={self.browser_version}"
                )
        try:
            self.browser_proxy, self.upstream_proxy, self.forwarder_instance = prepare_browser_proxy(
                self.upstream_proxy_raw,
                parent_proxy=self.parent_proxy_raw,
                preferred_local_port=0,
                instance_key=f"ts-slot-{self.slot_id}",
            )
            try:
                from DrissionPage import Chromium, ChromiumOptions
            except Exception as exc:
                raise RuntimeError(f"常驻浏览器池需要 DrissionPage/Chrome: {exc}") from exc

            try:
                from xai_http_flow import _build_turnstile_browser_options as build_options
            except Exception:
                build_options = None

            options = ChromiumOptions()
            if self.affinity.browser_path:
                set_browser_path = getattr(options, "set_browser_path", None)
                if not callable(set_browser_path):
                    raise RuntimeError("当前 ChromiumOptions 不支持 set_browser_path")
                set_browser_path(self.affinity.browser_path)
            use_headless = self._resolve_launch_headless()
            try:
                from xai_http_flow import _log as _ts_log
            except Exception:
                _ts_log = None
            if _ts_log is not None:
                _ts_log(
                    self.worker.log_callback,
                    f"[Turnstile] 浏览器池启动 mode={self._browser_mode} "
                    f"headless={use_headless} display={self._launch_display or '-'}",
                )
            if build_options is not None:
                options = build_options(
                    options=options,
                    proxy=self.browser_proxy,
                    headless=bool(use_headless),
                    user_agent=self.user_agent,
                    log_callback=self.worker.log_callback,
                )
            else:
                try:
                    options.auto_port()
                except Exception:
                    pass
                if use_headless:
                    options.headless(True)
                if self.user_agent:
                    options.set_user_agent(self.user_agent)
                if self.browser_proxy:
                    options.set_proxy(self.browser_proxy)
            if self.affinity.no_sandbox:
                set_argument = getattr(options, "set_argument", None)
                if not callable(set_argument):
                    raise RuntimeError("当前 ChromiumOptions 不支持 set_argument，无法启用 --no-sandbox")
                set_argument("--no-sandbox")
            else:
                remove_argument = getattr(options, "remove_argument", None)
                if not callable(remove_argument):
                    raise RuntimeError(
                        "当前 ChromiumOptions 不支持 remove_argument，无法保证 sandbox 启动策略"
                    )
                remove_argument("--no-sandbox")
            if self.affinity.locale:
                try:
                    options.set_argument(f"--lang={self.affinity.locale}")
                except Exception:
                    pass
            self.options = options
            self.profile_dir = str(getattr(options, "_xai_profile_dir", "") or "")
            self.browser = Chromium(options)

            # Feature-probe real browser-context isolation. Never silently fall
            # back to a cookie-sharing normal tab in strict mode.
            probe = None
            probe_context_id = ""
            try:
                probe = self.browser.new_tab(new_context=True)
                probe_context_id = self._browser_context_id_for_page(probe)
            except TypeError as exc:
                raise RuntimeError(
                    "当前 DrissionPage 不支持 new_tab(new_context=True)，严格隔离拒绝启动"
                ) from exc
            finally:
                if probe is not None:
                    cleanup_error = self._close_page_and_dispose_context(
                        probe,
                        probe_context_id,
                    )
                    if cleanup_error:
                        self.context_close_failed = True
                        raise RuntimeError(
                            f"fresh browser context probe cleanup failed: {cleanup_error}"
                        )
            self.created_monotonic = time.monotonic()
            self.last_used_monotonic = self.created_monotonic
        except Exception:
            self._stop_virtual_display()
            self.close()
            raise

    def solve(self, request: SolveRequest) -> SolveResult:
        if self._closed or self.browser is None:
            return SolveResult(ok=False, error="browser slot is not available")
        accept_language = str(request.accept_language or self.config.accept_language or "").strip()
        locale = self.config.locale or accept_language.split(",", 1)[0].split(";", 1)[0].strip()
        expected = BrowserAffinity.build(
            proxy=request.proxy or self.config.proxy,
            parent_proxy=str((request.metadata or {}).get("parent_proxy") or self.config.parent_proxy or ""),
            user_agent=request.user_agent or self.config.user_agent,
            headless=bool(request.headless or self.config.headless),
            locale=locale,
            accept_language=accept_language,
            browser_path=self.config.resolved_browser_path(),
            expected_platform=request.expected_platform,
            expected_client_hint_platform=request.expected_client_hint_platform,
            expected_browser_major=request.expected_browser_major,
            no_sandbox=self.config.resolved_no_sandbox(),
        )
        if expected != self.affinity:
            return SolveResult(ok=False, error="browser affinity mismatch")

        page = None
        context_id = ""
        result = SolveResult(ok=False, error="browser context was not created")
        try:
            try:
                page = self.browser.new_tab(new_context=True)
                context_id = self._browser_context_id_for_page(page)
            except TypeError as exc:
                raise RuntimeError(
                    "当前 DrissionPage 不支持 new_tab(new_context=True)，严格隔离拒绝执行"
                ) from exc
            result = self.worker.solve_on_page(
                page,
                request,
                browser_proxy=self.browser_proxy,
                upstream_proxy=self.upstream_proxy or normalize_proxy(self.upstream_proxy_raw),
                affinity_id=self.affinity.affinity_id,
                browser_version=self.browser_version,
            )
            return result
        except Exception as exc:
            result = SolveResult(
                ok=False,
                proxy=self.upstream_proxy or normalize_proxy(self.upstream_proxy_raw),
                page_url=request.page_url,
                error=str(exc),
                extras={"affinity_id": self.affinity.affinity_id},
            )
            return result
        finally:
            self.last_used_monotonic = time.monotonic()
            if page is not None:
                cleanup_error = self._close_page_and_dispose_context(page, context_id)
                self._record_context_cleanup(result, cleanup_error)
                if cleanup_error:
                    self.context_close_failed = True

    def rss_mb(self) -> int:
        if self.browser is None:
            return 0
        pid = 0
        for name in ("process_id", "pid"):
            value = getattr(self.browser, name, 0)
            try:
                value = value() if callable(value) else value
                pid = int(value or 0)
            except (TypeError, ValueError):
                pid = 0
            if pid:
                break
        if not pid:
            return 0
        try:
            import psutil

            process = psutil.Process(pid)
            total = process.memory_info().rss
            for child in process.children(recursive=True):
                try:
                    total += child.memory_info().rss
                except (psutil.Error, OSError):
                    pass
            return int(total / (1024 * 1024))
        except Exception:
            return 0

    def recycle_reason(self) -> str:
        now = time.monotonic()
        if self._closed or self.browser is None:
            return "closed"
        if self.context_close_failed:
            return "context_close_failed"
        if self.completed_tasks >= self.config.browser_max_tasks:
            return "max_tasks"
        if self.consecutive_failures >= self.config.browser_max_consecutive_failures:
            return "consecutive_failures"
        if self.created_monotonic and now - self.created_monotonic >= self.config.browser_max_age_sec:
            return "max_age"
        if (
            self.config.browser_idle_ttl_sec > 0
            and self.last_used_monotonic
            and now - self.last_used_monotonic >= self.config.browser_idle_ttl_sec
        ):
            return "idle_ttl"
        rss = self.rss_mb()
        if self.config.browser_max_rss_mb > 0 and rss > self.config.browser_max_rss_mb:
            return "max_rss"
        try:
            self.browser.get_tabs()
        except Exception:
            return "browser_disconnected"
        return ""

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        browser, self.browser = self.browser, None
        browser_pid = _browser_pid(browser)
        if browser is not None:
            try:
                browser.quit()
            except Exception:
                pass
        # Always reap the process tree. quit() alone can leave zombie chrome under
        # long-lived turnstile_solver parents.
        try:
            _reap_chrome_process_tree(browser_pid, timeout_sec=2.0)
        except Exception:
            pass
        try:
            _reap_zombie_children()
        except Exception:
            pass
        stop_browser_proxy(self.forwarder_instance)
        self.forwarder_instance = ""
        if self.profile_dir:
            try:
                shutil.rmtree(Path(self.profile_dir), ignore_errors=True)
            except Exception:
                pass
        self._stop_virtual_display()


class PersistentBrowserPool:
    def __init__(self, config: SolverConfig, worker: Optional[BrowserWorker] = None):
        self.config = config
        self.worker = worker or BrowserWorker(config)
        self.stats = PoolStats(max_concurrency=max(1, int(config.max_concurrency)))
        self._condition = threading.Condition(threading.Lock())
        self._slots: Dict[str, BrowserSlot] = {}
        self._busy: set[str] = set()
        self._creating = 0
        self._waiters = 0
        self._started = False
        self._closed = False
        self._maintenance_stop = threading.Event()
        self._maintenance_thread: Optional[threading.Thread] = None

    def start(self) -> None:
        with self._condition:
            if self._closed:
                raise RuntimeError("browser pool is closed")
            if self._started:
                return
            self._started = True
            try:
                _install_sigchld_reaper()
            except Exception:
                pass
            thread = threading.Thread(
                target=self._maintenance_loop,
                name="turnstile-browser-pool-maintenance",
                daemon=True,
            )
            self._maintenance_thread = thread
        thread.start()

    def _maintenance_loop(self) -> None:
        interval = max(0.05, float(self.config.browser_maintenance_interval_sec or 5.0))
        while not self._maintenance_stop.wait(interval):
            try:
                _reap_zombie_children()
            except Exception:
                pass
            self._reap_idle_slots()

    def _reap_idle_slots(self) -> int:
        ttl = max(0, int(self.config.browser_idle_ttl_sec or 0))
        if ttl <= 0:
            return 0
        now = time.monotonic()
        with self._condition:
            if self._closed:
                return 0
            stale = [
                slot
                for slot_id, slot in self._slots.items()
                if slot_id not in self._busy
                and slot.last_used_monotonic
                and now - slot.last_used_monotonic >= ttl
            ]
            for slot in stale:
                self._slots.pop(slot.slot_id, None)
            if stale:
                self.stats.recycling_slots += len(stale)
                self._refresh_stats_locked()
                self._condition.notify_all()
        for slot in stale:
            slot.close()
            self._record_recycle("idle_ttl")
        if stale:
            with self._condition:
                self.stats.recycling_slots = max(
                    0, self.stats.recycling_slots - len(stale)
                )
                self._refresh_stats_locked()
        return len(stale)

    def _refresh_stats_locked(self) -> None:
        self.stats.active_workers = len(self._busy)
        self.stats.busy_slots = len(self._busy)
        self.stats.ready_slots = max(0, len(self._slots) - len(self._busy))
        self.stats.starting_slots = self._creating
        self.stats.queue_depth = self._waiters
        self.stats.affinity_count = len({s.affinity.affinity_id for s in self._slots.values()})

    def _record_recycle(self, reason: str) -> None:
        with self._condition:
            self.stats.browser_restarts += 1
            self.stats.recycle_reasons[reason] = self.stats.recycle_reasons.get(reason, 0) + 1
            self._refresh_stats_locked()

    def _request_parts(self, request: SolveRequest) -> Tuple[BrowserAffinity, str, str, str]:
        proxy = str(request.proxy or self.config.proxy or "").strip()
        parent = str((request.metadata or {}).get("parent_proxy") or self.config.parent_proxy or "").strip()
        ua = str(request.user_agent or self.config.user_agent or "").strip()
        accept_language = str(request.accept_language or self.config.accept_language or "").strip()
        locale = self.config.locale or accept_language.split(",", 1)[0].split(";", 1)[0].strip()
        affinity = BrowserAffinity.build(
            proxy=proxy,
            parent_proxy=parent,
            user_agent=ua,
            headless=bool(request.headless or self.config.headless),
            locale=locale,
            accept_language=accept_language,
            browser_path=self.config.resolved_browser_path(),
            expected_platform=request.expected_platform,
            expected_client_hint_platform=request.expected_client_hint_platform,
            expected_browser_major=request.expected_browser_major,
            no_sandbox=self.config.resolved_no_sandbox(),
        )
        return affinity, proxy, parent, ua

    def _acquire(self, request: SolveRequest, deadline: float) -> Optional[BrowserSlot]:
        affinity, proxy, parent, ua = self._request_parts(request)
        waiter_counted = False
        try:
            while True:
                retire: Optional[Tuple[BrowserSlot, str]] = None
                create = False
                with self._condition:
                    if self._closed:
                        raise RuntimeError("browser pool is closed")
                    for slot in self._slots.values():
                        if slot.slot_id not in self._busy and slot.affinity == affinity:
                            reason = slot.recycle_reason()
                            if reason:
                                self._slots.pop(slot.slot_id, None)
                                retire = (slot, reason)
                                break
                            self._busy.add(slot.slot_id)
                            self._refresh_stats_locked()
                            return slot
                    if retire is None and len(self._slots) + self._creating < self.stats.max_concurrency:
                        self._creating += 1
                        self._refresh_stats_locked()
                        create = True
                    elif retire is None:
                        idle = [s for s in self._slots.values() if s.slot_id not in self._busy]
                        if idle:
                            victim = min(idle, key=lambda s: s.last_used_monotonic)
                            self._slots.pop(victim.slot_id, None)
                            retire = (victim, "affinity_eviction")
                        else:
                            remaining = deadline - time.monotonic()
                            if remaining <= 0:
                                return None
                            if not waiter_counted:
                                self._waiters += 1
                                waiter_counted = True
                                self._refresh_stats_locked()
                            self._condition.wait(timeout=remaining)
                            continue

                if retire is not None:
                    retire[0].close()
                    self._record_recycle(retire[1])
                    continue

                if create:
                    slot = BrowserSlot(
                        self.config,
                        self.worker,
                        affinity=affinity,
                        upstream_proxy=proxy,
                        parent_proxy=parent,
                        user_agent=ua,
                    )
                    try:
                        slot.start()
                    except Exception:
                        with self._condition:
                            self._creating = max(0, self._creating - 1)
                            self._refresh_stats_locked()
                            self._condition.notify_all()
                        raise
                    with self._condition:
                        self._creating = max(0, self._creating - 1)
                        if self._closed:
                            slot.close()
                            raise RuntimeError("browser pool is closed")
                        self._slots[slot.slot_id] = slot
                        self._busy.add(slot.slot_id)
                        self.stats.browser_starts += 1
                        self._refresh_stats_locked()
                        return slot
        finally:
            if waiter_counted:
                with self._condition:
                    self._waiters = max(0, self._waiters - 1)
                    self._refresh_stats_locked()

    def _release(self, slot: BrowserSlot, result: SolveResult) -> None:
        slot.completed_tasks += 1
        if result.ok:
            slot.consecutive_failures = 0
        else:
            slot.consecutive_failures += 1
        reason = slot.recycle_reason()
        close_slot = False
        with self._condition:
            self._busy.discard(slot.slot_id)
            if reason:
                self._slots.pop(slot.slot_id, None)
                close_slot = True
                self.stats.recycling_slots += 1
            self._refresh_stats_locked()
            self._condition.notify_all()
        if close_slot:
            slot.close()
            self._record_recycle(reason)
            with self._condition:
                self.stats.recycling_slots = max(0, self.stats.recycling_slots - 1)
                self._refresh_stats_locked()

    def solve(self, request: SolveRequest) -> SolveResult:
        self.start()
        started = time.monotonic()
        total_budget = max(1, int(request.timeout_sec or self.config.queue_timeout_sec))
        deadline = started + total_budget
        slot: Optional[BrowserSlot] = None
        result = SolveResult(ok=False, error="solver did not run")
        try:
            slot = self._acquire(request, deadline)
            if slot is None:
                result = SolveResult(
                    ok=False,
                    error="solver pool busy: acquire timeout",
                    elapsed_ms=int((time.monotonic() - started) * 1000),
                )
                return result
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                result = SolveResult(ok=False, error="solver deadline exhausted before capture")
                return result
            effective = replace(request, timeout_sec=max(1, int(remaining)))
            result = slot.solve(effective)
            with self._condition:
                if result.ok:
                    self.stats.completed += 1
                else:
                    self.stats.failed += 1
                    self.stats.last_error = result.error
            return result
        except Exception as exc:
            result = SolveResult(
                ok=False,
                error=str(exc),
                elapsed_ms=int((time.monotonic() - started) * 1000),
            )
            with self._condition:
                self.stats.failed += 1
                self.stats.last_error = result.error
            return result
        finally:
            if slot is not None:
                self._release(slot, result)

    def close(self) -> None:
        with self._condition:
            if self._closed:
                return
            self._closed = True
            self._maintenance_stop.set()
            maintenance_thread = self._maintenance_thread
            self._maintenance_thread = None
            slots = list(self._slots.values())
            self._slots.clear()
            self._busy.clear()
            self._refresh_stats_locked()
            self._condition.notify_all()
        if (
            maintenance_thread is not None
            and maintenance_thread is not threading.current_thread()
        ):
            maintenance_thread.join(timeout=max(
                1.0, float(self.config.browser_maintenance_interval_sec) * 2
            ))
        for slot in slots:
            slot.close()
        try:
            _reap_zombie_children()
        except Exception:
            pass
