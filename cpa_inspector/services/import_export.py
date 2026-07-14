from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
import json
import re
import zipfile

from cpa_inspector.models import (
    CredentialRecord,
    ImportPreviewItem,
    JobResult,
    mask_email,
    parse_datetime,
)
from cpa_inspector.services.api_client import ManagementApiClient
from cpa_inspector.services.parallel_jobs import DEFAULT_MAX_WORKERS, run_ordered_parallel
from cpa_inspector.services.token_refresh import TokenRefreshError, refresh_credential_payload

ACTION_IMPORT = "import"
ACTION_SKIP = "skip"
ACTION_OVERWRITE = "overwrite"
ACTION_RENAME = "rename"

REQUIRED_FIELDS_BY_PROVIDER = {
    "codex": ("access_token", "refresh_token", "id_token", "email", "expired"),
    "xai": ("access_token", "refresh_token", "email", "expired"),
}

# 粘贴导入常见分隔：JSON____SSO / JSON----SSO
_PASTE_TAIL_SEP = re.compile(r"_{2,}|-{4,}")

ProgressCallback = Callable[[int, int, str], None]


def _sanitize_filename_part(value: str, *, max_len: int = 80) -> str:
    text = str(value or "").strip()
    text = re.sub(r"[\\/:*?\"<>|\s]+", "_", text)
    text = re.sub(r"_+", "_", text).strip("._")
    if not text:
        return "unknown"
    return text[:max_len]


def suggest_credential_filename(payload: dict, reserved_names: set[str] | None = None) -> str:
    """根据凭证内容生成可上传文件名，如 xai-user@mail.com.json。"""
    reserved = set(reserved_names or set())
    provider = str(payload.get("type") or "cred").strip().lower() or "cred"
    email = str(payload.get("email") or "").strip()
    account_id = str(payload.get("account_id") or payload.get("sub") or "").strip()
    if email:
        stem = f"{provider}-{_sanitize_filename_part(email)}"
    elif account_id:
        stem = f"{provider}-{_sanitize_filename_part(account_id)}"
    else:
        stem = f"{provider}-import"
    candidate = f"{stem}.json"
    if candidate not in reserved:
        return candidate
    index = 1
    while True:
        candidate = f"{stem} ({index}).json"
        if candidate not in reserved:
            return candidate
        index += 1


def _looks_like_credential_payload(payload: dict) -> bool:
    if not isinstance(payload, dict):
        return False
    provider = str(payload.get("type") or "").strip().lower()
    if provider:
        return True
    if payload.get("access_token") or payload.get("refresh_token") or payload.get("key"):
        return True
    return False


def _normalize_pasted_payload(payload: dict) -> dict:
    """规范化粘贴进来的 JSON，补全 type 等导入所需字段。"""
    doc = dict(payload)
    provider = str(doc.get("type") or "").strip().lower()
    base_url = str(doc.get("base_url") or "").strip().lower()
    if not provider:
        if "x.ai" in base_url or str(doc.get("token_endpoint") or "").lower().find("x.ai") >= 0:
            provider = "xai"
        else:
            provider = "unknown"
        doc["type"] = provider
    else:
        doc["type"] = provider

    # 兼容部分导出把 access 写成 key
    if not str(doc.get("access_token") or "").strip() and str(doc.get("key") or "").strip():
        doc["access_token"] = str(doc.get("key")).strip()

    if provider == "xai":
        if "auth_kind" not in doc:
            doc["auth_kind"] = "oauth"
        if "token_type" not in doc:
            doc["token_type"] = "Bearer"
        if "base_url" not in doc or not str(doc.get("base_url") or "").strip():
            doc["base_url"] = "https://api.x.ai/v1"
        if "disabled" not in doc:
            doc["disabled"] = False
    return doc


