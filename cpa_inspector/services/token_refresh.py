from __future__ import annotations

import base64
import json
import time
from datetime import datetime, timezone
from typing import Any
from urllib import error as urlerror
from urllib import parse as urlparse
from urllib import request as urlrequest

# 与 CLIProxyAPI / xAI Grok CLI 一致的公共 client_id
DEFAULT_XAI_CLIENT_ID = "b1a00492-073a-47ea-816f-4c329264a828"
DEFAULT_XAI_TOKEN_ENDPOINT = "https://auth.x.ai/oauth2/token"


class TokenRefreshError(Exception):
    """OAuth refresh_token 刷新失败。"""


def _b64url_decode(segment: str) -> bytes:
    padding = "=" * (-len(segment) % 4)
    return base64.urlsafe_b64decode(segment + padding)


def decode_jwt_payload(token: str) -> dict[str, Any]:
    text = str(token or "").strip()
    parts = text.split(".")
    if len(parts) < 2:
        return {}
    try:
        raw = _b64url_decode(parts[1])
        payload = json.loads(raw.decode("utf-8"))
    except Exception:  # noqa: BLE001 - JWT 解析失败视为空
        return {}
    return payload if isinstance(payload, dict) else {}


def utc_iso_z(ts: float | None = None) -> str:
    if ts is None:
        ts = time.time()
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def resolve_token_endpoint(payload: dict[str, Any]) -> str:
    endpoint = str(payload.get("token_endpoint") or "").strip()
    if endpoint:
        return endpoint
    provider = str(payload.get("type") or "").strip().lower()
    if provider == "xai":
        return DEFAULT_XAI_TOKEN_ENDPOINT
    return ""


def resolve_client_id(payload: dict[str, Any]) -> str:
    for key in ("client_id", "clientId"):
        value = str(payload.get(key) or "").strip()
        if value:
            return value
    access = str(payload.get("access_token") or "").strip()
    access_payload = decode_jwt_payload(access)
    for key in ("client_id", "azp", "aud"):
        value = access_payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, list):
            for item in value:
                text = str(item or "").strip()
                if text:
                    return text
    provider = str(payload.get("type") or "").strip().lower()
    if provider == "xai":
        return DEFAULT_XAI_CLIENT_ID
    return ""


def refresh_oauth_tokens(
    refresh_token: str,
    *,
    token_endpoint: str,
    client_id: str,
    timeout_seconds: int = 20,
) -> dict[str, Any]:
    """调用 OAuth2 refresh_token grant，返回 token 端点 JSON。"""
    refresh_token = str(refresh_token or "").strip()
    token_endpoint = str(token_endpoint or "").strip()
    client_id = str(client_id or "").strip()
    if not refresh_token:
        raise TokenRefreshError("缺少 refresh_token")
    if not token_endpoint:
        raise TokenRefreshError("缺少 token_endpoint")
    if not client_id:
        raise TokenRefreshError("缺少 client_id")

    form = urlparse.urlencode(
        {
            "grant_type": "refresh_token",
            "client_id": client_id,
            "refresh_token": refresh_token,
        }
    ).encode("utf-8")
    req = urlrequest.Request(
        token_endpoint,
        data=form,
        method="POST",
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        },
    )
    try:
        with urlrequest.urlopen(req, timeout=max(1, int(timeout_seconds))) as resp:
            body = resp.read()
            status = getattr(resp, "status", 200)
    except urlerror.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            detail = str(exc.reason or "")
        detail = " ".join(detail.split())
        if len(detail) > 180:
            detail = detail[:179] + "…"
        raise TokenRefreshError(f"HTTP {exc.code}: {detail or '刷新失败'}") from exc
    except Exception as exc:  # noqa: BLE001 - 网络/超时统一包装
        raise TokenRefreshError(str(exc) or type(exc).__name__) from exc

    if int(status or 0) >= 400:
        raise TokenRefreshError(f"HTTP {status}")
    try:
        data = json.loads(body.decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        raise TokenRefreshError("token 响应不是合法 JSON") from exc
    if not isinstance(data, dict):
        raise TokenRefreshError("token 响应顶层必须是对象")
    access = str(data.get("access_token") or "").strip()
    if not access:
        raise TokenRefreshError("响应缺少 access_token")
    return data


def apply_refreshed_tokens(payload: dict[str, Any], token_response: dict[str, Any]) -> dict[str, Any]:
    """把 refresh 结果合并进凭证 JSON，返回新 dict。"""
    doc = dict(payload)
    access = str(token_response.get("access_token") or "").strip()
    if access:
        doc["access_token"] = access
    new_refresh = str(token_response.get("refresh_token") or "").strip()
    if new_refresh:
        doc["refresh_token"] = new_refresh
    id_token = str(token_response.get("id_token") or "").strip()
    if id_token:
        doc["id_token"] = id_token
    token_type = str(token_response.get("token_type") or "").strip()
    if token_type:
        doc["token_type"] = token_type

    expires_in_raw = token_response.get("expires_in")
    expires_in: int | None = None
    try:
        if expires_in_raw is not None and str(expires_in_raw).strip() != "":
            expires_in = int(expires_in_raw)
    except (TypeError, ValueError):
        expires_in = None

    access_payload = decode_jwt_payload(access)
    if "exp" in access_payload:
        try:
            exp_ts = float(access_payload["exp"])
            doc["expired"] = utc_iso_z(exp_ts)
            left = int(exp_ts - time.time())
            if left > 0:
                doc["expires_in"] = left
            elif expires_in is not None and expires_in > 0:
                doc["expires_in"] = expires_in
        except (TypeError, ValueError):
            if expires_in is not None and expires_in > 0:
                doc["expires_in"] = expires_in
                doc["expired"] = utc_iso_z(time.time() + expires_in)
    elif expires_in is not None and expires_in > 0:
        doc["expires_in"] = expires_in
        doc["expired"] = utc_iso_z(time.time() + expires_in)

    id_payload = decode_jwt_payload(id_token) if id_token else {}
    email = (
        str(doc.get("email") or "").strip()
        or str(id_payload.get("email") or "").strip()
        or str(access_payload.get("email") or "").strip()
    )
    if email:
        doc["email"] = email
    sub = (
        str(doc.get("sub") or "").strip()
        or str(access_payload.get("sub") or access_payload.get("principal_id") or "").strip()
        or str(id_payload.get("sub") or "").strip()
    )
    if sub:
        doc["sub"] = sub

    doc["last_refresh"] = utc_iso_z()
    if not str(doc.get("token_endpoint") or "").strip():
        endpoint = resolve_token_endpoint(doc)
        if endpoint:
            doc["token_endpoint"] = endpoint
    return doc


def refresh_credential_payload(
    payload: dict[str, Any],
    *,
    timeout_seconds: int = 20,
) -> tuple[dict[str, Any], str]:
    """刷新凭证 payload。

    返回 (新 payload, 说明)。
    无 refresh_token 时原样返回。
    """
    if not isinstance(payload, dict):
        raise TokenRefreshError("凭证不是对象")
    refresh_token = str(payload.get("refresh_token") or "").strip()
    if not refresh_token:
        return dict(payload), "无 refresh_token，跳过刷新"

    endpoint = resolve_token_endpoint(payload)
    client_id = resolve_client_id(payload)
    token_response = refresh_oauth_tokens(
        refresh_token,
        token_endpoint=endpoint,
        client_id=client_id,
        timeout_seconds=timeout_seconds,
    )
    updated = apply_refreshed_tokens(payload, token_response)
    return updated, "已刷新 token"
