# -*- coding: utf-8 -*-
"""Unified Cloudflare Turnstile state machine for register / OAuth / final pages.

States:
  ABSENT -> WAITING -> TOKEN_READY -> STABLE_READY
                  \\-> ERROR / CHECKBOX -> (reset) -> WAITING
"""

from __future__ import annotations

import time
from typing import Any, Callable, Dict, Optional

TURNSTILE_MIN_TOKEN_LEN = 80

SCENE_REGISTER = "register"
SCENE_OAUTH = "oauth"
SCENE_FINAL = "final"

STATE_ABSENT = "ABSENT"
STATE_WAITING = "WAITING"
STATE_TOKEN_READY = "TOKEN_READY"
STATE_STABLE_READY = "STABLE_READY"
STATE_ERROR = "ERROR"
STATE_CHECKBOX = "CHECKBOX"

SCENE_DEFAULTS = {
    SCENE_REGISTER: {
        "use_cache": True,
        "require_stable_sec": 0.5,
        "allow_reset_on_error": True,
        "native_wait_sec": 10.0,
        "retry_gap_sec": 6.0,
    },
    SCENE_OAUTH: {
        "use_cache": False,
        "require_stable_sec": 1.2,
        "allow_reset_on_error": True,
        "native_wait_sec": 12.0,
        "retry_gap_sec": 12.0,
        # OAuth 登录页：外部注入 token 很容易 Failed to verify，优先原生通过
        "require_native_pass": True,
    },
    SCENE_FINAL: {
        "use_cache": True,
        "require_stable_sec": 0.3,
        "allow_reset_on_error": True,
        "native_wait_sec": 8.0,
        "retry_gap_sec": 10.0,
    },
}

_TURNSTILE_SESSION_CACHE = {"token": "", "at": 0.0}

GetTokenFn = Callable[..., str]
LogFn = Optional[Callable[[str], None]]
CancelFn = Optional[Callable[[], bool]]


TURNSTILE_PROBE_JS = r"""
function readToken() {
  const out = {
    inputLen: 0, apiLen: 0, token: '', present: false, successMark: false,
    errorMark: false, checkboxMode: false, bodyHint: '', pageAdvanced: false
  };
  const cfInput = document.querySelector('input[name="cf-turnstile-response"]');
  const inputVal = cfInput ? String(cfInput.value || '').trim() : '';
  out.inputLen = inputVal.length;
  let apiVal = '';
  try {
    if (window.turnstile && typeof turnstile.getResponse === 'function') {
      apiVal = String(turnstile.getResponse() || '').trim();
      if (!apiVal) {
        const widgets = document.querySelectorAll('.cf-turnstile, [data-sitekey]');
        for (const w of widgets) {
          const id = w.getAttribute('data-turnstile-id') || w.id;
          if (id) {
            const one = String(turnstile.getResponse(id) || '').trim();
            if (one) { apiVal = one; break; }
          }
        }
      }
    }
  } catch (e) {}
  out.apiLen = apiVal.length;
  out.token = inputVal.length >= apiVal.length ? inputVal : apiVal;
  out.present = !!cfInput
    || !!document.querySelector('iframe[src*="turnstile"], div.cf-turnstile, [data-sitekey], script[src*="turnstile"]');

  const bodyRaw = (document.body && document.body.innerText) ? document.body.innerText : '';
  const body = bodyRaw.toLowerCase();
  out.bodyHint = bodyRaw.replace(/\s+/g, ' ').trim().slice(0, 160);

  const successDom = !!document.querySelector(
    '[aria-label*="success" i], [aria-label*="成功"], .cf-turnstile-success, #success'
  );
  const successText = /成功\s*[!！]?/.test(bodyRaw)
    || body.includes('success!')
    || !!document.querySelector('[aria-label*="成功"], [title*="成功"], [aria-label*="Success" i]');
  let hostCardSuccess = false;
  try {
    const cards = Array.from(document.querySelectorAll('div, span, p, label'));
    for (const n of cards) {
      const t = String(n.innerText || n.textContent || '').replace(/\s+/g, '');
      if (t === '成功' || t === '成功!' || t === '成功！' || t.toLowerCase() === 'success!') {
        hostCardSuccess = true; break;
      }
    }
  } catch (e) {}
  let iframeSuccess = false;
  try {
    for (const f of Array.from(document.querySelectorAll('iframe[src*="turnstile"], iframe[src*="challenges.cloudflare"]'))) {
      const t = ((f.getAttribute('title') || '') + ' ' + (f.getAttribute('aria-label') || '')).toLowerCase();
      if (t.includes('success') || t.includes('成功')) { iframeSuccess = true; break; }
    }
  } catch (e) {}
  out.successMark = successDom || successText || iframeSuccess || hostCardSuccess;

  out.errorMark = /failed to verify|verify cloudflare|turnstile token|验证失败|人机验证失败|无法验证/.test(body)
    || body.includes('trace id');
  out.checkboxMode = !!document.querySelector(
    'input[type="checkbox"][id*="cf"], input[type="checkbox"][name*="cf"], label[for*="cf-"]'
  ) || /确认您是真人|verify you are human|请完成验证|请完成以下验证/.test(body);

  const signupPwd = document.querySelector('input[autocomplete="new-password"], input[data-testid="password"][name="password"]');
  const signupGiven = document.querySelector('input[data-testid="givenName"], input[name="givenName"]');
  const onConsent = (location.pathname || '').toLowerCase().includes('/oauth2/consent');
  const onSignInOnly = (location.pathname || '').toLowerCase().includes('/sign-in');
  const hasLoginPassword = !!document.querySelector('input[type="password"][name="password"], input[data-testid="password"]');
  out.pageAdvanced = onSignInOnly && !signupPwd && !signupGiven && !onConsent && !hasLoginPassword;
  return out;
}
return readToken();
"""