def parse_paste_import_text(text: str) -> list[tuple[str, bytes]]:
    """解析粘贴文本为 (filename, raw_json_bytes) 列表。

    支持：
    - 纯 JSON 对象（可多行）
    - `XAI JSON____SSO` / `JSON----SSO`：只取 JSON，忽略尾部 SSO
    - 同一段文本内连续多个 JSON 对象
    """
    raw = str(text or "").strip().lstrip("﻿")
    if not raw:
        return []

    decoder = json.JSONDecoder()
    items: list[tuple[str, bytes]] = []
    reserved: set[str] = set()
    i = 0
    n = len(raw)

    while i < n:
        while i < n and raw[i].isspace():
            i += 1
        if i >= n:
            break

        # 跳过非 JSON 前缀（例如序号、标签）
        if raw[i] != "{":
            next_brace = raw.find("{", i)
            if next_brace < 0:
                break
            i = next_brace
            continue

        try:
            payload, end = decoder.raw_decode(raw, i)
        except json.JSONDecodeError:
            i += 1
            continue

        if isinstance(payload, dict) and _looks_like_credential_payload(payload):
            normalized = _normalize_pasted_payload(payload)
            filename = suggest_credential_filename(normalized, reserved)
            reserved.add(filename)
            content = json.dumps(normalized, ensure_ascii=False, indent=2).encode("utf-8")
            items.append((filename, content))

        i = end
        # 吃掉紧随其后的 ____SSO / ----SSO 尾巴，直到空白或下一个 JSON
        while i < n and raw[i].isspace():
            i += 1
        if i < n and raw[i] in "_-":
            # 分隔符 + 一段非空白内容
            m = _PASTE_TAIL_SEP.match(raw, i)
            if m:
                i = m.end()
                while i < n and not raw[i].isspace() and raw[i] != "{":
                    i += 1

    return items


# 本机路径导入：限制单文件体积与目录扫描数量，避免误操作拖垮进程。
MAX_LOCAL_IMPORT_FILE_BYTES = 2 * 1024 * 1024
MAX_LOCAL_IMPORT_FILES = 500


def _read_local_json_file(path: Path) -> tuple[str, bytes]:
    if not path.is_file():
        raise ValueError(f"不是文件：{path}")
    if path.suffix.lower() != ".json":
        raise ValueError(f"仅支持 .json：{path.name}")
    size = path.stat().st_size
    if size > MAX_LOCAL_IMPORT_FILE_BYTES:
        raise ValueError(f"文件过大（>{MAX_LOCAL_IMPORT_FILE_BYTES} 字节）：{path.name}")
    return path.name, path.read_bytes()


def collect_local_import_items(path_text: str) -> list[tuple[str, bytes]]:
    """从本机路径收集待导入 JSON。

    支持：
    - 单个 .json 文件
    - 目录（递归收集 *.json）
    """
    raw = str(path_text or "").strip().strip('"').strip("'")
    if not raw:
        raise ValueError("路径为空")
    path = Path(raw).expanduser()
    try:
        path = path.resolve(strict=True)
    except FileNotFoundError as exc:
        raise ValueError(f"路径不存在：{raw}") from exc
    except OSError as exc:
        raise ValueError(f"无法访问路径：{raw}（{exc}）") from exc

    if path.is_file():
        return [_read_local_json_file(path)]

    if not path.is_dir():
        raise ValueError(f"路径既不是文件也不是目录：{raw}")

    files = sorted(
        candidate
        for candidate in path.rglob("*.json")
        if candidate.is_file()
    )
    if not files:
        raise ValueError(f"目录下未找到 .json：{path}")
    if len(files) > MAX_LOCAL_IMPORT_FILES:
        raise ValueError(
            f"目录内 JSON 过多（{len(files)} > {MAX_LOCAL_IMPORT_FILES}），请缩小范围"
        )

    items: list[tuple[str, bytes]] = []
    used_names: set[str] = set()
    for file_path in files:
        name, content = _read_local_json_file(file_path)
        # 目录内可能有同名文件，必要时用相对路径 stem 消歧。
        if name in used_names:
            try:
                relative = file_path.relative_to(path)
            except ValueError:
                relative = Path(file_path.name)
            stem = _sanitize_filename_part(str(relative.with_suffix("")), max_len=120)
            name = f"{stem}.json"
            index = 1
            while name in used_names:
                name = f"{stem} ({index}).json"
                index += 1
        used_names.add(name)
        items.append((name, content))
    return items


def _worker_client(client: ManagementApiClient) -> ManagementApiClient:
    base_url = getattr(client, "base_url", None)
    secret_key = getattr(client, "secret_key", None)
    # 仅在真实字符串时重建，避免 MagicMock 误建真实 client。
    if isinstance(base_url, str) and isinstance(secret_key, str):
        return ManagementApiClient(base_url, secret_key)
    return client


