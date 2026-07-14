from __future__ import annotations

import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import MagicMock

from cpa_inspector.models import CredentialRecord
from cpa_inspector.services.import_export import export_credentials_to_zip


def _credential(
    name: str,
    *,
    email: str = "user@example.com",
    runtime_only: bool = False,
    source: str = "file",
) -> CredentialRecord:
    return CredentialRecord.from_api_payload(
        {
            "name": name,
            "provider": "codex",
            "status": "active",
            "disabled": False,
            "unavailable": False,
            "runtime_only": runtime_only,
            "source": source,
            "email": email,
        }
    )


class ExportDeleteTest(unittest.TestCase):
    def test_export_zip_and_delete_only_after_download_success(self) -> None:
        ok = _credential("ok.json", email="ok@example.com")
        bad = _credential("bad.json", email="bad@example.com")
        client = MagicMock()
        # Reuse the same mock client in worker path (no base_url/secret_key rebuild).
        del client.base_url

        def download(name: str) -> bytes:
            if name == "bad.json":
                raise RuntimeError("download failed")
            return b'{"ok":true}'

        client.download_credential.side_effect = download
        with tempfile.TemporaryDirectory() as tmp:
            zip_path = str(Path(tmp) / "out.zip")
            results = export_credentials_to_zip(
                client,
                [ok, bad],
                zip_path,
                delete_after_export=True,
                max_workers=2,
            )
            by_name = {item.name: item for item in results}
            self.assertEqual(by_name["ok.json"].result, "成功")
            self.assertEqual(by_name["bad.json"].result, "失败")
            client.delete_credential.assert_called_once_with("ok.json")
            with zipfile.ZipFile(zip_path) as zf:
                self.assertEqual(zf.namelist(), ["ok.json"])

    def test_non_exportable_is_skipped_without_download_or_delete(self) -> None:
        skipped = _credential("memory-item", runtime_only=True, source="memory")
        client = MagicMock()
        del client.base_url

        with tempfile.TemporaryDirectory() as tmp:
            zip_path = str(Path(tmp) / "out.zip")
            results = export_credentials_to_zip(
                client,
                [skipped],
                zip_path,
                delete_after_export=True,
                max_workers=1,
            )

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].name, "memory-item")
        self.assertEqual(results[0].result, "跳过")
        client.download_credential.assert_not_called()
        client.delete_credential.assert_not_called()


if __name__ == "__main__":
    unittest.main()
