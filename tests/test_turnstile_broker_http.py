# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
import unittest
from unittest import mock

import xai_http_flow as flow
from turnstile_broker import SolveRequest, build_canonical_fingerprint_profile


class _Resp:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.text = text or str(payload)

    def json(self):
        return self._payload


class BrokerHttpTests(unittest.TestCase):
    def test_loopback_uses_stdlib_requests_not_curl_cffi(self):
        calls = {"std": 0, "curl": 0}

        def std_post(url, json=None, timeout=None):
            calls["std"] += 1
            return _Resp(200, {"ok": True, "token": "t" * 100, "user_agent": "ua"})

        def curl_post(*_a, **_k):
            calls["curl"] += 1
            raise AssertionError("curl_cffi should not be used for loopback broker")

        with mock.patch("requests.post", side_effect=std_post), mock.patch.object(
            flow.requests, "post", side_effect=curl_post
        ):
            resp = flow._http_post_json(
                "http://127.0.0.1:8010/v1/solve",
                json_body={"provider": "local"},
                timeout=5,
                impersonate="chrome136",
            )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(calls["std"], 1)
        self.assertEqual(calls["curl"], 0)

    def test_broker_422_is_raised_not_fingerprint_mismatch(self):
        fp = build_canonical_fingerprint_profile()
        request = SolveRequest(
            provider="local",
            sitekey="sk",
            page_url="https://accounts.x.ai/sign-up",
            fingerprint=fp,
            broker_url="http://127.0.0.1:8010",
            timeout_sec=30,
        )

        def fake_post(url, json_body=None, timeout=None, impersonate=""):
            return _Resp(
                422,
                {
                    "detail": [
                        {"type": "missing", "loc": ["body"], "msg": "Field required", "input": None}
                    ]
                },
            )

        with mock.patch.object(flow, "_http_post_json", side_effect=fake_post):
            with self.assertRaises(flow.VerificationRequiredError) as ctx:
                asyncio.run(flow._solve_request_async(request, asyncio.sleep))
        msg = str(ctx.exception)
        self.assertIn("HTTP 422", msg)
        self.assertNotIn("UA 与 HTTP 会话指纹不一致", msg)

    def test_expected_browser_major_sent_as_int(self):
        fp = build_canonical_fingerprint_profile()
        request = SolveRequest(
            provider="local",
            sitekey="sk",
            page_url="https://accounts.x.ai/sign-up",
            fingerprint=fp,
            broker_url="http://127.0.0.1:8010",
            timeout_sec=30,
            headless=True,
        )
        captured = {}

        def fake_post(url, json_body=None, timeout=None, impersonate=""):
            captured["body"] = dict(json_body or {})
            return _Resp(
                200,
                {
                    "ok": True,
                    "token": "",
                    "user_agent": fp.user_agent,
                    "fingerprint": {
                        "navigator_language": "zh-CN",
                        "platform": fp.navigator_platform,
                        "client_hint_platform": fp.client_hint_platform,
                        "browser_major": fp.browser_major,
                    },
                    "lease": {
                        "lease_id": "lease-1",
                        "token_length": 120,
                    },
                },
            )

        with mock.patch.object(flow, "_http_post_json", side_effect=fake_post):
            result = asyncio.run(flow._solve_request_async(request, asyncio.sleep))
        self.assertIsInstance(captured["body"]["expected_browser_major"], int)
        self.assertEqual(captured["body"]["expected_browser_major"], int(fp.browser_major))
        self.assertNotIn("metadata", captured["body"])
        self.assertEqual(result.extras.get("lease_id"), "lease-1")


if __name__ == "__main__":
    unittest.main()