def _classify_duplicate(
    target_name: str,
    provider: str,
    email: str,
    account_id: str,
    existing_credentials: list[CredentialRecord],
) -> str:
    for credential in existing_credentials:
        if credential.name == target_name:
            return "name"
    if provider and email:
        for credential in existing_credentials:
            if credential.provider == provider and credential.email.lower() == email.lower():
                return "provider_email"
    if provider and account_id:
        for credential in existing_credentials:
            raw_account_id = str(credential.raw.get("account_id", "")).strip()
            if credential.provider == provider and raw_account_id and raw_account_id == account_id:
                return "provider_account_id"
    return ""


def _preview_single_file(
    filename: str,
    raw_content: bytes,
    existing_credentials: list[CredentialRecord],
    now: datetime,
) -> ImportPreviewItem:
    warnings: list[str] = []
    errors: list[str] = []
    provider = ""
    email = ""
    account_id = ""
    payload: dict = {}

    source_name = Path(filename).name
    if not source_name.lower().endswith(".json"):
        errors.append("文件扩展名不是 .json")

    try:
        decoded = raw_content.decode("utf-8")
        loaded = json.loads(decoded)
        if not isinstance(loaded, dict):
            errors.append("JSON 顶层必须是对象")
            payload = {}
        else:
            payload = loaded
    except (UnicodeDecodeError, ValueError):
        errors.append("JSON 解析失败")
        payload = {}

    provider = str(payload.get("type", "")).strip().lower() if payload else ""
    email = str(payload.get("email", "")).strip() if payload else ""
    account_id = ""
    if payload:
        account_id = str(payload.get("account_id") or payload.get("sub") or "").strip()
    expires_at = parse_datetime(payload.get("expired")) if payload else None
    last_refresh = parse_datetime(payload.get("last_refresh")) if payload else None

    if payload and not provider:
        errors.append("缺少 type 字段")

    required_fields = REQUIRED_FIELDS_BY_PROVIDER.get(provider, ())
    missing_fields = [
        field_name
        for field_name in required_fields
        if not str(payload.get(field_name, "")).strip()
    ]
    if missing_fields:
        errors.append(f"{provider or '凭证'} 缺少字段：{', '.join(missing_fields)}")
    elif payload and provider and provider not in REQUIRED_FIELDS_BY_PROVIDER:
        # 未知 provider：至少要有 access_token，避免空壳导入
        if not str(payload.get("access_token") or payload.get("key") or "").strip():
            errors.append(f"未知 provider「{provider}」缺少 access_token")

    if provider in {"codex", "xai"}:
        raw_id_token = str(payload.get("id_token", "")).strip()
        if raw_id_token and len(raw_id_token.split(".")) != 3:
            warnings.append("id_token 不是标准三段 JWT")
        raw_access = str(payload.get("access_token", "")).strip()
        if raw_access and len(raw_access.split(".")) != 3:
            warnings.append("access_token 不是标准三段 JWT")

    if payload.get("expired") and expires_at is None:
        errors.append("过期时间格式非法")
    if payload.get("last_refresh") and last_refresh is None:
        warnings.append("上次刷新时间格式非法")

    expired_state = "unknown"
    if expires_at is not None:
        expired_state = "valid"
        if expires_at <= now:
            expired_state = "expired"
            warnings.append("凭证已过期")
        elif expires_at <= now + timedelta(hours=24):
            expired_state = "expiring"
            warnings.append("凭证将在 24 小时内过期")

    if last_refresh and expires_at and last_refresh > expires_at:
        warnings.append("上次刷新时间晚于过期时间")

    has_refresh_token = bool(str(payload.get("refresh_token") or "").strip()) if payload else False
    if expired_state == "expired" and has_refresh_token:
        warnings.append("含 refresh_token，确认导入后将尝试刷新")

    target_name = source_name
    duplicate_type = _classify_duplicate(
        target_name, provider, email, account_id, existing_credentials
    )
    if duplicate_type == "name":
        warnings.append("与现有文件同名")
    elif duplicate_type:
        warnings.append("与现有凭证重复")

    valid = not errors
    if duplicate_type == "name":
        available_actions = (ACTION_SKIP, ACTION_OVERWRITE, ACTION_RENAME)
        planned_action = ACTION_SKIP
    elif duplicate_type:
        available_actions = (ACTION_SKIP, ACTION_IMPORT)
        planned_action = ACTION_SKIP
    elif valid and expired_state == "expired":
        available_actions = (ACTION_SKIP, ACTION_IMPORT)
        # 过期但有 refresh_token：默认导入，执行阶段会尝试刷新
        planned_action = ACTION_IMPORT if has_refresh_token else ACTION_SKIP
    elif valid:
        available_actions = (ACTION_IMPORT, ACTION_SKIP)
        planned_action = ACTION_IMPORT
    else:
        available_actions = (ACTION_SKIP,)
        planned_action = ACTION_SKIP

    return ImportPreviewItem(
        source_name=source_name,
        target_name=target_name,
        provider=provider or "unknown",
        email=email,
        email_masked=mask_email(email),
        account_id=account_id,
        valid=valid,
        duplicate_type=duplicate_type,
        expired_state=expired_state,
        warnings=warnings,
        errors=errors,
        planned_action=planned_action,
        available_actions=available_actions,
        raw_payload=payload,
        raw_content=raw_content,
    )


