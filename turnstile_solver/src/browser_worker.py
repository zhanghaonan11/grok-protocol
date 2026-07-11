from __future__ import annotations

import os
import secrets
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from .config import SolverConfig
from .models import SolveRequest, SolveResult
from .proxy import normalize_proxy, parse_proxy

LogFn = Optional[Callable[[str], None]]

# Parent repo root: .../grok协议
REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


CLICK_EMAIL_JS = r"""
function isVisible(node) {
    if (!node) return false;
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}
function nodeText(node) {
    return [
        node.innerText,
        node.textContent,
        node.getAttribute('aria-label'),
        node.getAttribute('title'),
        node.getAttribute('href'),
    ].filter(Boolean).join(' ').replace(/\s+/g, ' ').trim();
}
function scoreEntry(node) {
    const compact = nodeText(node).replace(/\s+/g, '');
    const lower = compact.toLowerCase();
    if (compact.includes('使用邮箱注册')) return 100;
    if (lower.includes('signupwithemail')) return 95;
    if (lower.includes('continuewithemail')) return 90;
    if (lower.includes('email') && (lower.includes('sign') || lower.includes('continue') || lower.includes('use') || lower.includes('with'))) return 80;
    if (lower === 'email' || lower.includes('邮箱')) return 70;
    return 0;
}
const candidates = Array.from(document.querySelectorAll('button, a, [role="button"]'))
    .filter((node) => isVisible(node) && !node.disabled && node.getAttribute('aria-disabled') !== 'true')
    .map((node) => ({ node, score: scoreEntry(node), text: nodeText(node) }))
    .filter((item) => item.score > 0)
    .sort((a, b) => b.score - a.score);
const target = candidates[0]?.node || null;
if (!target) return false;
target.click();
return candidates[0].text || true;
"""


def click_email_signup_entry(page: Any, *, log_callback: LogFn = None, timeout: int = 12) -> bool:
    deadline = time.monotonic() + max(3, int(timeout or 12))
    while time.monotonic() < deadline:
        try:
            clicked = page.run_js(CLICK_EMAIL_JS)
        except Exception:
            clicked = False
        if clicked:
            detail = f": {clicked}" if isinstance(clicked, str) else ""
            _log(log_callback, f"[Turnstile] 已点击邮箱注册入口{detail}")
            time.sleep(1.5)
            return True
        time.sleep(0.8)
    return False

TOKEN_READ_JS = """
const cf = document.querySelector('input[name="cf-turnstile-response"]');
let v = cf ? String(cf.value || '').trim() : '';
if ((!v || v.length < 20) && window.turnstile && typeof turnstile.getResponse === 'function') {
  try { v = String(turnstile.getResponse() || '').trim(); } catch (e) {}
}
return v;
"""

PAGE_DIAG_JS = """
const html = document.documentElement ? document.documentElement.innerHTML : '';
const bodyText = (document.body && document.body.innerText) ? document.body.innerText : '';
const cf = document.querySelector('input[name="cf-turnstile-response"]');
const token = cf ? String(cf.value || '').trim() : '';
let turnstileResp = '';
try {
  if (window.turnstile && typeof turnstile.getResponse === 'function') {
    turnstileResp = String(turnstile.getResponse() || '').trim();
  }
} catch (e) {}
const sitekeys = [];
const pushKey = (k) => {
  k = String(k || '').trim();
  if (k && !sitekeys.includes(k)) sitekeys.push(k);
};
document.querySelectorAll('[data-sitekey]').forEach(el => pushKey(el.getAttribute('data-sitekey')));
const m1 = html.match(/data-sitekey=[\"']([^\"']+)[\"']/ig) || [];
m1.forEach(s => {
  const mm = s.match(/data-sitekey=[\"']([^\"']+)[\"']/i);
  if (mm) pushKey(mm[1]);
});
const m2 = html.match(/sitekey[\"']?\\s*[:=]\\s*[\"']([^\"']+)[\"']/ig) || [];
m2.forEach(s => {
  const mm = s.match(/[\"']([^\"']+)[\"']\\s*$/i) || s.match(/[:=]\\s*[\"']([^\"']+)[\"']/i);
  if (mm) pushKey(mm[1]);
});
const iframes = [...document.querySelectorAll('iframe')].map(f => {
  const src = String(f.src || '');
  return {
    src: src.slice(0, 180),
    title: String(f.title || ''),
    id: String(f.id || ''),
    name: String(f.name || ''),
    isTurnstile: /turnstile|challenges\\.cloudflare\\.com|cf-chl|cloudflare/i.test(src + ' ' + (f.title||'') + ' ' + (f.name||'')),
  };
}).slice(0, 20);
const widgets = [...document.querySelectorAll('.cf-turnstile, [data-sitekey], #cf-turnstile, #turnstile-wrapper, iframe[src*="turnstile"], iframe[src*="challenges.cloudflare"]')].length;
return {
  url: location.href,
  title: document.title || '',
  readyState: document.readyState || '',
  hasCfInput: !!cf,
  tokenLen: token.length,
  turnstileApiType: typeof window.turnstile,
  turnstileRespLen: turnstileResp.length,
  sitekeys,
  sitekeyCount: sitekeys.length,
  iframeCount: iframes.length,
  turnstileIframeCount: iframes.filter(x => x.isTurnstile).length,
  iframes,
  widgetLikeCount: widgets,
  challengeLike: /(just a moment|checking your browser|cf-browser-verification|attention required|verify you are human)/i.test(html + ' ' + bodyText),
  bodySnippet: bodyText.replace(/\\s+/g, ' ').trim().slice(0, 220),
};
"""


