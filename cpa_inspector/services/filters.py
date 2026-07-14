from __future__ import annotations

from math import ceil
from typing import Iterable, Sequence, TypeVar

from cpa_inspector.models import HEALTH_STATUS_LABELS, CredentialRecord

T = TypeVar("T")

HEALTH_LABEL_TO_STATUS = {label: key for key, label in HEALTH_STATUS_LABELS.items()}
ALLOWED_PAGE_SIZES = {20, 50, 100}
_NO_MATCH = object()


def _matches_search(item: CredentialRecord, search_text: str) -> bool:
    if not search_text:
        return True
    needle = search_text.casefold()
    haystacks = (
        item.name,
        item.email,
        item.account,
        item.note,
        item.status_message,
    )
    return any(needle in value.casefold() for value in haystacks if value)


def _matches_exportable(item: CredentialRecord, exportable: str) -> bool:
    if exportable in ("", "全部"):
        return True
    if exportable == "仅可导出":
        return item.can_export is True
    if exportable == "仅不可导出":
        return item.can_export is False
    # 未知取值不按“不过滤”处理，直接匹配不到任何记录。
    return False


def _normalize_health(health: str):
    if health in ("", "全部"):
        return None
    if health in HEALTH_STATUS_LABELS:
        return health
    mapped = HEALTH_LABEL_TO_STATUS.get(health)
    if mapped is not None:
        return mapped
    return _NO_MATCH


def filter_credentials(
    credentials: Iterable[CredentialRecord],
    *,
    search_text: str = "",
    status: str = "全部",
    provider: str = "全部",
    exportable: str = "全部",
    health: str = "全部",
) -> list[CredentialRecord]:
    """按关键词、状态、provider、可导出、健康状态筛选，并按名称 A-Z 排序。"""
    search = (search_text or "").strip()
    status_filter = (status or "全部").strip()
    provider_filter = (provider or "全部").strip()
    exportable_filter = (exportable or "全部").strip()
    health_filter = _normalize_health((health or "全部").strip())

    rows: list[CredentialRecord] = []
    for item in credentials:
        if not _matches_search(item, search):
            continue
        if status_filter not in ("", "全部") and item.status_display != status_filter:
            continue
        if provider_filter not in ("", "全部") and item.provider.casefold() != provider_filter.casefold():
            continue
        if not _matches_exportable(item, exportable_filter):
            continue
        if health_filter is _NO_MATCH:
            continue
        if health_filter is not None and item.health_status != health_filter:
            continue
        rows.append(item)

    rows.sort(key=lambda item: item.name.casefold())
    return rows


def _normalize_page_size(page_size: int) -> int:
    try:
        size = int(page_size)
    except (TypeError, ValueError):
        return 50
    if size in ALLOWED_PAGE_SIZES:
        return size
    # 仅允许 20/50/100，其它一律回落 50。
    return 50


def paginate(items: Sequence[T], page: int, page_size: int) -> dict:
    """对列表分页，返回 items/total/page/page_size/total_pages。"""
    try:
        page_num = int(page)
    except (TypeError, ValueError):
        page_num = 1
    if page_num < 1:
        page_num = 1

    size = _normalize_page_size(page_size)

    total = len(items)
    total_pages = ceil(total / size) if total else 0
    if total_pages == 0:
        page_num = 1
    elif page_num > total_pages:
        page_num = total_pages

    start = (page_num - 1) * size
    end = start + size
    return {
        "items": list(items[start:end]),
        "total": total,
        "page": page_num,
        "page_size": size,
        "total_pages": total_pages,
    }