def preview_import(
    file_items: list[tuple[str, bytes]],
    existing: list[CredentialRecord],
    progress_callback: ProgressCallback | None = None,
    *,
    max_workers: int = DEFAULT_MAX_WORKERS,
) -> list[ImportPreviewItem]:
    now = datetime.now(timezone.utc)
    if not file_items:
        return []

    def worker(item: tuple[str, bytes]) -> ImportPreviewItem:
        filename, raw_content = item
        return _preview_single_file(filename, raw_content, existing, now)

    def on_item_done(current: int, total: int, item: tuple[str, bytes]) -> None:
        if progress_callback:
            filename, _ = item
            progress_callback(current, total, f"正在预检 {current}/{total}：{Path(filename).name}")

    return run_ordered_parallel(
        file_items,
        worker,
        max_workers=max_workers,
        on_item_done=on_item_done,
    )


def _next_available_name(name: str, reserved_names: set[str]) -> str:
    path = Path(name)
    stem = path.stem
    suffix = path.suffix
    candidate = name
    index = 1
    while candidate in reserved_names:
        candidate = f"{stem} ({index}){suffix}"
        index += 1
    return candidate


def _prepare_import_content(
    item: ImportPreviewItem,
    *,
    refresh_tokens: bool,
    refresh_timeout_seconds: int,
) -> tuple[bytes, str]:
    """准备上传内容；可选在导入前用 refresh_token 刷新。"""
    if not refresh_tokens:
        return item.raw_content, "已导入"

    payload = item.raw_payload if isinstance(item.raw_payload, dict) else {}
    if not payload:
        try:
            loaded = json.loads(item.raw_content.decode("utf-8"))
            payload = loaded if isinstance(loaded, dict) else {}
        except Exception:  # noqa: BLE001
            payload = {}
    if not str(payload.get("refresh_token") or "").strip():
        return item.raw_content, "已导入（无 refresh_token）"

    try:
        updated, note = refresh_credential_payload(
            payload,
            timeout_seconds=refresh_timeout_seconds,
        )
        content = json.dumps(updated, ensure_ascii=False, indent=2).encode("utf-8")
        return content, f"已导入（{note}）"
    except TokenRefreshError as exc:
        # 刷新失败仍导入原始内容，避免整批导入被挡。
        return item.raw_content, f"已导入（刷新失败：{exc}）"


def execute_import(
    client: ManagementApiClient,
    items: list[ImportPreviewItem],
    existing: list[CredentialRecord],
    progress_callback: ProgressCallback | None = None,
    *,
    max_workers: int = DEFAULT_MAX_WORKERS,
    refresh_tokens: bool = False,
    refresh_timeout_seconds: int = 20,
) -> list[JobResult]:
    reserved_names = {credential.name for credential in existing}
    prepared_items: list[tuple[ImportPreviewItem, str]] = []
    for item in items:
        target_name = item.target_name
        if item.planned_action == ACTION_RENAME:
            target_name = _next_available_name(target_name, reserved_names)
            reserved_names.add(target_name)
        elif item.planned_action != ACTION_SKIP:
            reserved_names.add(target_name)
        prepared_items.append((item, target_name))

    def worker(prepared: tuple[ImportPreviewItem, str]) -> JobResult:
        item, target_name = prepared
        if item.planned_action == ACTION_SKIP:
            return JobResult(name=item.target_name, result="跳过", detail=item.summary)
        try:
            if refresh_tokens and progress_callback:
                # 线程内即时反馈：让任务区能看到“正在刷新”
                progress_callback(
                    0,
                    max(1, len(prepared_items)),
                    f"正在刷新 token：{target_name}",
                )
            content, detail = _prepare_import_content(
                item,
                refresh_tokens=refresh_tokens,
                refresh_timeout_seconds=refresh_timeout_seconds,
            )
            request_client = _worker_client(client)
            request_client.upload_credential(target_name, content)
            return JobResult(name=target_name, result="成功", detail=detail)
        except Exception as exc:  # noqa: BLE001 - surface upload failure to job result
            return JobResult(name=target_name, result="失败", detail=str(exc))

    def on_item_done(current: int, total: int, prepared: tuple[ImportPreviewItem, str]) -> None:
        if progress_callback:
            _, target_name = prepared
            verb = "刷新并导入" if refresh_tokens else "导入"
            progress_callback(current, total, f"正在{verb} {current}/{total}：{target_name}")

    return run_ordered_parallel(
        prepared_items,
        worker,
        max_workers=max_workers,
        on_item_done=on_item_done,
    )


