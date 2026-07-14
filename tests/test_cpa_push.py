# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import cpa_push


class _FakeCpaHandler(BaseHTTPRequestHandler):
    files: list = []
    requests: list = []

    def log_message(self, format, *args):  # noqa: A003
        return

    def _auth_ok(self) -> bool:
        auth = self.headers.get("Authorization", "")
        return auth == "Bearer cpa-secret"

    def do_GET(self):  # noqa: N802
        parsed = urlparse(self.path)
        _FakeCpaHandler.requests.append(("GET", parsed.path, dict(self.headers)))
        if parsed.path != "/v0/management/auth-files":
            self.send_response(404)
            self.end_headers()
            return
        if not self._auth_ok():
            self.send_response(401)
            self.end_headers()
            self.wfile.write(b'{"error":"unauthorized"}')
            return
        body = json.dumps({"files": list(_FakeCpaHandler.files)}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):  # noqa: N802
        parsed = urlparse(self.path)
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        qs = parse_qs(parsed.query or "")
        name = (qs.get("name") or [""])[0]
        _FakeCpaHandler.requests.append(("POST", parsed.path, name, raw.decode("utf-8", errors="replace")))
        if parsed.path != "/v0/management/auth-files":
            self.send_response(404)
            self.end_headers()
            return
        if not self._auth_ok():
            self.send_response(401)
            self.end_headers()
            self.wfile.write(b'{"error":"unauthorized"}')
            return
        try:
            payload = json.loads(raw.decode("utf-8"))
        except Exception:
            payload = {}
        _FakeCpaHandler.files.append(
            {
                "name": name,
                "provider": str(payload.get("type") or ""),
                "email": str(payload.get("email") or ""),
                "account_id": str(payload.get("sub") or payload.get("account_id") or ""),
                "disabled": False,
            }
        )
        body = b'{"ok":true}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _sample_payload(email: str = "a@example.com") -> dict:
    return {
        "type": "xai",
        "email": email,
        "access_token": "access-token-value",
        "refresh_token": "refresh-token-value",
        "id_token": "id-token-value",
        "expired": "2099-01-01T00:00:00Z",
        "sub": "sub-123",
    }


