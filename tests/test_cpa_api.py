from __future__ import annotations

import json
import tempfile
import time
import unittest
import zipfile
from io import BytesIO
from pathlib import Path
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from cpa_inspector.models import CredentialRecord
from cpa_inspector.services.profile_store import ProfileStore
from cpa_inspector.web.app import create_app


def _sample_credential(
    name: str = "a.json",
    *,
    email: str = "a@example.com",
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
            "access_token": "should-not-leak",
            "refresh_token": "also-secret",
        }
    )


class ApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.store = ProfileStore(
            config_path=root / "profiles.json",
            settings_path=root / "settings.json",
        )
        self.app = create_app(profile_store=self.store)
        self.client = TestClient(self.app)

    def _connect(self, items: list[CredentialRecord] | None = None) -> MagicMock:
        fake_items = items if items is not None else [_sample_credential()]
        factory = patch("cpa_inspector.web.routes.api.ManagementApiClient")
        self.addCleanup(factory.stop)
        mocked = factory.start()
        api = MagicMock()
        api.fetch_credentials.return_value = fake_items
        mocked.return_value = api
        resp = self.client.post(
            "/api/cpa/connect",
            json={
                "base_url": "http://127.0.0.1:8317",
                "secret_key": "secret",
                "name": "local",
            },
        )
        self.assertEqual(resp.status_code, 200)
        return api

    def test_settings_read_write(self) -> None:
        got = self.client.get("/api/cpa/settings")
        self.assertEqual(got.status_code, 200)
        body = got.json()
        self.assertEqual(body["max_parallel_workers"], 4)
        self.assertEqual(body["page_size"], 50)

        saved = self.client.put(
            "/api/cpa/settings",
            json={
                "max_parallel_workers": 8,
                "page_size": 20,
                "probe_model": "gpt-test",
                "probe_timeout_seconds": 9,
                "probe_max_workers": 12,
                "import_refresh_tokens": False,
                "import_refresh_timeout_seconds": 11,
                "auto_cleanup_scope": "filtered",
                "auto_cleanup_match": "failed_uncertain",
                "auto_cleanup_keyword": "invalid_grant,revoked",
                "auto_cleanup_interval_seconds": 5,
                "auto_cleanup_max_rounds": 3,
            },
        )
        self.assertEqual(saved.status_code, 200)
        self.assertEqual(saved.json()["max_parallel_workers"], 8)
        self.assertEqual(saved.json()["page_size"], 20)
        self.assertEqual(saved.json()["probe_max_workers"], 12)
        self.assertFalse(saved.json()["import_refresh_tokens"])
        self.assertEqual(saved.json()["import_refresh_timeout_seconds"], 11)
        self.assertEqual(saved.json()["auto_cleanup_scope"], "filtered")
        self.assertEqual(saved.json()["auto_cleanup_match"], "failed_uncertain")
        self.assertEqual(saved.json()["auto_cleanup_keyword"], "invalid_grant,revoked")
        self.assertEqual(saved.json()["auto_cleanup_interval_seconds"], 5)
        self.assertEqual(saved.json()["auto_cleanup_max_rounds"], 3)

        again = self.client.get("/api/cpa/settings")
        self.assertEqual(again.json()["probe_model"], "gpt-test")
        self.assertEqual(again.json()["probe_timeout_seconds"], 9)
        self.assertEqual(again.json()["probe_max_workers"], 12)
        self.assertFalse(again.json()["import_refresh_tokens"])
        self.assertEqual(again.json()["import_refresh_timeout_seconds"], 11)
        self.assertEqual(again.json()["auto_cleanup_scope"], "filtered")
        self.assertEqual(again.json()["auto_cleanup_keyword"], "invalid_grant,revoked")

    def test_list_without_connect_returns_400(self) -> None:
        resp = self.client.get("/api/cpa/credentials")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("连接", resp.json()["detail"])

    def test_connect_and_list(self) -> None:
        fake_items = [_sample_credential()]
        with patch("cpa_inspector.web.routes.api.ManagementApiClient") as factory:
            api = MagicMock()
            api.fetch_credentials.return_value = fake_items
            factory.return_value = api
            resp = self.client.post(
                "/api/cpa/connect",
                json={
                    "base_url": "http://127.0.0.1:8317",
                    "secret_key": "secret",
                    "name": "local",
                },
            )
            self.assertEqual(resp.status_code, 200)
            payload = resp.json()
            self.assertTrue(payload["connected"])
            self.assertEqual(payload["total"], 1)

            listed = self.client.get("/api/cpa/credentials?page=1&page_size=50")
            self.assertEqual(listed.status_code, 200)
            body = listed.json()
            self.assertEqual(body["total"], 1)
            self.assertEqual(body["items"][0]["name"], "a.json")
            self.assertEqual(body["items"][0]["email_masked"], "a*@example.com")
            self.assertNotIn("access_token", body["items"][0])
            self.assertNotIn("raw", body["items"][0])

    def test_credential_detail_is_desensitized(self) -> None:
        with patch("cpa_inspector.web.routes.api.ManagementApiClient") as factory:
            api = MagicMock()
            api.fetch_credentials.return_value = [
                _sample_credential(email="alice@example.com")
            ]
            factory.return_value = api
            self.client.post(
                "/api/cpa/connect",
                json={
                    "base_url": "http://127.0.0.1:8317",
                    "secret_key": "secret",
                    "name": "local",
                },
            )
        detail = self.client.get("/api/cpa/credentials/detail", params={"name": "a.json"})
        self.assertEqual(detail.status_code, 200)
        body = detail.json()
        self.assertEqual(body["name"], "a.json")
        self.assertEqual(body["email_masked"], "al***@example.com")
        self.assertNotIn("access_token", body)
        self.assertNotIn("refresh_token", body)
        self.assertNotIn("raw", body)
        self.assertNotIn("email", body)

    def test_health_check_creates_job_and_pollable(self) -> None:
        item = _sample_credential()
        with patch("cpa_inspector.web.routes.api.ManagementApiClient") as factory:
            api = MagicMock()
            api.fetch_credentials.return_value = [item]
            factory.return_value = api
            self.client.post(
                "/api/cpa/connect",
                json={
                    "base_url": "http://127.0.0.1:8317",
                    "secret_key": "secret",
                    "name": "local",
                },
            )

        with patch("cpa_inspector.web.routes.api.probe_credentials") as probe:
            from cpa_inspector.models import JobResult

            def _fake_probe(client, credentials, **kwargs):
                results = [
                    JobResult(name=c.name, result="成功", detail="HTTP 200")
                    for c in credentials
                ]
                callback = kwargs.get("progress_callback")
                if callback:
                    callback(1, 1, "done")
                return results

            probe.side_effect = _fake_probe
            started = self.client.post(
                "/api/cpa/health-check",
                json={"names": ["a.json"]},
            )
            self.assertEqual(started.status_code, 200)
            job_id = started.json()["job_id"]
            self.assertTrue(job_id)

            deadline = time.time() + 2
            status = None
            while time.time() < deadline:
                polled = self.client.get(f"/api/cpa/jobs/{job_id}")
                self.assertEqual(polled.status_code, 200)
                status = polled.json()
                if status["status"] in ("success", "failed"):
                    break
                time.sleep(0.05)

            self.assertIsNotNone(status)
            assert status is not None
            self.assertEqual(status["status"], "success")
            self.assertEqual(status["total"], 1)
            self.assertEqual(status["results"][0]["name"], "a.json")

    def test_export_returns_zip_with_summary_headers(self) -> None:
        item = _sample_credential()
        with patch("cpa_inspector.web.routes.api.ManagementApiClient") as factory:
            api = MagicMock()
            api.fetch_credentials.return_value = [item]
            api.download_credential.return_value = b'{"type":"codex"}'
            factory.return_value = api
            self.client.post(
                "/api/cpa/connect",
                json={
                    "base_url": "http://127.0.0.1:8317",
                    "secret_key": "secret",
                    "name": "local",
                },
            )
            exported = self.client.post(
                "/api/cpa/export",
                json={"names": ["a.json"]},
            )
        self.assertEqual(exported.status_code, 200)
        self.assertEqual(
            exported.headers.get("content-type", "").split(";")[0],
            "application/zip",
        )
        self.assertEqual(exported.headers.get("X-Export-Success"), "1")
        self.assertEqual(exported.headers.get("X-Export-Failed"), "0")
        self.assertEqual(exported.headers.get("X-Export-Skipped"), "0")
        with zipfile.ZipFile(BytesIO(exported.content)) as zf:
            self.assertEqual(zf.namelist(), ["a.json"])

    def test_profiles_roundtrip(self) -> None:
        payload = [
            {
                "name": "local",
                "base_url": "http://127.0.0.1:8317",
                "secret_key": "secret",
                "last_used_at": "",
            }
        ]
        saved = self.client.put("/api/cpa/profiles", json={"profiles": payload})
        self.assertEqual(saved.status_code, 200)
        self.assertEqual(len(saved.json()["profiles"]), 1)

        listed = self.client.get("/api/cpa/profiles")
        self.assertEqual(listed.status_code, 200)
        self.assertEqual(listed.json()["profiles"][0]["name"], "local")
        self.assertEqual(
            listed.json()["profiles"][0]["base_url"],
            "http://127.0.0.1:8317",
        )

    def test_import_preview_text_xai_json_with_sso(self) -> None:
        self._connect([])
        payload = {
            "type": "xai",
            "access_token": "eyJhbGciOiJIUzI1NiJ9.e30.sig",
            "refresh_token": "rt",
            "id_token": "eyJhbGciOiJIUzI1NiJ9.e30.id",
            "email": "xaixxemfpxalt@uivm.top",
            "expired": "2099-07-12T15:43:20Z",
            "sub": "e9bdd96d-5ae2-4402-a8d3-4424de380ddd",
            "base_url": "https://api.x.ai/v1",
            "auth_kind": "oauth",
        }
        sso = (
            "eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9."
            "eyJzZXNzaW9uX2lkIjoiMjY4NWQ3OTAtMTIyMC00YzNmLTk0MTYtOWQ1ZDY5NzNkZjM5In0."
            "2IoxNUCRF9z5IY8zhsimRUIrm4s7aOve2vKWVuLIPzE"
        )
        text = json.dumps(payload, ensure_ascii=False) + "____" + sso
        resp = self.client.post("/api/cpa/import/preview-text", json={"text": text})
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["total"], 1)
        item = body["items"][0]
        self.assertTrue(item["valid"])
        self.assertEqual(item["provider"], "xai")
        self.assertEqual(item["source_name"], "xai-xaixxemfpxalt@uivm.top.json")
        self.assertEqual(item["planned_action"], "import")
        self.assertNotIn("access_token", item)
        self.assertNotIn("raw_payload", item)

    def test_import_preview_text_empty_rejected(self) -> None:
        self._connect([])
        resp = self.client.post("/api/cpa/import/preview-text", json={"text": "   "})
        self.assertEqual(resp.status_code, 400)

    def test_import_preview_path_reads_local_file(self) -> None:
        self._connect([])
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "xai-local.json"
            path.write_text(
                json.dumps(
                    {
                        "type": "xai",
                        "access_token": "a.b.c",
                        "refresh_token": "rt",
                        "email": "path@x.ai",
                        "expired": "2099-01-01T00:00:00Z",
                    }
                ),
                encoding="utf-8",
            )
            resp = self.client.post(
                "/api/cpa/import/preview-path",
                json={"path": str(path)},
            )
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["total"], 1)
        item = body["items"][0]
        self.assertTrue(item["valid"])
        self.assertEqual(item["provider"], "xai")
        self.assertEqual(item["source_name"], "xai-local.json")
        self.assertNotIn("access_token", item)

    def test_delete_credentials_creates_job(self) -> None:
        item = _sample_credential()
        with patch("cpa_inspector.web.routes.api.ManagementApiClient") as factory:
            api = MagicMock()
            api.fetch_credentials.return_value = [item]
            api.delete_credential.return_value = None
            factory.return_value = api
            self.client.post(
                "/api/cpa/connect",
                json={
                    "base_url": "http://127.0.0.1:8317",
                    "secret_key": "secret",
                    "name": "local",
                },
            )
            started = self.client.post(
                "/api/cpa/credentials/delete",
                json={"names": ["a.json"], "max_workers": 1},
            )
            self.assertEqual(started.status_code, 200)
            job_id = started.json()["job_id"]
            deadline = time.time() + 2
            status = None
            while time.time() < deadline:
                polled = self.client.get(f"/api/cpa/jobs/{job_id}")
                status = polled.json()
                if status["status"] in ("success", "failed"):
                    break
                time.sleep(0.05)
            self.assertIsNotNone(status)
            assert status is not None
            self.assertEqual(status["status"], "success")
            self.assertEqual(status["results"][0]["name"], "a.json")
            self.assertEqual(status["results"][0]["result"], "成功")


if __name__ == "__main__":
    unittest.main()