def scene_defaults(scene: str) -> Dict[str, Any]:
    scene = str(scene or SCENE_REGISTER).strip().lower() or SCENE_REGISTER
    base = dict(SCENE_DEFAULTS.get(scene) or SCENE_DEFAULTS[SCENE_REGISTER])
    base["scene"] = scene
    return base


def remember_turnstile_token(token: str) -> None:
    token = str(token or "").strip()
    if len(token) >= TURNSTILE_MIN_TOKEN_LEN:
        _TURNSTILE_SESSION_CACHE["token"] = token
        _TURNSTILE_SESSION_CACHE["at"] = time.time()


def clear_turnstile_session_cache(reason: str = "", log_callback: LogFn = None) -> None:
    old = str(_TURNSTILE_SESSION_CACHE.get("token") or "")
    _TURNSTILE_SESSION_CACHE["token"] = ""
    _TURNSTILE_SESSION_CACHE["at"] = 0.0
    if log_callback and old:
        msg = f"[Debug] 已清空 Turnstile 缓存(len={len(old)})"
        if reason:
            msg += f" | reason={reason}"
        log_callback(msg)


def clear_cf_for_scene(scene: str, log_callback: LogFn = None) -> None:
    """OAuth 等场景必须清缓存，避免 register token 污染。"""
    scene = str(scene or "").strip().lower()
    if scene == SCENE_OAUTH:
        clear_turnstile_session_cache(reason=f"scene={scene}", log_callback=log_callback)
    elif scene:
        # 其他场景默认不强制清；调用方需要时可直接 clear_turnstile_session_cache
        pass


def get_cached_token(max_age: float = 45.0) -> str:
    token = str(_TURNSTILE_SESSION_CACHE.get("token") or "").strip()
    if len(token) < TURNSTILE_MIN_TOKEN_LEN:
        return ""
    age = time.time() - float(_TURNSTILE_SESSION_CACHE.get("at") or 0)
    if age > max_age:
        return ""
    return token


def probe_turnstile_status(page, use_cache: bool = True, scene: str = SCENE_REGISTER) -> Dict[str, Any]:
    """Probe current Turnstile status. scene only affects default use_cache when caller omits intent."""
    defaults = scene_defaults(scene)
    if use_cache is None:
        use_cache = bool(defaults.get("use_cache", True))

    try:
        data = page.run_js(TURNSTILE_PROBE_JS)
    except Exception:
        data = {}
    if not isinstance(data, dict):
        data = {}

    token = str(data.get("token") or "").strip()
    input_len = int(data.get("inputLen") or 0)
    api_len = int(data.get("apiLen") or 0)
    token_len = max(len(token), input_len, api_len)
    solved = token_len >= TURNSTILE_MIN_TOKEN_LEN
    cache_used = False

    if use_cache and (not solved):
        cached = get_cached_token(45.0)
        if cached:
            token = cached
            token_len = len(cached)
            solved = True
            cache_used = True

    success_mark = bool(data.get("successMark"))
    error_mark = bool(data.get("errorMark"))
    checkbox_mode = bool(data.get("checkboxMode"))

    if success_mark and not error_mark:
        solved = True
    if error_mark:
        # dirty/error: cache must not force solved
        if checkbox_mode:
            solved = False
        else:
            solved = (token_len >= TURNSTILE_MIN_TOKEN_LEN) and (not cache_used)

    return {
        "present": bool(data.get("present")),
        "token": token,
        "token_len": token_len,
        "input_len": input_len,
        "api_len": api_len,
        "solved": solved,
        "success_mark": success_mark,
        "error_mark": error_mark,
        "checkbox_mode": checkbox_mode,
        "page_advanced": bool(data.get("pageAdvanced")),
        "raw": data,
        "cache_used": cache_used,
        "scene": str(scene or SCENE_REGISTER),
        "body_hint": str(data.get("bodyHint") or ""),
    }