def export_credentials_to_zip(
    client: ManagementApiClient,
    credentials: list[CredentialRecord],
    zip_path: str,
    *,
    delete_after_export: bool = False,
    max_workers: int = DEFAULT_MAX_WORKERS,
    progress_callback: ProgressCallback | None = None,
) -> list[JobResult]:
    """Export credentials into a ZIP file, optionally deleting only successful downloads."""
    total = len(credentials)
    if total == 0:
        Path(zip_path).parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            pass
        return []

    # Phase 1: download concurrently; keep input order via run_ordered_parallel.
    def download_worker(credential: CredentialRecord) -> tuple[CredentialRecord, bytes | None, str | None]:
        if not credential.can_export:
            return credential, None, "skip"
        try:
            request_client = _worker_client(client)
            content = request_client.download_credential(credential.name)
            return credential, content, None
        except Exception as exc:  # noqa: BLE001 - surface download failure to job result
            return credential, None, str(exc)

    def on_download_done(current: int, total_count: int, credential: CredentialRecord) -> None:
        if progress_callback:
            progress_callback(
                current,
                total_count,
                f"正在下载 {current}/{total_count}：{credential.name}",
            )

    download_rows = run_ordered_parallel(
        credentials,
        download_worker,
        max_workers=max_workers,
        on_item_done=on_download_done,
    )

    # Phase 2: write only successful downloads into ZIP (stable order by input).
    Path(zip_path).parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for credential, content, error in download_rows:
            if content is not None:
                # Use basename only to avoid path traversal inside the archive.
                zf.writestr(Path(credential.name).name, content)

    # Phase 3: build results; delete only when download succeeded and requested.
    results: list[JobResult] = []
    delete_targets = [
        credential
        for credential, content, error in download_rows
        if content is not None and delete_after_export
    ]

    delete_errors: dict[str, str] = {}
    if delete_targets:
        def delete_worker(credential: CredentialRecord) -> tuple[str, str | None]:
            try:
                request_client = _worker_client(client)
                request_client.delete_credential(credential.name)
                return credential.name, None
            except Exception as exc:  # noqa: BLE001 - surface delete failure to job result
                return credential.name, str(exc)

        def on_delete_done(current: int, total_count: int, credential: CredentialRecord) -> None:
            if progress_callback:
                progress_callback(
                    current,
                    total_count,
                    f"正在删除 {current}/{total_count}：{credential.name}",
                )

        delete_rows = run_ordered_parallel(
            delete_targets,
            delete_worker,
            max_workers=max_workers,
            on_item_done=on_delete_done,
        )
        for name, error in delete_rows:
            if error is not None:
                delete_errors[name] = error

    for credential, content, error in download_rows:
        if error == "skip":
            results.append(
                JobResult(
                    name=credential.name,
                    result="跳过",
                    detail="不可导出",
                )
            )
            continue
        if content is None:
            results.append(
                JobResult(
                    name=credential.name,
                    result="失败",
                    detail=error or "下载失败",
                )
            )
            continue
        if delete_after_export:
            delete_error = delete_errors.get(credential.name)
            if delete_error is None:
                results.append(
                    JobResult(
                        name=credential.name,
                        result="成功",
                        detail="已导出并删除",
                    )
                )
            else:
                results.append(
                    JobResult(
                        name=credential.name,
                        result="部分成功",
                        detail=f"已导出，删除失败：{delete_error}",
                    )
                )
        else:
            results.append(
                JobResult(
                    name=credential.name,
                    result="成功",
                    detail="已导出",
                )
            )
    return results
