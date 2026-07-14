from __future__ import annotations

import json
import tempfile
import threading
from pathlib import Path
from typing import Any

from fastapi import APIRouter, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, Field

from cpa_inspector.models import (
    AppSettings,
    ConnectionProfile,
    CredentialRecord,
    ImportPreviewItem,
    JobResult,
    format_datetime,
)
from cpa_inspector.services.api_client import ApiError, ManagementApiClient
from cpa_inspector.services.health_probe import probe_credentials
from cpa_inspector.services.import_export import (
    ACTION_IMPORT,
    ACTION_SKIP,
    collect_local_import_items,
    execute_import,
    export_credentials_to_zip,
    parse_paste_import_text,
    preview_import,
)
from cpa_inspector.services.jobs import Job, JobManager
from cpa_inspector.services.workspace import WorkspaceError, WorkspaceService

router = APIRouter(prefix="/api/cpa")


class SettingsPayload(BaseModel):
    max_parallel_workers: int = 4
    page_size: int = 50
    probe_model: str = "gpt-5"
    probe_timeout_seconds: int = 15
    probe_max_workers: int = 0
    import_refresh_tokens: bool = True
    import_refresh_timeout_seconds: int = 20
    auto_cleanup_scope: str = "all"
    auto_cleanup_match: str = "failed"
    auto_cleanup_keyword: str = "invalid_grant,revoked,bad-credentials,凭证无效"
    auto_cleanup_interval_seconds: int = 0
    auto_cleanup_max_rounds: int = 0


class ProfileItem(BaseModel):
    name: str
    base_url: str
    secret_key: str = ""
    last_used_at: str = ""


class ProfilesPayload(BaseModel):
    profiles: list[ProfileItem] = Field(default_factory=list)


class ConnectPayload(BaseModel):
    base_url: str
    secret_key: str = ""
    name: str = "default"


class NamesPayload(BaseModel):
    names: list[str] = Field(default_factory=list)
    # 探测专用并发；None/0 表示用设置里的 effective_probe_workers
    max_workers: int | None = None


class ImportActionItem(BaseModel):
    source_name: str
    planned_action: str = ACTION_IMPORT
    target_name: str | None = None


class ImportExecutePayload(BaseModel):
    items: list[ImportActionItem] = Field(default_factory=list)


class ImportPastePayload(BaseModel):
    text: str = ""


class ImportPathPayload(BaseModel):
    path: str = ""


def _workspace(request: Request) -> WorkspaceService:
    return request.app.state.workspace


def _store(request: Request):
    return request.app.state.profile_store


def _state(request: Request):
    return request.app.state.app_state


def _jobs(request: Request) -> JobManager:
    return request.app.state.job_manager


def _require_connected(workspace: WorkspaceService) -> None:
    if not workspace.state.connected or workspace.state.current_profile is None:
        raise HTTPException(status_code=400, detail="尚未连接，请先建立连接")


def _client_for(workspace: WorkspaceService) -> ManagementApiClient:
    _require_connected(workspace)
    profile = workspace.state.current_profile
    assert profile is not None
    existing = getattr(workspace, "_client", None)
    if existing is not None:
        return existing
    return ManagementApiClient(profile.base_url, profile.secret_key)


def _http_from_workspace_error(exc: WorkspaceError) -> HTTPException:
    return HTTPException(status_code=400, detail=str(exc))


def _http_from_api_error(exc: ApiError) -> HTTPException:
    message = str(exc)
    status = 502
    if "鉴权失败" in message:
        status = 401
    return HTTPException(status_code=status, detail=message)