def _log(log_callback: LogFn, message: str) -> None:
    if log_callback:
        log_callback(message)


def prepare_browser_proxy(
    proxy: str = "",
    *,
    parent_proxy: str = "",
    preferred_local_port: int = 0,
    instance_key: str = "",
) -> Tuple[str, str, str]:
    """Return (browser_proxy_url, upstream_proxy_url, forwarder_instance_key)."""
    upstream = normalize_proxy(proxy)
    parent = normalize_proxy(parent_proxy)
    if not upstream and not parent:
        return "", "", ""
    if parent and not upstream:
        raise RuntimeError("设置 parent_proxy 时必须同时提供 proxy 上游")

    parsed = parse_proxy(upstream)
    needs_forwarder = bool(parent) or bool(parsed.username or parsed.password)
    if not needs_forwarder:
        return upstream, upstream, ""

    from local_proxy_forwarder import ensure_local_forwarder

    key = instance_key or f"turnstile-solver-{os.getpid()}-{secrets.token_hex(3)}"
    browser_proxy, _used = ensure_local_forwarder(
        upstream,
        preferred_local_port=int(preferred_local_port or 0),
        instance_key=key,
        parent_proxy_raw=parent,
    )
    return str(browser_proxy or ""), upstream, key


def stop_browser_proxy(forwarder_instance: str = "") -> None:
    if not forwarder_instance:
        return
    try:
        from local_proxy_forwarder import stop_local_forwarder

        stop_local_forwarder(forwarder_instance)
    except Exception:
        pass


def read_turnstile_token_from_page(page: Any) -> str:
    try:
        value = page.run_js(TOKEN_READ_JS)
    except Exception:
        return ""
    return str(value or "").strip()


def diagnose_page(page: Any) -> Dict[str, Any]:
    try:
        data = page.run_js(PAGE_DIAG_JS)
    except Exception as exc:
        return {"error": f"diagnose failed: {exc}"}
    return data if isinstance(data, dict) else {"error": "diagnose returned non-object", "raw": str(data)}


def _summarize_diag(samples: List[Dict[str, Any]]) -> Dict[str, Any]:
    if not samples:
        return {}
    token_lens = [int(s.get("tokenLen") or 0) for s in samples if isinstance(s, dict)]
    resp_lens = [int(s.get("turnstileRespLen") or 0) for s in samples if isinstance(s, dict)]
    sitekeys: List[str] = []
    for s in samples:
        for k in s.get("sitekeys") or []:
            k = str(k or "").strip()
            if k and k not in sitekeys:
                sitekeys.append(k)
    last = samples[-1] if samples else {}
    return {
        "samples": len(samples),
        "token_len_max": max(token_lens) if token_lens else 0,
        "token_len_last": token_lens[-1] if token_lens else 0,
        "token_seen_nonzero": any(x > 0 for x in token_lens),
        "turnstile_resp_len_max": max(resp_lens) if resp_lens else 0,
        "sitekeys": sitekeys,
        "sitekey_count": len(sitekeys),
        "has_cf_input_last": bool(last.get("hasCfInput")),
        "turnstile_api_type_last": last.get("turnstileApiType"),
        "iframe_count_last": last.get("iframeCount"),
        "turnstile_iframe_count_last": last.get("turnstileIframeCount"),
        "challenge_like_last": bool(last.get("challengeLike")),
        "title_last": last.get("title"),
        "url_last": last.get("url"),
        "body_snippet_last": last.get("bodySnippet"),
        "iframes_last": last.get("iframes") or [],
        "timeline": [
            {
                "t": s.get("_t"),
                "tokenLen": s.get("tokenLen"),
                "turnstileRespLen": s.get("turnstileRespLen"),
                "hasCfInput": s.get("hasCfInput"),
                "turnstileIframeCount": s.get("turnstileIframeCount"),
                "sitekeyCount": s.get("sitekeyCount"),
                "challengeLike": s.get("challengeLike"),
            }
            for s in samples
        ],
    }


