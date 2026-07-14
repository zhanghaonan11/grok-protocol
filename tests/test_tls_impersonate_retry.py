# -*- coding: utf-8 -*-
from __future__ import annotations

import unittest
from unittest import mock

import xai_http_flow as flow


class _BoomThenOkSession:
    def __init__(self):
        self.calls = 0
        self.headers = {}
        self.cookies = mock.Mock()
        self.cookies.get.return_value = None
        self.cookies.__iter__ = lambda self: iter([])

    def get(self, url, **kwargs):
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("Failed to perform, curl: (35) TLS connect error: OPENSSL_internal:invalid library (0)")
        return mock.Mock(status_code=200, text="ok", url=url)


class TlsImpersonateRetryTests(unittest.TestCase):
    def test_is_tls_transport_error(self):
        self.assertTrue(flow._is_tls_transport_error(RuntimeError("curl: (35) TLS connect error")))
        self.assertFalse(flow._is_tls_transport_error(RuntimeError("HTTP 403 forbidden")))

    def test_request_retries_with_next_impersonate_on_tls_error(self):
        client = mock.Mock()
        client.timeout = 5
        client.proxies = None
        client.log_callback = None
        client.fingerprint = mock.Mock(impersonate="chrome136", sec_ch_ua='""', client_hint_platform="Windows")
        client.user_agent = "UA"
        client.accept_language = "en"
        client.session = _BoomThenOkSession()

        rebuilt = []

        def _rebuild(c, name):
            rebuilt.append(name)
            # After rebuild, next get succeeds via same session counter path.
            c.session = client.session

        with mock.patch.object(flow, "_impersonate_fallback_chain", return_value=["chrome136", "chrome120", "chrome"]), mock.patch.object(
            flow, "_rebuild_session_with_impersonate", side_effect=_rebuild
        ):
            # Bind real method
            resp = flow.BrowserlessXAIClient._request(client, "get", "https://example.test/")
        self.assertEqual(getattr(resp, "status_code", None), 200)
        self.assertEqual(client.session.calls, 2)
        self.assertEqual(rebuilt, ["chrome120"])

    def test_non_tls_error_is_not_retried(self):
        client = mock.Mock()
        client.timeout = 5
        client.proxies = None
        client.log_callback = None
        client.fingerprint = mock.Mock(impersonate="chrome136")
        client.session = mock.Mock()
        client.session.get.side_effect = RuntimeError("HTTP 500 boom")
        with self.assertRaisesRegex(RuntimeError, "HTTP 500"):
            flow.BrowserlessXAIClient._request(client, "get", "https://example.test/")
        self.assertEqual(client.session.get.call_count, 1)


if __name__ == "__main__":
    unittest.main()