def _serialize_credential(item: CredentialRecord, *, detail: bool = False) -> dict[str, Any]:
    """列表/详情统一脱敏输出，绝不带 token / raw。"""
    payload: dict[str, Any] = {
        "local_key": item.local_key,
        "name": item.name,
        "provider": item.provider,
        "status": item.status,
        "status_display": item.status_display,
        "disabled": item.disabled,
        "unavailable": item.unavailable,
        "runtime_only": item.runtime_only,
        "source": item.source,
        "email_masked": item.email_masked,
        "account": item.account,
        "account_type": item.account_type,
        "auth_index": item.auth_index,
        "credential_id": item.credential_id,
        "label": item.label,
        "status_message": item.status_message,
        "priority": item.priority,
        "note": item.note,
        "prefix": item.prefix,
        "proxy_url": item.proxy_url,
        "size": item.size,
        "modtime": format_datetime(item.modtime),
        "created_at": format_datetime(item.created_at),
        "updated_at": format_datetime(item.updated_at),
        "last_refresh": format_datetime(item.last_refresh),
        "next_retry_after": format_datetime(item.next_retry_after),
        "can_export": item.can_export,
        "health_status": item.health_status,
        "health_display": item.health_display,
        "health_detail": item.health_detail,
        "health_checked_at": format_datetime(item.health_checked_at),
    }
    if detail:
        # 详情仍不暴露明文邮箱与任何 token 字段。
        payload["proxy_url"] = item.proxy_url or "-"
    return payload


def _serialize_preview(item: ImportPreviewItem) -> dict[str, Any]:
    return {
        "source_name": item.source_name,
        "target_name": item.target_name,
        "provider": item.provider,
        "email_masked": item.email_masked,
        "account_id": item.account_id,
        "valid": item.valid,
        "duplicate_type": item.duplicate_type,
        "expired_state": item.expired_state,
        "warnings": list(item.warnings),
        "errors": list(item.errors),
        "planned_action": item.planned_action,
        "available_actions": list(item.available_actions),
        "summary": item.summary,
    }


def _serialize_job_result(item: JobResult) -> dict[str, Any]:
    return {"name": item.name, "result": item.result, "detail": item.detail}


def _serialize_job(job: Job) -> dict[str, Any]:
    return {
        "job_id": job.job_id,
        "type": job.type,
        "status": job.status,
        "current": job.current,
        "total": job.total,
        "message": job.message,
        "results": [_serialize_job_result(item) for item in job.results],
        "download_path": job.download_path,
        "created_at": format_datetime(job.created_at),
        "finished_at": format_datetime(job.finished_at),
    }


def _find_credentials(workspace: WorkspaceService, names: list[str]) -> list[CredentialRecord]:
    wanted = [name.strip() for name in names if str(name).strip()]
    if not wanted:
        return []
    by_name = {item.name: item for item in workspace.state.credentials}
    missing = [name for name in wanted if name not in by_name]
    if missing:
        raise HTTPException(status_code=404, detail=f"未找到凭证：{', '.join(missing)}")
    return [by_name[name] for name in wanted]


def _count_export_results(results: list[JobResult]) -> tuple[int, int, int]:
    success = failed = skipped = 0
    for item in results:
        label = item.result
        if label in ("成功", "部分成功"):
            success += 1
        elif label == "跳过":
            skipped += 1
        else:
            failed += 1
    return success, failed, skipped


def _spawn_job(
    request: Request,
    *,
    job_type: str,
    total: int,
    worker,
) -> dict[str, str]:
    jobs = _jobs(request)
    if jobs.has_running(job_type):
        raise HTTPException(status_code=409, detail=f"已有进行中的 {job_type} 任务")
    job = jobs.create(job_type, total=total)

    def runner() -> None:
        try:
            worker(job)
            current = jobs.get(job.job_id)
            if current is not None and current.status in ("queued", "running"):
                jobs.finish(job.job_id, status="success")
        except Exception as exc:  # noqa: BLE001 - surface background failure
            jobs.update(job.job_id, current=job.current, message=str(exc), results=job.results)
            jobs.finish(job.job_id, status="failed")

    thread = threading.Thread(target=runner, name=f"job-{job_type}-{job.job_id[:8]}", daemon=True)
    thread.start()
    return {"job_id": job.job_id}


@router.get("/settings")
def get_settings(request: Request) -> dict[str, Any]:
    state = _state(request)
    return state.settings.to_dict()


