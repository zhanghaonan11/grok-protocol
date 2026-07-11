from __future__ import annotations

import unittest
from pathlib import Path
import sys
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.browser_worker import BrowserWorker, prepare_browser_proxy, read_turnstile_token_from_page
from src.config import SolverConfig
from src.models import SolveRequest
from src.proxy import normalize_proxy, parse_proxy
from src.service import SolverService


class SolverScaffoldTests(unittest.TestCase):
    def test_normalize_proxy_host_port_user_pass(self):
        self.assertEqual(
            normalize_proxy("1.2.3.4:8080:user:pass"),
            "http://user:pass@1.2.3.4:8080",
        )

    def test_parse_proxy(self):
        spec = parse_proxy("http://u:p@127.0.0.1:7890")
        self.assertTrue(spec.enabled)
        self.assertEqual(spec.host, "127.0.0.1")
        self.assertEqual(spec.port, "7890")
        self.assertEqual(spec.username, "u")

    def test_prepare_browser_proxy_no_auth_direct(self):
        browser_proxy, upstream, key = prepare_browser_proxy("http://1.2.3.4:8080")
        self.assertEqual(browser_proxy, "http://1.2.3.4:8080")
        self.assertEqual(upstream, "http://1.2.3.4:8080")
        self.assertEqual(key, "")

    def test_prepare_browser_proxy_auth_uses_forwarder(self):
        with patch("local_proxy_forwarder.ensure_local_forwarder", return_value=("http://127.0.0.1:17999", True)) as mocked:
            browser_proxy, upstream, key = prepare_browser_proxy(
                "http://u:p@1.2.3.4:8080",
                instance_key="t1",
            )
        self.assertEqual(browser_proxy, "http://127.0.0.1:17999")
        self.assertEqual(upstream, "http://u:p@1.2.3.4:8080")
        self.assertEqual(key, "t1")
        mocked.assert_called_once()

    def test_read_token_from_page(self):
        class FakePage:
            def run_js(self, _script):
                return "x" * 90

        self.assertEqual(len(read_turnstile_token_from_page(FakePage())), 90)

    def test_solve_success_with_mocked_browser(self):
        worker = BrowserWorker(SolverConfig(max_concurrency=1, token_min_length=80))
        with patch.object(
            worker,
            "_capture_with_browser",
            return_value=("t" * 100, "UA-TEST", {"sitekey_count": 1}),
        ):
            result = worker.solve(SolveRequest(proxy="http://1.2.3.4:8080"))
        self.assertTrue(result.ok)
        self.assertEqual(result.token, "t" * 100)
        self.assertEqual(result.user_agent, "UA-TEST")
        self.assertEqual(result.proxy, "http://1.2.3.4:8080")

    def test_solve_timeout_style_failure(self):
        worker = BrowserWorker(SolverConfig(max_concurrency=1, token_min_length=80))
        with patch.object(
            worker,
            "_capture_with_browser",
            return_value=("short", "UA", {"token_len_max": 0}),
        ):
            result = worker.solve(SolveRequest(proxy="http://1.2.3.4:8080", timeout_sec=30))
        self.assertFalse(result.ok)
        self.assertIn("未捕获到可用 Turnstile token", result.error)

    def test_health_shape(self):
        service = SolverService(SolverConfig())
        health = service.health()
        self.assertTrue(health["ok"])
        self.assertIn("pool", health)


if __name__ == "__main__":
    unittest.main()
