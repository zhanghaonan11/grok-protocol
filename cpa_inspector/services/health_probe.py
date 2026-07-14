from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import datetime, timezone
import json
from typing import Any

from cpa_inspector.models import CredentialRecord, JobResult
from cpa_inspector.services.api_client import ApiError, ManagementApiClient
from cpa_inspector.services.parallel_jobs import DEFAULT_MAX_WORKERS, run_ordered_parallel
from cpa_inspector.services.token_refresh import TokenRefreshError, refresh_credential_payload

ProgressCallback = Callable[[int, int, str], None]

# 真·凭证失效（鉴权上下文/token 吊销）
INVALID_CREDENTIAL_MARKERS = (
    "invalid or expired credentials",
    "x_xai_token_auth=none",
    "no auth context",
    "unauthenticated:bad-credentials",
    "bad-credentials",
    "could not be validated",
    "token_invalidated",
    "token_revoked",
    "refresh token has been revoked",
    "invalid_grant",
    "your authentication token has been invalidated",
    "auth token not found",
    "auth token refresh failed",
)

# 凭证可能仍有效，但额度/订阅/限流导致不可用
QUOTA_OR_LIMIT_MARKERS = (
    "spending-limit",
    "personal-team-blocked",
    "run out of credits",
    "need a grok subscription",
    "add credits",
    "quota",
    "rate limit",
    "rate_limit",
    "usage_limit",
    "insufficient_quota",
    "resource_exhausted",
    "too many requests",
)

DEFAULT_PROBE_BY_PROVIDER = {
    "xai": {
        "url": "https://api.x.ai/v1/models",
        "method": "GET",
        "headers": {
            "Authorization": "Bearer $TOKEN$",
            "Accept": "application/json",
            "X-XAI-Token-Auth": "xai-grok-cli",
            "User-Agent": "grok-cli/0.1",
        },
        "data": "",
    },
    "codex": {
        "url": "https://chatgpt.com/backend-api/codex/responses/compact",
        "method": "POST",
        "headers": {
            "Authorization": "Bearer $TOKEN$",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Version": "0.101.0",
            "User-Agent": "codex_cli_rs/0.101.0",
            "Originator": "codex_cli_rs",
        },
        "data": (
            '{"model":"gpt-5-codex","instructions":"",'
            '"input":[{"type":"message","role":"user",'
            '"content":[{"type":"input_text","text":"ping"}]}]}'
        ),
    },
}


def _short_detail(text: str, *, limit: int = 220) -> str:
    cleaned = " ".join(str(text or "").split())
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: limit - 1] + "…"


def _normalize_headers(raw: Any) -> dict[str, str]:
    if not isinstance(raw, dict):
        return {}
    out: dict[str, str] = {}
    for key, value in raw.items():
        if isinstance(value, list) and value:
            out[str(key)] = str(value[0])
        else:
            out[str(key)] = str(value)
    return out


def _contains_any(text: str, markers: tuple[str, ...]) -> bool:
    lowered = text.casefold()
    return any(marker in lowered for marker in markers)


def classify_probe_response(
    status_code: int,
    body_text: str = "",
    error: Exception | None = None,
) -> tuple[str, str]:
    """把探测响应归类为 healthy / failed / uncertain。

    - healthy: 上游可用
    - failed: 凭证无效（坏 token / refresh 吊销 / no auth context）
    - uncertain: 限流/无额度/订阅限制/服务端异常（凭证未必废）
    """
    if error is not None:
        detail = _short_detail(str(error) or type(error).__name__)
        if _contains_any(detail, INVALID_CREDENTIAL_MARKERS):
            return "failed", f"凭证无效：{detail}"
        if _contains_any(detail, QUOTA_OR_LIMIT_MARKERS):
            return "uncertain", f"无额度/限流：{detail}"
        return "uncertain", detail or "探测异常"

    code = int(status_code or 0)
    body = str(body_text or "")
    compact = _short_detail(body)

    if 200 <= code < 300:
        return "healthy", f"HTTP {code}"

    # 先识别额度/订阅限制，避免 402/403 spending-limit 被误判成废号
    if code in (402, 429) or _contains_any(body, QUOTA_OR_LIMIT_MARKERS):
        reason = compact or f"HTTP {code}"
        return "uncertain", f"凭证有效但无额度/受限：{reason}"

    if _contains_any(body, INVALID_CREDENTIAL_MARKERS):
        reason = compact or f"HTTP {code}"
        if reason.casefold().startswith("额度获取失败"):
            return "failed", reason
        return "failed", f"凭证无效：{reason}"

    if code in (401, 403):
        reason = compact or f"HTTP {code}"
        # 未知 401/403：偏凭证问题，但仍给原文便于核对
        return "failed", f"凭证无效：{reason}"

    if code >= 500 or code == 0:
        reason = compact or (f"HTTP {code}" if code else "无响应")
        return "uncertain", reason

    reason = compact or f"HTTP {code}"
    return "uncertain", reason


def _job_result_label(health_status: str) -> str:
    if health_status == "healthy":
        return "成功"
    if health_status == "failed":
        return "失败"
    return "不确定"


def _worker_client(client: Any) -> Any:
    base_url = getattr(client, "base_url", None)
    secret_key = getattr(client, "secret_key", None)
    if isinstance(base_url, str) and isinstance(secret_key, str):
        return ManagementApiClient(base_url, secret_key)
    return client