def classify_cf_status(
    status: Dict[str, Any],
    *,
    scene: str = SCENE_REGISTER,
    token_good_since: float = 0.0,
    now: Optional[float] = None,
    require_stable_sec: Optional[float] = None,
) -> Dict[str, Any]:
    """Map probe status -> state machine result."""
    defaults = scene_defaults(scene)
    if require_stable_sec is None:
        require_stable_sec = float(defaults["require_stable_sec"])
    now = time.time() if now is None else float(now)
    status = status if isinstance(status, dict) else {}

    present = bool(status.get("present"))
    success = bool(status.get("success_mark"))
    error = bool(status.get("error_mark"))
    checkbox = bool(status.get("checkbox_mode"))
    token_len = max(
        int(status.get("token_len") or 0),
        int(status.get("input_len") or 0),
        int(status.get("api_len") or 0),
    )
    solved = bool(status.get("solved"))
    has_token = token_len >= TURNSTILE_MIN_TOKEN_LEN and solved

    stable_for = 0.0
    if token_good_since and has_token and not error and not checkbox:
        stable_for = max(0.0, now - float(token_good_since))
    token_stable = stable_for >= float(require_stable_sec)

    input_len = int(status.get("input_len") or 0)
    api_len = int(status.get("api_len") or 0)
    from_retry = bool(status.get("from_retry"))
    require_native = bool(defaults.get("require_native_pass"))
    # 原生通过信号：成功文案，或 input+api 都有 token（外部注入通常 api=0）
    native_pass = bool(success) or (input_len >= TURNSTILE_MIN_TOKEN_LEN and api_len >= TURNSTILE_MIN_TOKEN_LEN)

    if checkbox and not success:
        state = STATE_CHECKBOX
        ready = False
    elif error and not success:
        state = STATE_ERROR
        ready = False
    elif not present:
        state = STATE_ABSENT
        ready = True
    elif success:
        state = STATE_STABLE_READY
        ready = True
    elif require_native:
        # OAuth：禁止“仅外部注入 input token”直接 ready
        if native_pass and token_stable and not from_retry:
            state = STATE_STABLE_READY
            ready = True
        elif native_pass and token_stable and from_retry and success:
            state = STATE_STABLE_READY
            ready = True
        elif has_token:
            state = STATE_TOKEN_READY
            ready = False
        else:
            state = STATE_WAITING
            ready = False
    elif has_token and token_stable:
        state = STATE_STABLE_READY
        ready = True
    elif has_token:
        state = STATE_TOKEN_READY
        ready = False
    else:
        state = STATE_WAITING
        ready = False

    return {
        "state": state,
        "ready": ready,
        "present": present,
        "success": success,
        "error": error,
        "checkbox": checkbox,
        "has_token": has_token,
        "native_pass": native_pass,
        "from_retry": from_retry,
        "token_len": token_len,
        "input_len": input_len,
        "api_len": api_len,
        "token_stable": token_stable,
        "stable_for": stable_for,
        "require_stable_sec": float(require_stable_sec),
        "scene": defaults["scene"],
        "status": status,
    }


def is_cf_ready(
    status: Dict[str, Any],
    *,
    scene: str = SCENE_REGISTER,
    token_good_since: float = 0.0,
    now: Optional[float] = None,
    require_stable_sec: Optional[float] = None,
) -> bool:
    return bool(
        classify_cf_status(
            status,
            scene=scene,
            token_good_since=token_good_since,
            now=now,
            require_stable_sec=require_stable_sec,
        ).get("ready")
    )


