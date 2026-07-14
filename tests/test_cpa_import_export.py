from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from cpa_inspector.models import CredentialRecord, ImportPreviewItem
from cpa_inspector.services.import_export import (
    ACTION_IMPORT,
    ACTION_OVERWRITE,
    ACTION_RENAME,
    ACTION_SKIP,
    collect_local_import_items,
    execute_import,
    parse_paste_import_text,
    preview_import,
    suggest_credential_filename,
)


def _valid_codex_bytes(
    *,
    email: str = "a@x.com",
    expired: str = "2099-01-01T00:00:00Z",
) -> bytes:
    payload = {
        "type": "codex",
        "access_token": "x",
        "refresh_token": "y",
        "id_token": "a.b.c",
        "email": email,
        "expired": expired,
    }
    return json.dumps(payload).encode("utf-8")


def _existing(name: str = "a.json") -> CredentialRecord:
    return CredentialRecord.from_api_payload(
        {
            "name": name,
            "provider": "codex",
            "status": "active",
            "disabled": False,
            "unavailable": False,
            "runtime_only": False,
            "source": "file",
            "email": "old@x.com",
        }
    )


class ImportExportTest(unittest.TestCase):
    def test_illegal_json_only_allows_skip(self) -> None:
        items = preview_import([("bad.json", b"{not-json")], existing=[])
        self.assertEqual(len(items), 1)
        item = items[0]
        self.assertFalse(item.valid)
        self.assertTrue(item.errors)
        self.assertEqual(item.planned_action, ACTION_SKIP)
        self.assertEqual(item.available_actions, (ACTION_SKIP,))

    def test_same_name_defaults_to_skip_with_overwrite_rename(self) -> None:
        items = preview_import(
            [("a.json", _valid_codex_bytes())],
            existing=[_existing("a.json")],
        )
        item = items[0]
        self.assertEqual(item.duplicate_type, "name")
        self.assertEqual(item.planned_action, ACTION_SKIP)
        self.assertEqual(
            item.available_actions,
            (ACTION_SKIP, ACTION_OVERWRITE, ACTION_RENAME),
        )

    def test_valid_new_file_defaults_to_import(self) -> None:
        items = preview_import(
            [("a.json", _valid_codex_bytes())],
            existing=[],
        )
        item = items[0]
        self.assertTrue(item.valid)
        self.assertEqual(item.planned_action, ACTION_IMPORT)
        self.assertIn(ACTION_IMPORT, item.available_actions)
        self.assertEqual(item.source_name, "a.json")
        self.assertEqual(item.target_name, "a.json")

    def test_rename_generates_stem_index_name(self) -> None:
        item = ImportPreviewItem(
            source_name="a.json",
            target_name="a.json",
            provider="codex",
            planned_action=ACTION_RENAME,
            available_actions=(ACTION_SKIP, ACTION_OVERWRITE, ACTION_RENAME),
            raw_content=_valid_codex_bytes(),
        )
        client = MagicMock()
        # Reuse the same mock client in worker path (no base_url/secret_key rebuild).
        del client.base_url
        results = execute_import(
            client,
            [item],
            existing=[_existing("a.json")],
            max_workers=1,
        )
        self.assertEqual(results[0].result, "成功")
        self.assertEqual(results[0].name, "a (1).json")
        client.upload_credential.assert_called_once_with("a (1).json", item.raw_content)

    def test_execute_skip_does_not_upload_others_do(self) -> None:
        raw = _valid_codex_bytes()
        skip_item = ImportPreviewItem(
            source_name="skip.json",
            target_name="skip.json",
            provider="codex",
            planned_action=ACTION_SKIP,
            available_actions=(ACTION_SKIP,),
            raw_content=raw,
        )
        import_item = ImportPreviewItem(
            source_name="import.json",
            target_name="import.json",
            provider="codex",
            planned_action=ACTION_IMPORT,
            available_actions=(ACTION_IMPORT, ACTION_SKIP),
            raw_content=raw,
        )
        overwrite_item = ImportPreviewItem(
            source_name="over.json",
            target_name="over.json",
            provider="codex",
            planned_action=ACTION_OVERWRITE,
            available_actions=(ACTION_SKIP, ACTION_OVERWRITE, ACTION_RENAME),
            raw_content=raw,
        )
        rename_item = ImportPreviewItem(
            source_name="rename.json",
            target_name="rename.json",
            provider="codex",
            planned_action=ACTION_RENAME,
            available_actions=(ACTION_SKIP, ACTION_OVERWRITE, ACTION_RENAME),
            raw_content=raw,
        )

        client = MagicMock()
        del client.base_url

        results = execute_import(
            client,
            [skip_item, import_item, overwrite_item, rename_item],
            existing=[_existing("rename.json")],
            max_workers=1,
        )

        self.assertEqual(results[0].result, "跳过")
        self.assertEqual(results[1].result, "成功")
        self.assertEqual(results[2].result, "成功")
        self.assertEqual(results[3].result, "成功")
        self.assertEqual(results[3].name, "rename (1).json")

        uploaded_names = [call.args[0] for call in client.upload_credential.call_args_list]
        self.assertEqual(
            uploaded_names,
            ["import.json", "over.json", "rename (1).json"],
        )
        self.assertNotIn("skip.json", uploaded_names)

    def test_xai_valid_file_imports(self) -> None:
        payload = {
            "type": "xai",
            "access_token": "a.b.c",
            "refresh_token": "rt",
            "id_token": "d.e.f",
            "email": "user@x.ai",
            "expired": "2099-01-01T00:00:00Z",
            "sub": "sub-1",
        }
        raw = json.dumps(payload).encode("utf-8")
        items = preview_import([("xai-user@x.ai.json", raw)], existing=[])
        item = items[0]
        self.assertTrue(item.valid)
        self.assertEqual(item.provider, "xai")
        self.assertEqual(item.planned_action, ACTION_IMPORT)
        self.assertEqual(item.account_id, "sub-1")

    def test_xai_expired_with_refresh_defaults_to_import(self) -> None:
        payload = {
            "type": "xai",
            "access_token": "a.b.c",
            "refresh_token": "rt",
            "email": "user@x.ai",
            "expired": "2020-01-01T00:00:00Z",
            "sub": "sub-1",
        }
        raw = json.dumps(payload).encode("utf-8")
        items = preview_import([("xai-expired.json", raw)], existing=[])
        item = items[0]
        self.assertTrue(item.valid)
        self.assertEqual(item.expired_state, "expired")
        self.assertEqual(item.planned_action, ACTION_IMPORT)
        self.assertTrue(any("refresh_token" in w for w in item.warnings))

    def test_xai_expired_without_refresh_defaults_to_skip(self) -> None:
        payload = {
            "type": "xai",
            "access_token": "a.b.c",
            "refresh_token": "rt",
            "email": "user@x.ai",
            "expired": "2020-01-01T00:00:00Z",
        }
        # 故意去掉 refresh 后，xai 必填校验会失败；这里改用 unknown provider 仅测过期默认动作
        payload = {
            "type": "other",
            "access_token": "a.b.c",
            "email": "user@x.ai",
            "expired": "2020-01-01T00:00:00Z",
        }
        raw = json.dumps(payload).encode("utf-8")
        items = preview_import([("other-expired.json", raw)], existing=[])
        item = items[0]
        self.assertTrue(item.valid)
        self.assertEqual(item.expired_state, "expired")
        self.assertEqual(item.planned_action, ACTION_SKIP)

    def test_xai_missing_refresh_is_invalid(self) -> None:
        payload = {
            "type": "xai",
            "access_token": "a.b.c",
            "email": "user@x.ai",
            "expired": "2099-01-01T00:00:00Z",
        }
        raw = json.dumps(payload).encode("utf-8")
        items = preview_import([("xai.json", raw)], existing=[])
        self.assertFalse(items[0].valid)
        self.assertIn("refresh_token", items[0].errors[0])

    def test_parse_paste_json_with_sso_tail(self) -> None:
        payload = {
            "type": "xai",
            "access_token": "eyJhbGciOiJIUzI1NiJ9.e30.sig",
            "refresh_token": "rt",
            "email": "xaixxemfpxalt@uivm.top",
            "expired": "2026-07-12T15:43:20Z",
            "sub": "e9bdd96d-5ae2-4402-a8d3-4424de380ddd",
        }
        sso = "eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9.eyJzZXNzaW9uX2lkIjoiMjY4NWQ3OTAtMTIyMC00YzNmLTk0MTYtOWQ1ZDY5NzNkZjM5In0.sig"
        text = json.dumps(payload, ensure_ascii=False) + "____" + sso
        items = parse_paste_import_text(text)
        self.assertEqual(len(items), 1)
        name, content = items[0]
        self.assertEqual(name, "xai-xaixxemfpxalt@uivm.top.json")
        loaded = json.loads(content.decode("utf-8"))
        self.assertEqual(loaded["type"], "xai")
        self.assertEqual(loaded["email"], "xaixxemfpxalt@uivm.top")
        self.assertNotIn("session_id", json.dumps(loaded))

    def test_parse_paste_multiple_json_objects(self) -> None:
        a = {
            "type": "xai",
            "access_token": "a.b.c",
            "refresh_token": "r1",
            "email": "a@x.ai",
            "expired": "2099-01-01T00:00:00Z",
        }
        b = {
            "type": "xai",
            "access_token": "d.e.f",
            "refresh_token": "r2",
            "email": "b@x.ai",
            "expired": "2099-01-01T00:00:00Z",
        }
        text = json.dumps(a) + "____sso1\n" + json.dumps(b) + "----sso2"
        items = parse_paste_import_text(text)
        self.assertEqual(len(items), 2)
        names = [name for name, _ in items]
        self.assertEqual(names, ["xai-a@x.ai.json", "xai-b@x.ai.json"])

    def test_suggest_filename_avoids_collision(self) -> None:
        payload = {"type": "xai", "email": "a@x.ai"}
        first = suggest_credential_filename(payload, set())
        second = suggest_credential_filename(payload, {first})
        self.assertEqual(first, "xai-a@x.ai.json")
        self.assertEqual(second, "xai-a@x.ai (1).json")

    def test_collect_local_import_single_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "demo.json"
            path.write_text(
                json.dumps(
                    {
                        "type": "xai",
                        "access_token": "a.b.c",
                        "refresh_token": "rt",
                        "email": "a@x.ai",
                        "expired": "2099-01-01T00:00:00Z",
                    }
                ),
                encoding="utf-8",
            )
            items = collect_local_import_items(str(path))
            self.assertEqual(len(items), 1)
            self.assertEqual(items[0][0], "demo.json")

    def test_collect_local_import_directory_recursive(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "a.json").write_text('{"type":"xai"}', encoding="utf-8")
            nested = root / "sub"
            nested.mkdir()
            (nested / "b.json").write_text('{"type":"codex"}', encoding="utf-8")
            (nested / "skip.txt").write_text("nope", encoding="utf-8")
            items = collect_local_import_items(str(root))
            names = sorted(name for name, _ in items)
            self.assertEqual(names, ["a.json", "b.json"])

    def test_execute_import_refreshes_when_enabled(self) -> None:
        raw_payload = {
            "type": "xai",
            "access_token": "old",
            "refresh_token": "rt",
            "email": "a@x.ai",
            "expired": "2020-01-01T00:00:00Z",
        }
        raw = json.dumps(raw_payload).encode("utf-8")
        item = ImportPreviewItem(
            source_name="xai.json",
            target_name="xai.json",
            provider="xai",
            planned_action=ACTION_IMPORT,
            available_actions=(ACTION_IMPORT, ACTION_SKIP),
            raw_payload=raw_payload,
            raw_content=raw,
        )
        client = MagicMock()
        del client.base_url
        refreshed = {
            **raw_payload,
            "access_token": "new-access",
            "refresh_token": "new-rt",
            "expired": "2099-01-01T00:00:00Z",
            "last_refresh": "2026-07-12T00:00:00Z",
        }
        with patch(
            "cpa_inspector.services.import_export.refresh_credential_payload",
            return_value=(refreshed, "已刷新 token"),
        ) as mocked:
            results = execute_import(
                client,
                [item],
                existing=[],
                max_workers=1,
                refresh_tokens=True,
                refresh_timeout_seconds=12,
            )
        self.assertEqual(results[0].result, "成功")
        self.assertIn("已刷新", results[0].detail)
        mocked.assert_called_once()
        uploaded = client.upload_credential.call_args.args
        self.assertEqual(uploaded[0], "xai.json")
        uploaded_payload = json.loads(uploaded[1].decode("utf-8"))
        self.assertEqual(uploaded_payload["access_token"], "new-access")

    def test_execute_import_refresh_failure_still_uploads_original(self) -> None:
        from cpa_inspector.services.token_refresh import TokenRefreshError

        raw_payload = {
            "type": "xai",
            "access_token": "old",
            "refresh_token": "rt",
            "email": "a@x.ai",
            "expired": "2020-01-01T00:00:00Z",
        }
        raw = json.dumps(raw_payload).encode("utf-8")
        item = ImportPreviewItem(
            source_name="xai.json",
            target_name="xai.json",
            provider="xai",
            planned_action=ACTION_IMPORT,
            available_actions=(ACTION_IMPORT, ACTION_SKIP),
            raw_payload=raw_payload,
            raw_content=raw,
        )
        client = MagicMock()
        del client.base_url
        with patch(
            "cpa_inspector.services.import_export.refresh_credential_payload",
            side_effect=TokenRefreshError("invalid_grant"),
        ):
            results = execute_import(
                client,
                [item],
                existing=[],
                max_workers=1,
                refresh_tokens=True,
            )
        self.assertEqual(results[0].result, "成功")
        self.assertIn("刷新失败", results[0].detail)
        uploaded = client.upload_credential.call_args.args[1]
        self.assertEqual(uploaded, raw)


if __name__ == "__main__":
    unittest.main()
