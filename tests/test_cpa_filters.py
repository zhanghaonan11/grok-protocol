from __future__ import annotations

import unittest

from cpa_inspector.models import CredentialRecord
from cpa_inspector.services.filters import filter_credentials, paginate


def _item(**kwargs):
    payload = {
        "name": "a.json",
        "provider": "codex",
        "status": "active",
        "disabled": False,
        "unavailable": False,
        "runtime_only": False,
        "source": "file",
        "email": "a@example.com",
    }
    payload.update(kwargs)
    return CredentialRecord.from_api_payload(payload)


class FilterPaginationTest(unittest.TestCase):
    def test_filter_by_keyword_status_provider_export_health(self) -> None:
        healthy = _item(name="ok.json", email="ok@example.com")
        healthy.health_status = "healthy"
        disabled = _item(name="bad.json", disabled=True, email="bad@example.com")
        disabled.health_status = "failed"
        runtime = _item(name="mem", runtime_only=True, source="memory")

        rows = filter_credentials(
            [healthy, disabled, runtime],
            search_text="ok",
            status="活跃",
            provider="codex",
            exportable="仅可导出",
            health="健康",
        )
        self.assertEqual([item.name for item in rows], ["ok.json"])

    def test_paginate(self) -> None:
        items = list(range(1, 26))
        page = paginate(items, page=2, page_size=20)
        self.assertEqual(page["items"], list(range(21, 26)))
        self.assertEqual(page["total"], 25)
        self.assertEqual(page["page"], 2)
        self.assertEqual(page["page_size"], 20)
        self.assertEqual(page["total_pages"], 2)

    def test_page_less_than_one_clamps_to_one(self) -> None:
        items = list(range(1, 21))
        page = paginate(items, page=0, page_size=20)
        self.assertEqual(page["page"], 1)
        self.assertEqual(page["items"], list(range(1, 21)))

    def test_invalid_page_size_falls_back_to_50(self) -> None:
        items = list(range(1, 60))
        page = paginate(items, page=1, page_size=25)
        self.assertEqual(page["page_size"], 50)
        self.assertEqual(page["items"], list(range(1, 51)))
        self.assertEqual(page["total_pages"], 2)

    def test_empty_list_page_normalizes_to_one(self) -> None:
        page = paginate([], page=9, page_size=20)
        self.assertEqual(page["items"], [])
        self.assertEqual(page["total"], 0)
        self.assertEqual(page["page"], 1)
        self.assertEqual(page["page_size"], 20)
        self.assertEqual(page["total_pages"], 0)

    def test_sort_a_to_z_on_multiple_matches(self) -> None:
        rows = filter_credentials(
            [
                _item(name="zeta.json"),
                _item(name="Alpha.json"),
                _item(name="beta.json"),
            ]
        )
        self.assertEqual(
            [item.name for item in rows],
            ["Alpha.json", "beta.json", "zeta.json"],
        )

    def test_provider_filter_is_casefold(self) -> None:
        rows = filter_credentials(
            [_item(name="ok.json", provider="codex")],
            provider="CODEX",
        )
        self.assertEqual([item.name for item in rows], ["ok.json"])

    def test_unknown_health_and_exportable_yield_empty(self) -> None:
        healthy = _item(name="ok.json")
        healthy.health_status = "healthy"
        self.assertEqual(
            filter_credentials([healthy], exportable="随便"),
            [],
        )
        self.assertEqual(
            filter_credentials([healthy], health="坏掉了"),
            [],
        )

    def test_english_health_enum_works(self) -> None:
        healthy = _item(name="ok.json")
        healthy.health_status = "healthy"
        failed = _item(name="bad.json", disabled=True)
        failed.health_status = "failed"
        rows = filter_credentials([healthy, failed], health="healthy")
        self.assertEqual([item.name for item in rows], ["ok.json"])


if __name__ == "__main__":
    unittest.main()