def log_turnstile_status(prefix: str, status: Dict[str, Any], log_callback: LogFn = None, classified: Optional[Dict[str, Any]] = None) -> None:
    if not log_callback:
        return
    status = status if isinstance(status, dict) else {}
    if classified is None:
        classified = classify_cf_status(status, scene=str(status.get("scene") or SCENE_REGISTER))
    log_callback(
        f"[*] {prefix} | state={classified.get('state')} ready={classified.get('ready')} "
        f"solved={status.get('solved')} success={status.get('success_mark')} "
        f"tokenLen={status.get('token_len')} (input={status.get('input_len')} api={status.get('api_len')}) "
        f"error={status.get('error_mark')} checkbox={status.get('checkbox_mode')} "
        f"present={status.get('present')} cache={status.get('cache_used')} scene={classified.get('scene')}"
    )


def sync_turnstile_token_to_page(page, token: str, log_callback: LogFn = None) -> int:
    token = str(token or "").strip()
    if not token:
        return 0
    synced = page.run_js(
        r"""
const token = String(arguments[0] || '').trim();
const cfInput = document.querySelector('input[name="cf-turnstile-response"]');
if (!cfInput || !token) return 0;
const nativeSetter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')?.set;
if (nativeSetter) nativeSetter.call(cfInput, token);
else cfInput.value = token;
cfInput.dispatchEvent(new Event('input', { bubbles: true }));
cfInput.dispatchEvent(new Event('change', { bubbles: true }));
return String(cfInput.value || '').trim().length;
""",
        token,
    )
    try:
        synced = int(synced or 0)
    except Exception:
        synced = 0
    if log_callback:
        log_callback(f"[Debug] Turnstile 同步到页面 input 长度={synced}")
    return synced


def ensure_cf_token(
    page,
    *,
    scene: str = SCENE_REGISTER,
    get_token_fn: Optional[GetTokenFn] = None,
    reset: bool = False,
    log_callback: LogFn = None,
    cancel_callback: CancelFn = None,
) -> Dict[str, Any]:
    """Acquire/sync a Turnstile token for the current page."""
    defaults = scene_defaults(scene)
    use_cache = bool(defaults.get("use_cache", True))
    if not get_token_fn:
        status = probe_turnstile_status(page, use_cache=use_cache, scene=scene)
        log_turnstile_status("Turnstile ensure(无外部求解器)", status, log_callback)
        return status

    try:
        try:
            token = get_token_fn(
                log_callback=log_callback,
                cancel_callback=cancel_callback,
                skip_reset=not reset,
            )
        except TypeError:
            token = get_token_fn(log_callback=log_callback, cancel_callback=cancel_callback)
    except Exception as exc:
        if log_callback:
            log_callback(f"[Debug] Turnstile ensure 失败: {exc}")
        return probe_turnstile_status(page, use_cache=use_cache, scene=scene)

    token = str(token or "").strip()
    if not token:
        return probe_turnstile_status(page, use_cache=use_cache, scene=scene)

    # OAuth 场景：外部 token 不进全局缓存，避免污染；注册场景可缓存
    if scene != SCENE_OAUTH:
        remember_turnstile_token(token)
    synced = sync_turnstile_token_to_page(page, token, log_callback=log_callback)
    time.sleep(0.6)
    status = probe_turnstile_status(page, use_cache=False if scene == SCENE_OAUTH else use_cache, scene=scene)
    status = dict(status or {})
    status["token"] = status.get("token") or token
    status["token_len"] = max(int(status.get("token_len") or 0), synced, len(token))
    status["input_len"] = max(int(status.get("input_len") or 0), synced)
    status["from_retry"] = True
    # 外部注入只表示 token 写进了 input，不等于页面原生通过
    if status.get("success_mark"):
        status["solved"] = True
    elif scene == SCENE_OAUTH:
        status["solved"] = int(status.get("api_len") or 0) >= TURNSTILE_MIN_TOKEN_LEN and status["token_len"] >= TURNSTILE_MIN_TOKEN_LEN
    else:
        status["solved"] = True if status["token_len"] >= TURNSTILE_MIN_TOKEN_LEN else bool(status.get("solved"))
    if log_callback:
        log_turnstile_status(
            f"Turnstile ensure 完成(reset={bool(reset)})",
            status,
            log_callback,
        )
    return status


def retry_turnstile_and_sync(
    page,
    log_callback: LogFn = None,
    cancel_callback: CancelFn = None,
    get_token_fn: Optional[GetTokenFn] = None,
    scene: str = SCENE_REGISTER,
    reset: bool = False,
) -> Dict[str, Any]:
    """Backward-compatible alias used by register flow."""
    return ensure_cf_token(
        page,
        scene=scene,
        get_token_fn=get_token_fn,
        reset=reset,
        log_callback=log_callback,
        cancel_callback=cancel_callback,
    )