class CpaPushTests(unittest.TestCase):
    def setUp(self) -> None:
        _FakeCpaHandler.files = []
        _FakeCpaHandler.requests = []
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeCpaHandler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.port}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def test_normalize_strips_management_path(self):
        self.assertEqual(
            cpa_push.normalize_cpa_base_url("http://x/v0/management/auth-files"),
            "http://x",
        )

    def test_check_connection(self):
        _FakeCpaHandler.files = [
            {"name": "a.json", "provider": "xai", "email": "a@x.com", "disabled": False},
            {"name": "b.json", "provider": "xai", "email": "b@x.com", "disabled": True},
        ]
        result = cpa_push.check_cpa_connection(self.base, "cpa-secret")
        self.assertTrue(result["ok"])
        self.assertEqual(result["total"], 2)
        self.assertEqual(result["active"], 1)
        self.assertEqual(result["disabled"], 1)

    def test_push_local_credentials(self):
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            payload = _sample_payload("push@example.com")
            path = root / "xai-push@example.com.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            result = cpa_push.push_local_credentials(
                base_url=self.base,
                api_key="cpa-secret",
                output_dir=root,
                use_local_name=True,
            )
            self.assertEqual(result["success"], 1)
            self.assertEqual(result["failed"], 0)
            posts = [r for r in _FakeCpaHandler.requests if r[0] == "POST"]
            self.assertEqual(len(posts), 1)
            self.assertEqual(posts[0][2], "xai-push@example.com.json")

    def test_skip_duplicate_name(self):
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            payload = _sample_payload("dup@example.com")
            name = "xai-dup@example.com.json"
            (root / name).write_text(json.dumps(payload), encoding="utf-8")
            _FakeCpaHandler.files = [
                {"name": name, "provider": "xai", "email": "dup@example.com", "disabled": False}
            ]
            result = cpa_push.push_local_credentials(
                base_url=self.base,
                api_key="cpa-secret",
                output_dir=root,
                use_local_name=True,
                skip_duplicates=True,
            )
            self.assertEqual(result["success"], 0)
            self.assertEqual(result["skipped"], 1)

    def test_validate_incomplete(self):
        msg = cpa_push.validate_cpa_payload({"type": "xai", "email": "x"})
        self.assertIn("缺少字段", msg)

    def test_batch_service_methods(self):
        import tempfile

        import http_batch_service as svc

        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            cfg = root / "config.json"
            cfg.write_text(
                json.dumps(
                    {
                        "cpa_api_url": self.base,
                        "cpa_api_key": "cpa-secret",
                        "cpa_use_local_name": True,
                        "xai_oauth_output_dir": str(root / "out"),
                    }
                ),
                encoding="utf-8",
            )
            out = root / "out"
            out.mkdir()
            payload = _sample_payload("svc@example.com")
            (out / "xai-svc@example.com.json").write_text(json.dumps(payload), encoding="utf-8")
            service = svc.BatchService(config_path=cfg, root_dir=root)
            check = service.check_cpa_connection()
            self.assertTrue(check["ok"])
            pushed = service.push_cpa_credentials()
            self.assertEqual(pushed["success"], 1)

    def test_webui_routes(self):
        import tempfile

        import http_batch_service as svc
        import webui_app
        from fastapi.testclient import TestClient

        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            cfg = root / "config.json"
            out = root / "out"
            out.mkdir()
            cfg.write_text(
                json.dumps(
                    {
                        "cpa_api_url": self.base,
                        "cpa_api_key": "cpa-secret",
                        "cpa_use_local_name": True,
                        "xai_oauth_output_dir": str(out),
                        "email_provider": "yyds",
                        "yyds_api_key": "k",
                        "turnstile_provider": "capsolver",
                        "turnstile_api_key": "t",
                    }
                ),
                encoding="utf-8",
            )
            service = svc.BatchService(config_path=cfg, root_dir=root)
            app = webui_app.create_app(service=service)
            client = TestClient(app)
            r = client.post("/api/cpa-push/test", json={})
            self.assertEqual(r.status_code, 200, r.text)
            self.assertTrue(r.json()["ok"])
            # config center exposes fields
            center = client.get("/api/config-center")
            self.assertEqual(center.status_code, 200)
            fields = center.json().get("fields") or {}
            self.assertIn("cpa_api_url", fields)
            self.assertIn("cpa_auto_upload", fields)
            # page contains panel
            page = client.get("/config/output")
            self.assertEqual(page.status_code, 200)
            self.assertIn("cpaPushPanel", page.text)
            self.assertIn("测试推送连接", page.text)

    def test_save_credential_file_logs_cpa_push(self):
        import tempfile
        from unittest.mock import patch

        import xai_oauth

        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            cfg_path = root / "config.json"
            cfg_path.write_text(
                json.dumps(
                    {
                        "cpa_auto_upload": True,
                        "cpa_api_url": self.base,
                        "cpa_api_key": "cpa-secret",
                        "cpa_use_local_name": True,
                        "cpa_skip_duplicates": True,
                    }
                ),
                encoding="utf-8",
            )
            logs: list[str] = []
            doc = _sample_payload("auto@example.com")
            with patch.object(xai_oauth, "__file__", str(root / "xai_oauth.py")):
                path = xai_oauth.save_credential_file(
                    doc,
                    str(root / "out"),
                    log_callback=logs.append,
                )
            self.assertTrue(Path(path).is_file())
            joined = "\n".join(logs)
            self.assertIn("[CPA]", joined)
            self.assertTrue(
                any("推送成功" in line or "CPA 自动推送成功" in line for line in logs),
                joined,
            )
            posts = [r for r in _FakeCpaHandler.requests if r[0] == "POST"]
            self.assertEqual(len(posts), 1)


if __name__ == "__main__":
    unittest.main()