@router.put("/settings")
def put_settings(payload: SettingsPayload, request: Request) -> dict[str, Any]:
    settings = AppSettings(
        max_parallel_workers=payload.max_parallel_workers,
        page_size=payload.page_size,
        probe_model=payload.probe_model,
        probe_timeout_seconds=payload.probe_timeout_seconds,
        probe_max_workers=payload.probe_max_workers,
        import_refresh_tokens=payload.import_refresh_tokens,
        import_refresh_timeout_seconds=payload.import_refresh_timeout_seconds,
        auto_cleanup_scope=payload.auto_cleanup_scope,
        auto_cleanup_match=payload.auto_cleanup_match,
        auto_cleanup_keyword=payload.auto_cleanup_keyword,
        auto_cleanup_interval_seconds=payload.auto_cleanup_interval_seconds,
        auto_cleanup_max_rounds=payload.auto_cleanup_max_rounds,
    )
    state = _state(request)
    state.settings = settings
    _store(request).save_app_settings(settings)
    return settings.to_dict()


@router.get("/profiles")
def get_profiles(request: Request) -> dict[str, Any]:
    profiles = _store(request).load_profiles()
    return {"profiles": [item.to_dict() for item in profiles]}


@router.put("/profiles")
def put_profiles(payload: ProfilesPayload, request: Request) -> dict[str, Any]:
    profiles = [
        ConnectionProfile(
            name=item.name.strip(),
            base_url=item.base_url.strip(),
            secret_key=item.secret_key,
            last_used_at=item.last_used_at or "",
        )
        for item in payload.profiles
        if item.name.strip() and item.base_url.strip()
    ]
    _store(request).save_profiles(profiles)
    return {"profiles": [item.to_dict() for item in profiles]}


@router.post("/connect")
def connect(payload: ConnectPayload, request: Request) -> dict[str, Any]:
    workspace = _workspace(request)
    try:
        # 保证 patch 点 ManagementApiClient 生效
        workspace.client_factory = (
            lambda base_url, secret_key: ManagementApiClient(base_url, secret_key)
        )
        items = workspace.connect(
            base_url=payload.base_url,
            secret_key=payload.secret_key,
            profile_name=payload.name,
        )
    except WorkspaceError as exc:
        raise _http_from_workspace_error(exc) from exc
    except ApiError as exc:
        raise _http_from_api_error(exc) from exc
    return {
        "connected": True,
        "total": len(items),
        "profile": workspace.state.current_profile.to_dict()
        if workspace.state.current_profile
        else None,
    }


@router.post("/refresh")
def refresh(request: Request) -> dict[str, Any]:
    workspace = _workspace(request)
    try:
        workspace.client_factory = (
            lambda base_url, secret_key: ManagementApiClient(base_url, secret_key)
        )
        items = workspace.refresh()
    except WorkspaceError as exc:
        raise _http_from_workspace_error(exc) from exc
    except ApiError as exc:
        raise _http_from_api_error(exc) from exc
    return {"connected": True, "total": len(items)}


@router.get("/credentials")
def list_credentials(
    request: Request,
    page: int = Query(1, ge=1),
    page_size: int = Query(50),
    search_text: str = Query(""),
    status: str = Query("全部"),
    provider: str = Query("全部"),
    exportable: str = Query("全部"),
    health: str = Query("全部"),
) -> dict[str, Any]:
    workspace = _workspace(request)
    try:
        page_data = workspace.list_credentials(
            page=page,
            page_size=page_size,
            search_text=search_text,
            status=status,
            provider=provider,
            exportable=exportable,
            health=health,
        )
    except WorkspaceError as exc:
        raise _http_from_workspace_error(exc) from exc
    items = [_serialize_credential(item) for item in page_data["items"]]
    return {
        "items": items,
        "total": page_data["total"],
        "page": page_data["page"],
        "page_size": page_data["page_size"],
        "total_pages": page_data["total_pages"],
    }