def update_token_stability(bag: Dict[str, Any], status: Dict[str, Any], now: Optional[float] = None) -> float:
    """Track continuous good-token window on a mutable state bag. Returns token_good_since."""
    now = time.time() if now is None else float(now)
    bag = bag if isinstance(bag, dict) else {}
    status = status if isinstance(status, dict) else {}
    token_len = max(
        int(status.get("token_len") or 0),
        int(status.get("input_len") or 0),
        int(status.get("api_len") or 0),
    )
    good = (
        token_len >= TURNSTILE_MIN_TOKEN_LEN
        and bool(status.get("solved"))
        and not status.get("error_mark")
        and not status.get("checkbox_mode")
    )
    if good:
        if not bag.get("token_good_since"):
            bag["token_good_since"] = now
    else:
        bag["token_good_since"] = 0.0
    return float(bag.get("token_good_since") or 0.0)


def wait_cf_ready(
    page,
    *,
    scene: str = SCENE_REGISTER,
    timeout: float = 45.0,
    get_token_fn: Optional[GetTokenFn] = None,
    log_callback: LogFn = None,
    cancel_callback: CancelFn = None,
    state_bag: Optional[Dict[str, Any]] = None,
    sleep_fn: Optional[Callable[[float], None]] = None,
) -> Dict[str, Any]:
    """Wait until CF is ready for the scene, with native wait then external ensure."""
    defaults = scene_defaults(scene)
    use_cache = bool(defaults["use_cache"])
    native_wait = float(defaults["native_wait_sec"])
    retry_gap = float(defaults["retry_gap_sec"])
    allow_reset = bool(defaults["allow_reset_on_error"])
    bag = state_bag if isinstance(state_bag, dict) else {}
    sleep_fn = sleep_fn or (lambda s: time.sleep(s))
    deadline = time.time() + max(1.0, float(timeout or 1.0))
    started = time.time()
    last_log = 0.0
    last_ensure = float(bag.get("last_turnstile_at") or 0.0)

    while time.time() < deadline:
        if cancel_callback and cancel_callback():
            raise RuntimeError("turnstile wait cancelled")

        now = time.time()
        status = probe_turnstile_status(page, use_cache=use_cache, scene=scene)
        token_good_since = update_token_stability(bag, status, now=now)
        classified = classify_cf_status(
            status,
            scene=scene,
            token_good_since=token_good_since,
            now=now,
        )

        if now - last_log >= 3.0:
            log_turnstile_status(f"Turnstile 等待({scene})", status, log_callback, classified)
            last_log = now

        if classified.get("ready"):
            bag["cf_ready_at"] = now
            bag["cf_last_state"] = classified.get("state")
            return {
                **classified,
                "status": status,
                "waited": now - started,
            }

        need_reset = bool(
            classified.get("state") in (STATE_ERROR, STATE_CHECKBOX)
            or bag.get("need_cf_reset")
        )
        waited = now - started
        can_ensure = bool(get_token_fn) and (waited >= native_wait or need_reset)
        if can_ensure and (now - last_ensure >= retry_gap):
            if log_callback:
                log_callback(
                    f"[*] Turnstile 未就绪({waited:.0f}s, state={classified.get('state')})，外部求解 | scene={scene} reset={need_reset and allow_reset}"
                )
            status = ensure_cf_token(
                page,
                scene=scene,
                get_token_fn=get_token_fn,
                reset=bool(need_reset and allow_reset),
                log_callback=log_callback,
                cancel_callback=cancel_callback,
            )
            bag["last_turnstile_at"] = now
            bag["need_cf_reset"] = False
            last_ensure = now
            token_good_since = update_token_stability(bag, status, now=time.time())
            classified = classify_cf_status(
                status,
                scene=scene,
                token_good_since=token_good_since,
            )
            if classified.get("ready"):
                bag["cf_ready_at"] = time.time()
                return {
                    **classified,
                    "status": status,
                    "waited": time.time() - started,
                }

        sleep_fn(0.5)

    # timeout: return last classification
    status = probe_turnstile_status(page, use_cache=use_cache, scene=scene)
    token_good_since = update_token_stability(bag, status)
    classified = classify_cf_status(status, scene=scene, token_good_since=token_good_since)
    if log_callback:
        log_turnstile_status(f"Turnstile 等待超时({scene})", status, log_callback, classified)
    return {
        **classified,
        "status": status,
        "waited": time.time() - started,
        "timeout": True,
    }


# Compatibility aliases
probe_cf = probe_turnstile_status
sync_cf_token = sync_turnstile_token_to_page