def _probe_spec_for(item: CredentialRecord, fallback_model: str) -> dict[str, str]:
    provider = str(item.provider or "").strip().lower()
    if provider in DEFAULT_PROBE_BY_PROVIDER:
        return dict(DEFAULT_PROBE_BY_PROVIDER[provider])

    model = str(fallback_model or "gpt-5").strip() or "gpt-5"
    return {
        "url": "https://api.openai.com/v1/chat/completions",
        "method": "POST",
        "headers": {
            "Authorization": "Bearer $TOKEN$",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        "data": (
            f'{{"model":"{model}","messages":[{{"role":"user","content":"ping"}}],'
            f'"max_tokens":1}}'
        ),
    }


def _maybe_refresh_before_probe(
    request_client: Any,
    item: CredentialRecord,
    *,
    refresh_timeout_seconds: int,
) -> str:
    """探测前尽量刷新 token，并回写 CPA。

    返回刷新说明；refresh 明确吊销时抛 TokenRefreshError。
    """
    if not hasattr(request_client, "download_credential"):
        return "跳过刷新（客户端不支持下载）"
    if not str(item.name or "").strip():
        return "跳过刷新（无文件名）"

    try:
        raw = request_client.download_credential(item.name)
        payload = json.loads(raw.decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        return f"下载凭证失败，跳过刷新：{_short_detail(str(exc), limit=80)}"

    if not isinstance(payload, dict):
        return "凭证不是 JSON 对象，跳过刷新"
    if not str(payload.get("refresh_token") or "").strip():
        return "无 refresh_token"

    try:
        updated, note = refresh_credential_payload(
            payload,
            timeout_seconds=refresh_timeout_seconds,
        )
    except TokenRefreshError:
        # 交给上层归类为凭证无效
        raise

    if note.startswith("无 refresh_token"):
        return note
    if not hasattr(request_client, "upload_credential"):
        return f"{note}（未回写）"
    try:
        content = json.dumps(updated, ensure_ascii=False, indent=2).encode("utf-8")
        request_client.upload_credential(item.name, content)
        return note
    except Exception as exc:  # noqa: BLE001
        return f"{note}，回写失败：{_short_detail(str(exc), limit=60)}"


def _probe_one(
    client: Any,
    item: CredentialRecord,
    *,
    model: str,
    timeout_seconds: int,
    refresh_before_probe: bool,
    refresh_timeout_seconds: int,
) -> JobResult:
    checked_at = datetime.now(timezone.utc)
    auth_index = str(item.auth_index or "").strip()
    refresh_note = ""
    try:
        if not auth_index:
            raise ApiError("缺少 auth_index，无法按凭证探测")

        request_client = _worker_client(client)

        if refresh_before_probe:
            try:
                refresh_note = _maybe_refresh_before_probe(
                    request_client,
                    item,
                    refresh_timeout_seconds=refresh_timeout_seconds,
                )
            except TokenRefreshError as exc:
                detail = f"凭证无效：刷新失败：{_short_detail(str(exc))}"
                item.health_status = "failed"
                item.health_detail = detail
                item.health_checked_at = checked_at
                return JobResult(
                    name=item.name or item.local_key,
                    result="失败",
                    detail=detail,
                )

        if not hasattr(request_client, "management_api_call"):
            response = request_client.probe_chat_completion(
                model=model,
                timeout_seconds=timeout_seconds,
            )
            status_code = int(getattr(response, "status_code", 0) or 0)
            body_text = str(getattr(response, "text", "") or "")
            health_status, detail = classify_probe_response(status_code, body_text=body_text)
        else:
            spec = _probe_spec_for(item, model)
            data = request_client.management_api_call(
                method=spec["method"],
                url=spec["url"],
                headers=spec["headers"],
                data=spec.get("data") or "",
                auth_index=auth_index,
                timeout_seconds=timeout_seconds,
            )
            status_code = int(data.get("status_code") or 0)
            body_text = str(data.get("body") or "")
            headers = _normalize_headers(data.get("header"))
            health_status, detail = classify_probe_response(status_code, body_text=body_text)
            plan = headers.get("x-codex-plan-type") or headers.get("X-Codex-Plan-Type")
            if health_status == "healthy" and plan:
                detail = f"{detail}；plan={plan}"

        if refresh_note:
            detail = f"{detail}；{refresh_note}"
    except Exception as exc:  # noqa: BLE001
        text = str(exc)
        lowered = text.casefold()
        if (
            "auth token" in lowered
            or "auth_index" in lowered
            or "鉴权失败" in text
            or "无法按凭证探测" in text
            or _contains_any(text, INVALID_CREDENTIAL_MARKERS)
        ):
            health_status, detail = classify_probe_response(401, body_text=text)
        else:
            health_status, detail = classify_probe_response(0, error=exc)
        if refresh_note:
            detail = f"{detail}；{refresh_note}"

    item.health_status = health_status
    item.health_detail = detail
    item.health_checked_at = checked_at
    return JobResult(
        name=item.name or item.local_key,
        result=_job_result_label(health_status),
        detail=detail,
    )


def probe_credentials(
    client: Any,
    credentials: Sequence[CredentialRecord],
    *,
    model: str,
    timeout_seconds: int,
    max_workers: int = DEFAULT_MAX_WORKERS,
    progress_callback: ProgressCallback | None = None,
    refresh_before_probe: bool = True,
    refresh_timeout_seconds: int = 20,
) -> list[JobResult]:
    items = list(credentials)
    if not items:
        return []

    def worker(item: CredentialRecord) -> JobResult:
        return _probe_one(
            client,
            item,
            model=model,
            timeout_seconds=timeout_seconds,
            refresh_before_probe=refresh_before_probe,
            refresh_timeout_seconds=refresh_timeout_seconds,
        )

    def on_item_done(current: int, total: int, item: CredentialRecord) -> None:
        if progress_callback:
            label = item.name or item.local_key or "-"
            progress_callback(current, total, f"正在探测 {current}/{total}：{label}")

    return run_ordered_parallel(
        items,
        worker,
        max_workers=max_workers,
        on_item_done=on_item_done,
    )