@router.get("/credentials/detail")
def credential_detail(
    request: Request,
    name: str = Query(..., min_length=1),
) -> dict[str, Any]:
    workspace = _workspace(request)
    _require_connected(workspace)
    target = next((item for item in workspace.state.credentials if item.name == name), None)
    if target is None:
        raise HTTPException(status_code=404, detail=f"未找到凭证：{name}")
    return _serialize_credential(target, detail=True)


@router.post("/import/preview")
async def import_preview(
    request: Request,
    files: list[UploadFile] = File(...),
) -> dict[str, Any]:
    workspace = _workspace(request)
    _require_connected(workspace)
    file_items: list[tuple[str, bytes]] = []
    for upload in files:
        content = await upload.read()
        filename = upload.filename or "unknown.json"
        file_items.append((filename, content))
    # 缓存预检结果，供 execute 使用
    previews = preview_import(
        file_items,
        workspace.state.credentials,
        max_workers=workspace.state.settings.max_parallel_workers,
    )
    request.app.state.import_preview_cache = {
        item.source_name: item for item in previews
    }
    return {"items": [_serialize_preview(item) for item in previews], "total": len(previews)}


def _cache_and_serialize_previews(
    request: Request,
    file_items: list[tuple[str, bytes]],
    existing: list,
    max_workers: int,
    *,
    merge_cache: bool = False,
) -> dict[str, Any]:
    previews = preview_import(
        file_items,
        existing,
        max_workers=max_workers,
    )
    if merge_cache:
        cache: dict[str, ImportPreviewItem] = getattr(
            request.app.state, "import_preview_cache", {}
        ) or {}
        for item in previews:
            cache[item.source_name] = item
        request.app.state.import_preview_cache = cache
    else:
        request.app.state.import_preview_cache = {
            item.source_name: item for item in previews
        }
    return {
        "items": [_serialize_preview(item) for item in previews],
        "total": len(previews),
    }


@router.post("/import/preview-text")
def import_preview_text(payload: ImportPastePayload, request: Request) -> dict[str, Any]:
    """粘贴导入预检：识别 XAI JSON____SSO / 纯 JSON 文本。"""
    workspace = _workspace(request)
    _require_connected(workspace)
    text = (payload.text or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="粘贴内容为空")
    file_items = parse_paste_import_text(text)
    if not file_items:
        raise HTTPException(status_code=400, detail="未识别到可导入的 JSON 凭证")
    return _cache_and_serialize_previews(
        request,
        file_items,
        workspace.state.credentials,
        workspace.state.settings.max_parallel_workers,
        merge_cache=True,
    )


@router.post("/import/preview-path")
def import_preview_path(payload: ImportPathPayload, request: Request) -> dict[str, Any]:
    """本机路径导入预检：支持单文件或目录（递归 *.json）。"""
    workspace = _workspace(request)
    _require_connected(workspace)
    path_text = (payload.path or "").strip()
    if not path_text:
        raise HTTPException(status_code=400, detail="路径为空")
    try:
        file_items = collect_local_import_items(path_text)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except OSError as exc:
        raise HTTPException(status_code=400, detail=f"读取失败：{exc}") from exc
    return _cache_and_serialize_previews(
        request,
        file_items,
        workspace.state.credentials,
        workspace.state.settings.max_parallel_workers,
        merge_cache=False,
    )


