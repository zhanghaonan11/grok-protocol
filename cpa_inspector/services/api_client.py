from __future__ import annotations

from typing import Any
from urllib.parse import urljoin

import requests

from cpa_inspector.constants import (
    API_CALL_ENDPOINT,
    AUTH_FILES_DOWNLOAD_ENDPOINT,
    AUTH_FILES_ENDPOINT,
    CHAT_COMPLETIONS_ENDPOINT,
    REQUEST_TIMEOUT_SECONDS,
)
from cpa_inspector.models import CredentialRecord


class ApiError(RuntimeError):
    pass


class ManagementApiClient:
    def __init__(self, base_url: str, secret_key: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.secret_key = secret_key.strip()
        self.session = requests.Session()

    def _build_url(self, path: str) -> str:
        return urljoin(f"{self.base_url}/", path.lstrip("/"))

    def _headers(self, content_type: str | None = None) -> dict[str, str]:
        headers: dict[str, str] = {}
        if self.secret_key:
            headers["Authorization"] = f"Bearer {self.secret_key}"
        if content_type:
            headers["Content-Type"] = content_type
        return headers

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        raw_body: bytes | None = None,
        expect_json: bool = True,
        timeout: int | None = None,
    ) -> Any:
        try:
            response = self.session.request(
                method=method,
                url=self._build_url(path),
                params=params,
                json=json_body,
                data=raw_body,
                headers=self._headers(
                    "application/json" if (json_body is not None or raw_body is not None) else None
                ),
                timeout=REQUEST_TIMEOUT_SECONDS if timeout is None else timeout,
            )
        except requests.RequestException as exc:
            raise ApiError(f"请求失败：{exc}") from exc

        if response.status_code >= 400:
            message = response.text.strip()
            try:
                payload = response.json()
            except ValueError:
                payload = None
            if isinstance(payload, dict):
                error = payload.get("error")
                if isinstance(error, str) and error.strip():
                    message = error.strip()
                else:
                    alt = payload.get("message")
                    if isinstance(alt, str) and alt.strip():
                        message = alt.strip()
            if response.status_code in (401, 403):
                raise ApiError(f"鉴权失败：{message or '管理密钥错误或远程管理未开启'}")
            raise ApiError(f"接口错误 HTTP {response.status_code}：{message}")

        if not expect_json:
            return response.content

        try:
            return response.json()
        except ValueError as exc:
            raise ApiError("接口返回的不是合法 JSON") from exc

    def test_connection(self) -> list[CredentialRecord]:
        return self.fetch_credentials()

    def fetch_credentials(self) -> list[CredentialRecord]:
        payload = self._request("GET", AUTH_FILES_ENDPOINT)
        files = payload.get("files", []) if isinstance(payload, dict) else []
        items = [
            CredentialRecord.from_api_payload(item)
            for item in files
            if isinstance(item, dict)
        ]
        return sorted(items, key=lambda item: item.name.lower())

    def upload_credential(self, name: str, raw_content: bytes) -> None:
        self._request(
            "POST",
            AUTH_FILES_ENDPOINT,
            params={"name": name},
            raw_body=raw_content,
        )

    def download_credential(self, name: str) -> bytes:
        return self._request(
            "GET",
            AUTH_FILES_DOWNLOAD_ENDPOINT,
            params={"name": name},
            expect_json=False,
        )

    def delete_credential(self, name: str) -> None:
        self._request("DELETE", AUTH_FILES_ENDPOINT, params={"name": name})

    def probe_chat_completion(
        self,
        model: str,
        timeout_seconds: int,
    ) -> requests.Response:
        body = {
            "model": model,
            "messages": [{"role": "user", "content": "ping"}],
            "max_tokens": 1,
        }
        try:
            response = self.session.request(
                method="POST",
                url=self._build_url(CHAT_COMPLETIONS_ENDPOINT),
                json=body,
                headers=self._headers("application/json"),
                timeout=max(1, int(timeout_seconds)),
            )
        except requests.RequestException as exc:
            # Do not leak secrets/tokens in exception text.
            raise ApiError(f"探测请求失败：{type(exc).__name__}") from exc
        return response

    def management_api_call(
        self,
        *,
        method: str,
        url: str,
        headers: dict[str, str] | None = None,
        data: str = "",
        auth_index: str = "",
        timeout_seconds: int = 30,
    ) -> dict[str, Any]:
        """调用 CPA management api-call，用指定 auth_index 的凭证请求上游。

        返回:
          {
            "status_code": int,   # 上游 HTTP 状态
            "header": dict,
            "body": str,
          }
        """
        payload: dict[str, Any] = {
            "method": str(method or "GET").upper(),
            "url": str(url or "").strip(),
            "header": dict(headers or {}),
            "data": data or "",
        }
        auth_index = str(auth_index or "").strip()
        if auth_index:
            payload["auth_index"] = auth_index
        try:
            response = self.session.request(
                method="POST",
                url=self._build_url(API_CALL_ENDPOINT),
                json=payload,
                headers=self._headers("application/json"),
                timeout=max(1, int(timeout_seconds)),
            )
        except requests.RequestException as exc:
            raise ApiError(f"api-call 请求失败：{type(exc).__name__}") from exc

        if response.status_code >= 400:
            message = response.text.strip()
            try:
                err_payload = response.json()
            except ValueError:
                err_payload = None
            if isinstance(err_payload, dict):
                error = err_payload.get("error") or err_payload.get("message")
                if isinstance(error, str) and error.strip():
                    message = error.strip()
            if response.status_code in (401, 403):
                raise ApiError(f"鉴权失败：{message or '管理密钥错误或远程管理未开启'}")
            raise ApiError(f"api-call 接口错误 HTTP {response.status_code}：{message}")

        try:
            data_payload = response.json()
        except ValueError as exc:
            raise ApiError("api-call 返回不是合法 JSON") from exc
        if not isinstance(data_payload, dict):
            raise ApiError("api-call 返回顶层必须是对象")
        return data_payload
