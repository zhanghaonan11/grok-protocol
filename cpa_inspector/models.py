from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


HEALTH_STATUS_LABELS = {
    "untested": "未测",
    "healthy": "健康",
    "failed": "失败",
    "uncertain": "不确定",
}


def parse_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    text = text.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def format_datetime(value: datetime | None) -> str:
    if value is None:
        return "-"
    return value.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")


def mask_secret(value: str | None, keep: int = 3) -> str:
    if not value:
        return "-"
    text = value.strip()
    if len(text) <= keep * 2:
        return text
    return f"{text[:keep]}...{text[-keep:]}"


def mask_email(value: str | None) -> str:
    if not value:
        return "-"
    text = value.strip()
    if "@" not in text:
        return mask_secret(text, keep=2)
    left, right = text.split("@", 1)
    if len(left) <= 2:
        left_masked = left[0] + "*"
    else:
        left_masked = left[:2] + "***"
    return f"{left_masked}@{right}"


def clamp_parallel_workers(value: Any, default: int = 4) -> int:
    if isinstance(value, bool):
        return default
    try:
        workers = int(value)
    except (TypeError, ValueError):
        return default
    return max(1, min(workers, 32))


@dataclass(slots=True)
class AppSettings:
    max_parallel_workers: int = 4
    page_size: int = 50
    probe_model: str = "gpt-5"
    probe_timeout_seconds: int = 15
    # 健康探测专用并发；0/空表示跟随全局 max_parallel_workers
    probe_max_workers: int = 0
    # 导入时若凭证带 refresh_token，是否尝试 OAuth 刷新
    import_refresh_tokens: bool = True
    import_refresh_timeout_seconds: int = 20
    # 自动巡检删除规则（仅保存规则；不会自动启动）
    auto_cleanup_scope: str = "all"  # all | filtered
    auto_cleanup_match: str = "failed"  # failed | failed_uncertain
    auto_cleanup_keyword: str = "invalid_grant,revoked,bad-credentials,凭证无效"
    auto_cleanup_interval_seconds: int = 0
    auto_cleanup_max_rounds: int = 0  # 0=不限轮次，直到本轮无匹配

    def __post_init__(self) -> None:
        self.max_parallel_workers = clamp_parallel_workers(self.max_parallel_workers)
        try:
            page_size = int(self.page_size)
        except (TypeError, ValueError):
            page_size = 50
        if page_size not in (20, 50, 100):
            page_size = 50
        self.page_size = page_size
        self.probe_model = str(self.probe_model or "gpt-5").strip() or "gpt-5"
        try:
            timeout = int(self.probe_timeout_seconds)
        except (TypeError, ValueError):
            timeout = 15
        self.probe_timeout_seconds = max(1, timeout)
        try:
            probe_workers = int(self.probe_max_workers)
        except (TypeError, ValueError):
            probe_workers = 0
        # 0 表示跟随全局并发；1~32 为探测专用并发
        if probe_workers <= 0:
            self.probe_max_workers = 0
        else:
            self.probe_max_workers = clamp_parallel_workers(probe_workers)
        self.import_refresh_tokens = bool(self.import_refresh_tokens)
        try:
            refresh_timeout = int(self.import_refresh_timeout_seconds)
        except (TypeError, ValueError):
            refresh_timeout = 20
        self.import_refresh_timeout_seconds = max(1, refresh_timeout)

        scope = str(self.auto_cleanup_scope or "all").strip().lower()
        self.auto_cleanup_scope = scope if scope in {"all", "filtered"} else "all"
        match = str(self.auto_cleanup_match or "failed").strip().lower()
        self.auto_cleanup_match = (
            match if match in {"failed", "failed_uncertain"} else "failed"
        )
        self.auto_cleanup_keyword = str(
            self.auto_cleanup_keyword
            or "invalid_grant,revoked,bad-credentials,凭证无效"
        ).strip()
        try:
            interval = int(self.auto_cleanup_interval_seconds)
        except (TypeError, ValueError):
            interval = 0
        self.auto_cleanup_interval_seconds = max(0, interval)
        try:
            rounds = int(self.auto_cleanup_max_rounds)
        except (TypeError, ValueError):
            rounds = 0
        self.auto_cleanup_max_rounds = max(0, rounds)

    @property
    def effective_probe_workers(self) -> int:
        if self.probe_max_workers > 0:
            return self.probe_max_workers
        return self.max_parallel_workers

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_parallel_workers": self.max_parallel_workers,
            "page_size": self.page_size,
            "probe_model": self.probe_model,
            "probe_timeout_seconds": self.probe_timeout_seconds,
            "probe_max_workers": self.probe_max_workers,
            "import_refresh_tokens": self.import_refresh_tokens,
            "import_refresh_timeout_seconds": self.import_refresh_timeout_seconds,
            "auto_cleanup_scope": self.auto_cleanup_scope,
            "auto_cleanup_match": self.auto_cleanup_match,
            "auto_cleanup_keyword": self.auto_cleanup_keyword,
            "auto_cleanup_interval_seconds": self.auto_cleanup_interval_seconds,
            "auto_cleanup_max_rounds": self.auto_cleanup_max_rounds,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "AppSettings":
        return cls(
            max_parallel_workers=payload.get("max_parallel_workers", 4),
            page_size=payload.get("page_size", 50),
            probe_model=payload.get("probe_model", "gpt-5"),
            probe_timeout_seconds=payload.get("probe_timeout_seconds", 15),
            probe_max_workers=payload.get("probe_max_workers", 0),
            import_refresh_tokens=payload.get("import_refresh_tokens", True),
            import_refresh_timeout_seconds=payload.get(
                "import_refresh_timeout_seconds", 20
            ),
            auto_cleanup_scope=payload.get("auto_cleanup_scope", "all"),
            auto_cleanup_match=payload.get("auto_cleanup_match", "failed"),
            auto_cleanup_keyword=payload.get(
                "auto_cleanup_keyword",
                "invalid_grant,revoked,bad-credentials,凭证无效",
            ),
            auto_cleanup_interval_seconds=payload.get(
                "auto_cleanup_interval_seconds", 0
            ),
            auto_cleanup_max_rounds=payload.get("auto_cleanup_max_rounds", 0),
        )