@router.post("/import/execute")
def import_execute(payload: ImportExecutePayload, request: Request) -> dict[str, str]:
    workspace = _workspace(request)
    _require_connected(workspace)
    cache: dict[str, ImportPreviewItem] = getattr(
        request.app.state, "import_preview_cache", {}
    ) or {}
    if not payload.items:
        raise HTTPException(status_code=400, detail="导入列表为空")

    selected: list[ImportPreviewItem] = []
    for action in payload.items:
        source = action.source_name.strip()
        preview = cache.get(source)
        if preview is None:
            raise HTTPException(status_code=400, detail=f"缺少预检缓存：{source}，请先 preview")
        planned = (action.planned_action or ACTION_SKIP).strip().lower()
        if planned not in preview.available_actions:
            # 非法动作强制跳过，避免误覆盖
            planned = ACTION_SKIP if ACTION_SKIP in preview.available_actions else preview.planned_action
        target_name = (action.target_name or preview.target_name or source).strip() or source
        selected.append(
            ImportPreviewItem(
                source_name=preview.source_name,
                target_name=target_name,
                provider=preview.provider,
                email=preview.email,
                email_masked=preview.email_masked,
                account_id=preview.account_id,
                valid=preview.valid,
                duplicate_type=preview.duplicate_type,
                expired_state=preview.expired_state,
                warnings=list(preview.warnings),
                errors=list(preview.errors),
                planned_action=planned,
                available_actions=tuple(preview.available_actions),
                raw_payload=dict(preview.raw_payload),
                raw_content=preview.raw_content,
            )
        )

    client = _client_for(workspace)
    jobs = _jobs(request)
    settings = workspace.state.settings

    def worker(job: Job) -> None:
        def progress(current: int, total: int, message: str) -> None:
            jobs.update(job.job_id, current=current, message=message, results=job.results)

        results = execute_import(
            client,
            selected,
            workspace.state.credentials,
            progress_callback=progress,
            max_workers=settings.max_parallel_workers,
            refresh_tokens=settings.import_refresh_tokens,
            refresh_timeout_seconds=settings.import_refresh_timeout_seconds,
        )
        jobs.update(job.job_id, current=len(results), message="导入完成", results=results)
        # 导入后刷新本地缓存，失败不阻断任务成功状态
        try:
            workspace.client_factory = (
                lambda base_url, secret_key: ManagementApiClient(base_url, secret_key)
            )
            workspace.refresh()
        except Exception:  # noqa: BLE001
            pass

    return _spawn_job(
        request,
        job_type="import",
        total=len(selected),
        worker=worker,
    )


def _export_response(
    request: Request,
    names: list[str],
    *,
    delete_after_export: bool,
) -> Response:
    workspace = _workspace(request)
    _require_connected(workspace)
    credentials = _find_credentials(workspace, names)
    if not credentials:
        raise HTTPException(status_code=400, detail="未选择任何凭证")

    client = _client_for(workspace)
    settings = workspace.state.settings
    tmp = tempfile.NamedTemporaryFile(prefix="cpa-export-", suffix=".zip", delete=False)
    tmp_path = tmp.name
    tmp.close()
    try:
        results = export_credentials_to_zip(
            client,
            credentials,
            tmp_path,
            delete_after_export=delete_after_export,
            max_workers=settings.max_parallel_workers,
        )
        success, failed, skipped = _count_export_results(results)
        if delete_after_export:
            # 删除成功后刷新本地缓存
            try:
                workspace.client_factory = (
                    lambda base_url, secret_key: ManagementApiClient(base_url, secret_key)
                )
                workspace.refresh()
            except Exception:  # noqa: BLE001
                pass
        headers = {
            "X-Export-Success": str(success),
            "X-Export-Failed": str(failed),
            "X-Export-Skipped": str(skipped),
            # HTTP 头必须是 latin-1；中文结果用 \u 转义后可安全传输。
            "X-Job-Summary": json.dumps(
                {
                    "success": success,
                    "failed": failed,
                    "skipped": skipped,
                    "results": [_serialize_job_result(item) for item in results],
                },
                ensure_ascii=True,
            ),
            "Content-Disposition": 'attachment; filename="credentials-export.zip"',
        }
        data = Path(tmp_path).read_bytes()
        Path(tmp_path).unlink(missing_ok=True)
        return Response(content=data, media_type="application/zip", headers=headers)
    except Exception:
        Path(tmp_path).unlink(missing_ok=True)
        raise


@router.post("/export")
def export_credentials(payload: NamesPayload, request: Request) -> Response:
    return _export_response(request, payload.names, delete_after_export=False)