class BrowserWorker:
    """Capture a real Turnstile token from the signup page via Chromium."""

    def __init__(self, config: SolverConfig, log_callback: LogFn = None):
        self.config = config
        self.log_callback = log_callback

    def solve(self, request: SolveRequest) -> SolveResult:
        started = time.monotonic()
        page_url = (request.page_url or self.config.signup_url).strip()
        upstream_raw = (request.proxy or self.config.proxy or "").strip()
        parent_proxy = (
            str((request.metadata or {}).get("parent_proxy") or "")
            or self.config.parent_proxy
            or ""
        ).strip()
        timeout = max(30, int(request.timeout_sec or self.config.browser_timeout_sec))
        headless = bool(request.headless) or bool(self.config.headless)
        min_len = max(20, int(self.config.token_min_length or 80))
        user_agent = (request.user_agent or self.config.user_agent or "").strip()
        diagnose = bool((request.metadata or {}).get("diagnose"))

        forwarder_instance = ""
        try:
            browser_proxy, upstream_proxy, forwarder_instance = prepare_browser_proxy(
                upstream_raw,
                parent_proxy=parent_proxy,
                preferred_local_port=int(self.config.local_proxy_port or 0),
                instance_key=f"ts-worker-{uuid.uuid4().hex[:10]}",
            )
            token, effective_ua, diag = self._capture_with_browser(
                page_url=page_url,
                browser_proxy=browser_proxy,
                timeout=timeout,
                headless=headless,
                user_agent=user_agent,
                min_len=min_len,
                diagnose=diagnose or True,  # keep diagnostics on for Phase1 live debugging
            )
            elapsed_ms = int((time.monotonic() - started) * 1000)
            extras = {
                "browser_proxy": browser_proxy,
                "parent_proxy": normalize_proxy(parent_proxy),
                "forwarder_instance": forwarder_instance,
                "diagnostics": diag,
            }
            if len(token) < min_len:
                return SolveResult(
                    ok=False,
                    token=token,
                    proxy=upstream_proxy or normalize_proxy(upstream_raw),
                    page_url=page_url,
                    user_agent=effective_ua,
                    elapsed_ms=elapsed_ms,
                    error=(
                        f"在 {timeout}s 内未捕获到可用 Turnstile token "
                        f"(len={len(token)}, min={min_len})"
                    ),
                    extras=extras,
                )
            extras["token_len"] = len(token)
            return SolveResult(
                ok=True,
                token=token,
                proxy=upstream_proxy or normalize_proxy(upstream_raw),
                page_url=page_url,
                user_agent=effective_ua,
                elapsed_ms=elapsed_ms,
                extras=extras,
            )
        except Exception as exc:
            elapsed_ms = int((time.monotonic() - started) * 1000)
            return SolveResult(
                ok=False,
                token="",
                proxy=normalize_proxy(upstream_raw),
                page_url=page_url,
                user_agent=user_agent,
                elapsed_ms=elapsed_ms,
                error=str(exc),
                extras={
                    "parent_proxy": normalize_proxy(parent_proxy),
                    "forwarder_instance": forwarder_instance,
                },
            )
        finally:
            stop_browser_proxy(forwarder_instance)

    def _capture_with_browser(
        self,
        *,
        page_url: str,
        browser_proxy: str,
        timeout: int,
        headless: bool,
        user_agent: str,
        min_len: int,
        diagnose: bool,
    ) -> Tuple[str, str, Dict[str, Any]]:
        """Prefer parent-project enhanced capture, then fall back to local loop."""
        # 1) Parent enhanced capture (click email + prime + shadow nudge + diagnostics)
        try:
            from xai_http_flow import capture_turnstile_token as parent_capture

            logs: List[str] = []

            def _cb(msg: str) -> None:
                logs.append(str(msg))
                _log(self.log_callback, msg)

            token = parent_capture(
                proxy=browser_proxy,
                output="",
                proxy_used_file="",
                selected_proxy_raw=browser_proxy,
                timeout=timeout,
                headless=headless,
                page_url=page_url,
                click_email_signup=True,
                log_callback=_cb,
            )
            token = str(token or "").strip()
            diag = {
                "source": "xai_http_flow.capture_turnstile_token",
                "logs_tail": logs[-12:],
                "token_len": len(token),
            }
            return token, user_agent, diag
        except Exception as parent_exc:
            _log(self.log_callback, f"[Turnstile][warn] parent capture failed, fallback local: {parent_exc}")

        # 2) Local fallback path
        try:
            from DrissionPage import Chromium, ChromiumOptions
        except Exception as exc:  # pragma: no cover
            raise RuntimeError(f"Turnstile capture 需要 DrissionPage/Chrome: {exc}") from exc

        try:
            from xai_http_flow import _build_turnstile_browser_options as build_opts
        except Exception:
            build_opts = None

        if build_opts is not None:
            options = build_opts(
                options=ChromiumOptions(),
                proxy=browser_proxy,
                headless=headless,
                user_agent=user_agent,
                log_callback=self.log_callback,
            )
        else:
            options = ChromiumOptions()
            try:
                options.auto_port()
            except Exception:
                pass
            try:
                options.set_argument("--no-first-run")
                options.set_argument("--no-default-browser-check")
                options.set_argument("--disable-dev-shm-usage")
            except Exception:
                pass
            if headless:
                try:
                    options.headless(True)
                except Exception:
                    pass
            if user_agent:
                try:
                    options.set_user_agent(user_agent)
                except Exception:
                    try:
                        options.set_argument(f"--user-agent={user_agent}")
                    except Exception:
                        pass
            if browser_proxy:
                try:
                    options.set_proxy(browser_proxy)
                    _log(self.log_callback, f"[Turnstile] 浏览器代理: {browser_proxy}")
                except Exception as exc:
                    raise RuntimeError(f"无法设置浏览器代理: {exc}") from exc

        browser = Chromium(options)
        samples: List[Dict[str, Any]] = []
        try:
            tabs = getattr(browser, "get_tabs", lambda: [])()
            page = tabs[-1] if tabs else browser.new_tab()
            page.get(page_url)
            _log(self.log_callback, f"[Turnstile] 已打开注册页: {page_url}")
            clicked = click_email_signup_entry(page, log_callback=self.log_callback, timeout=12)
            if clicked:
                _log(self.log_callback, "[Turnstile] 已进入邮箱注册入口，等待 Turnstile widget…")
            else:
                _log(self.log_callback, "[Turnstile][warn] 未点到邮箱注册入口，继续在当前页等待 token")
            deadline = time.monotonic() + timeout
            started = time.monotonic()
            token = ""
            next_diag_at = 0.0
            while time.monotonic() < deadline:
                now = time.monotonic()
                token = read_turnstile_token_from_page(page)
                if diagnose and now >= next_diag_at:
                    snap = diagnose_page(page)
                    if isinstance(snap, dict):
                        snap["_t"] = int(now - started)
                        samples.append(snap)
                        _log(
                            self.log_callback,
                            "[Turnstile][diag] "
                            f"t={snap.get('_t')}s tokenLen={snap.get('tokenLen')} "
                            f"sitekeys={snap.get('sitekeyCount')} "
                            f"cfInput={snap.get('hasCfInput')} "
                            f"tsIframes={snap.get('turnstileIframeCount')} "
                            f"challenge={snap.get('challengeLike')}",
                        )
                    next_diag_at = now + (5 if len(samples) <= 1 else 10)
                if len(token) >= min_len:
                    break
                try:
                    page.run_js(
                        """
const nodes = Array.from(document.querySelectorAll('.cf-turnstile, [data-sitekey], iframe[src*="turnstile"], iframe[src*="challenges.cloudflare"]'));
for (const n of nodes) {
  try { n.scrollIntoView({block:'center', inline:'center'}); } catch (e) {}
  try { n.click(); } catch (e) {}
}
"""
                    )
                except Exception:
                    pass
                time.sleep(1.0)

            if diagnose:
                snap = diagnose_page(page)
                if isinstance(snap, dict):
                    snap["_t"] = int(time.monotonic() - started)
                    samples.append(snap)

            effective_ua = user_agent
            if not effective_ua:
                try:
                    effective_ua = str(page.run_js("return navigator.userAgent;") or "").strip()
                except Exception:
                    effective_ua = ""
            diag = _summarize_diag(samples)
            diag["source"] = "local_fallback"
            _log(
                self.log_callback,
                f"[Turnstile] 捕获结束 token_len={len(token)} ua={'yes' if effective_ua else 'no'} "
                f"sitekeys={diag.get('sitekey_count')} ts_iframes={diag.get('turnstile_iframe_count_last')}",
            )
            return token, effective_ua, diag
        finally:
            try:
                browser.quit()
            except Exception:
                pass
