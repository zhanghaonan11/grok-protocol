from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable

from cpa_inspector.models import ConnectionProfile, CredentialRecord
from cpa_inspector.services.api_client import ManagementApiClient
from cpa_inspector.services.filters import filter_credentials, paginate
from cpa_inspector.services.profile_store import ProfileStore
from cpa_inspector.state import AppState


class WorkspaceError(RuntimeError):
    pass


class WorkspaceService:
    """连接 CPA、维护凭证列表，并做本地筛选分页。"""

    def __init__(
        self,
        state: AppState,
        store: ProfileStore,
        client_factory: Callable[[str, str], Any] | None = None,
    ) -> None:
        self.state = state
        self.store = store
        self.client_factory = client_factory or (
            lambda base_url, secret_key: ManagementApiClient(base_url, secret_key)
        )
        self._client: Any | None = None

    def connect(
        self,
        base_url: str,
        secret_key: str,
        profile_name: str = "default",
    ) -> list[CredentialRecord]:
        url = (base_url or "").strip()
        key = (secret_key or "").strip()
        name = (profile_name or "default").strip() or "default"
        if not url:
            raise WorkspaceError("base_url 不能为空")

        client = self.client_factory(url, key)
        items = self._fetch_and_merge(client)
        profile = ConnectionProfile(
            name=name,
            base_url=url,
            secret_key=key,
            last_used_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        )
        self._client = client
        self.state.current_profile = profile
        self.state.credentials = items
        self.state.connected = True
        self._persist_profile(profile)
        return items

    def refresh(self) -> list[CredentialRecord]:
        profile = self.state.current_profile
        if not self.state.connected or profile is None:
            raise WorkspaceError("尚未连接，请先调用 connect")
        client = self._client or self.client_factory(profile.base_url, profile.secret_key)
        items = self._fetch_and_merge(client)
        profile.last_used_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        self._client = client
        self.state.credentials = items
        self.state.connected = True
        self._persist_profile(profile)
        return items

    def list_credentials(self, **filters: Any) -> dict:
        if not self.state.connected:
            raise WorkspaceError("尚未连接，请先建立连接")
        page = filters.pop("page", 1)
        page_size = filters.pop("page_size", self.state.settings.page_size)
        filtered = filter_credentials(self.state.credentials, **filters)
        return paginate(filtered, page=page, page_size=page_size)

    def _fetch_and_merge(self, client: Any) -> list[CredentialRecord]:
        fetched = list(client.fetch_credentials())
        fetched.sort(key=lambda item: item.name.casefold())
        old_by_name = {item.name: item for item in self.state.credentials}
        for item in fetched:
            old = old_by_name.get(item.name)
            if old is None:
                continue
            # 刷新后尽量保留本地健康探测结果。
            item.health_status = old.health_status
            item.health_detail = old.health_detail
            item.health_checked_at = old.health_checked_at
        return fetched

    def _persist_profile(self, profile: ConnectionProfile) -> None:
        profiles = self.store.load_profiles()
        updated = False
        for idx, existing in enumerate(profiles):
            if existing.name == profile.name:
                profiles[idx] = profile
                updated = True
                break
        if not updated:
            profiles.append(profile)
        self.store.save_profiles(profiles)