@router.post("/export-delete")
def export_delete_credentials(payload: NamesPayload, request: Request) -> Response:
    return _export_response(request, payload.names, delete_after_export=True)


@router.post("/credentials/delete")
def delete_credentials(payload: NamesPayload, request: Request) -> dict[str, str]:
    """批量删除远端凭证（仅按名称）。"""
    workspace = _workspace(request)
    _require_connected(workspace)
    names = [str(name).strip() for name in payload.names if str(name).strip()]
    if not names:
        raise HTTPException(status_code=400, detail="删除列表为空")

    client = _client_for(workspace)
    settings = workspace.state.settings
    jobs = _jobs(request)

    workers = settings.max_parallel_workers
    if payload.max_workers is not None:
        try:
            override = int(payload.max_workers)
        except (TypeError, ValueError):
            override = 0
        if override > 0:
            from cpa_inspector.models import clamp_parallel_workers

            workers = clamp_parallel_workers(override)

    def worker(job: Job) -> None:
        from cpa_inspector.services.parallel_jobs import run_ordered_parallel

        def delete_one(name: str) -> JobResult:
            try:
                request_client = client
                base_url = getattr(client, "base_url", None)
                secret_key = getattr(client, "secret_key", None)
                if isinstance(base_url, str) and isinstance(secret_key, str):
                    request_client = ManagementApiClient(base_url, secret_key)
                request_client.delete_credential(name)
                return JobResult(name=name, result="成功", detail="已删除")
            except Exception as exc:  # noqa: BLE001
                return JobResult(name=name, result="失败", detail=str(exc))

        def progress(current: int, total: int, name: str) -> None:
            jobs.update(
                job.job_id,
                current=current,
                message=f"正在删除 {current}/{total}：{name}",
                results=job.results,
            )

        results = run_ordered_parallel(
            names,
            delete_one,
            max_workers=workers,
            on_item_done=progress,
        )
        jobs.update(job.job_id, current=len(results), message="删除完成", results=results)
        try:
            workspace.client_factory = (
                lambda base_url, secret_key: ManagementApiClient(base_url, secret_key)
            )
            workspace.refresh()
        except Exception:  # noqa: BLE001
            pass

    return _spawn_job(
        request,
        job_type="delete",
        total=len(names),
        worker=worker,
    )


@router.post("/health-check")
def health_check(payload: NamesPayload, request: Request) -> dict[str, str]:
    workspace = _workspace(request)
    _require_connected(workspace)
    credentials = _find_credentials(workspace, payload.names)
    if not credentials:
        raise HTTPException(status_code=400, detail="未选择任何凭证")

    client = _client_for(workspace)
    settings = workspace.state.settings
    jobs = _jobs(request)

    # 请求体可临时覆盖探测并发；否则用探测专用并发/全局并发
    workers = settings.effective_probe_workers
    if payload.max_workers is not None:
        try:
            override = int(payload.max_workers)
        except (TypeError, ValueError):
            override = 0
        if override > 0:
            from cpa_inspector.models import clamp_parallel_workers

            workers = clamp_parallel_workers(override)

    def worker(job: Job) -> None:
        def progress(current: int, total: int, message: str) -> None:
            jobs.update(job.job_id, current=current, message=message, results=job.results)

        results = probe_credentials(
            client,
            credentials,
            model=settings.probe_model,
            timeout_seconds=settings.probe_timeout_seconds,
            max_workers=workers,
            progress_callback=progress,
            refresh_before_probe=settings.import_refresh_tokens,
            refresh_timeout_seconds=settings.import_refresh_timeout_seconds,
        )
        jobs.update(job.job_id, current=len(results), message="探测完成", results=results)

    return _spawn_job(
        request,
        job_type="health-check",
        total=len(credentials),
        worker=worker,
    )


@router.get("/jobs/{job_id}")
def get_job(job_id: str, request: Request) -> dict[str, Any]:
    job = _jobs(request).get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    return _serialize_job(job)