@dataclass(slots=True)
class ConnectionProfile:
    name: str
    base_url: str
    secret_key: str
    last_used_at: str = ""

    def to_dict(self) -> dict[str, str]:
        return {
            "name": self.name,
            "base_url": self.base_url,
            "secret_key": self.secret_key,
            "last_used_at": self.last_used_at,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "ConnectionProfile":
        return cls(
            name=str(payload.get("name", "")).strip(),
            base_url=str(payload.get("base_url", "")).strip(),
            secret_key=str(payload.get("secret_key", "")).strip(),
            last_used_at=str(payload.get("last_used_at", "")).strip(),
        )


@dataclass(slots=True)
class CredentialRecord:
    local_key: str
    name: str
    provider: str
    status: str
    disabled: bool
    unavailable: bool
    runtime_only: bool
    source: str
    email: str = ""
    email_masked: str = "-"
    account: str = ""
    account_type: str = ""
    auth_index: str = ""
    credential_id: str = ""
    label: str = ""
    status_message: str = ""
    priority: int | None = None
    note: str = ""
    prefix: str = ""
    proxy_url: str = ""
    size: int = 0
    modtime: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    last_refresh: datetime | None = None
    next_retry_after: datetime | None = None
    health_status: str = "untested"
    health_detail: str = ""
    health_checked_at: datetime | None = None
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def can_export(self) -> bool:
        return not self.runtime_only and self.source != "memory" and self.name.lower().endswith(".json")

    @property
    def status_display(self) -> str:
        if self.disabled:
            return "已停用"
        if self.unavailable:
            return "暂不可用"
        if self.status:
            if self.status == "active":
                return "活跃"
            if self.status == "disabled":
                return "已停用"
            if self.status == "unknown":
                return "未知"
            return self.status
        return "未知"

    @property
    def health_display(self) -> str:
        return HEALTH_STATUS_LABELS.get(self.health_status, self.health_status or "未测")

    @classmethod
    def from_api_payload(cls, payload: dict[str, Any]) -> "CredentialRecord":
        auth_index = str(payload.get("auth_index", "")).strip()
        credential_id = str(payload.get("id", "")).strip()
        name = str(payload.get("name", "")).strip()
        provider = str(payload.get("provider") or payload.get("type") or "unknown").strip().lower()
        email = str(payload.get("email", "")).strip()
        priority_raw = payload.get("priority")
        priority: int | None = None
        if isinstance(priority_raw, int):
            priority = priority_raw
        elif isinstance(priority_raw, float):
            priority = int(priority_raw)
        elif isinstance(priority_raw, str) and priority_raw.strip():
            try:
                priority = int(priority_raw.strip())
            except ValueError:
                priority = None
        local_key = auth_index or credential_id or name
        size_raw = payload.get("size", 0)
        size = 0
        if isinstance(size_raw, bool):
            size = 0
        elif isinstance(size_raw, int):
            size = size_raw
        elif isinstance(size_raw, float):
            size = int(size_raw)
        elif isinstance(size_raw, str) and size_raw.strip():
            try:
                size = int(size_raw.strip())
            except ValueError:
                size = 0
        else:
            try:
                size = int(size_raw or 0)
            except (TypeError, ValueError):
                size = 0
        return cls(
            local_key=local_key,
            name=name,
            provider=provider,
            status=str(payload.get("status", "unknown")).strip().lower() or "unknown",
            disabled=bool(payload.get("disabled", False)),
            unavailable=bool(payload.get("unavailable", False)),
            runtime_only=bool(payload.get("runtime_only", False)),
            source=str(payload.get("source", "unknown")).strip() or "unknown",
            email=email,
            email_masked=mask_email(email),
            account=str(payload.get("account", "")).strip(),
            account_type=str(payload.get("account_type", "")).strip(),
            auth_index=auth_index,
            credential_id=credential_id,
            label=str(payload.get("label", "")).strip(),
            status_message=str(payload.get("status_message", "")).strip(),
            priority=priority,
            note=str(payload.get("note", "")).strip(),
            prefix=str(payload.get("prefix", "")).strip(),
            proxy_url=str(payload.get("proxy_url", "")).strip(),
            size=size,
            modtime=parse_datetime(payload.get("modtime")),
            created_at=parse_datetime(payload.get("created_at")),
            updated_at=parse_datetime(payload.get("updated_at")),
            last_refresh=parse_datetime(payload.get("last_refresh")),
            next_retry_after=parse_datetime(payload.get("next_retry_after")),
            health_status=str(payload.get("health_status", "untested")).strip() or "untested",
            health_detail=str(payload.get("health_detail", "")).strip(),
            health_checked_at=parse_datetime(payload.get("health_checked_at")),
            raw=dict(payload),
        )


@dataclass(slots=True)
class ImportPreviewItem:
    source_name: str
    target_name: str
    provider: str
    email: str = ""
    email_masked: str = "-"
    account_id: str = ""
    valid: bool = True
    duplicate_type: str = ""
    expired_state: str = ""
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    planned_action: str = "import"
    available_actions: tuple[str, ...] = ("import",)
    raw_payload: dict[str, Any] = field(default_factory=dict, repr=False)
    raw_content: bytes = field(default=b"", repr=False)

    @property
    def summary(self) -> str:
        parts = [*self.errors, *self.warnings]
        return "；".join(parts) if parts else "通过"


@dataclass(slots=True)
class JobResult:
    name: str
    result: str
    detail: str = ""
