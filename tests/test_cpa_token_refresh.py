from __future__ import annotations

import unittest
from io import BytesIO
from unittest.mock import patch
from urllib.error import HTTPError

from cpa_inspector.services.token_refresh import (
    DEFAULT_XAI_CLIENT_ID,
    DEFAULT_XAI_TOKEN_ENDPOINT,
    TokenRefreshError,
    apply_refreshed_tokens,
    refresh_credential_payload,
    refresh_oauth_tokens,
    resolve_client_id,
    resolve_token_endpoint,
)


class TokenRefreshTest(unittest.TestCase):
    def test_resolve_xai_defaults(self) -> None:
        payload = {"type": "xai", "refresh_token": "rt"}
        self.assertEqual(resolve_token_endpoint(payload), DEFAULT_XAI_TOKEN_ENDPOINT)
        self.assertEqual(resolve_client_id(payload), DEFAULT_XAI_CLIENT_ID)

    def test_apply_refreshed_tokens_updates_fields(self) -> None:
        payload = {
            "type": "xai",
            "access_token": "old",
            "refresh_token": "old-rt",
            "email": "a@x.ai",
            "expired": "2020-01-01T00:00:00Z",
        }
        updated = apply_refreshed_tokens(
            payload,
            {
                "access_token": "new-access",
                "refresh_token": "new-rt",
                "expires_in": 3600,
                "token_type": "Bearer",
            },
        )
        self.assertEqual(updated["access_token"], "new-access")
        self.assertEqual(updated["refresh_token"], "new-rt")
        self.assertEqual(updated["expires_in"], 3600)
        self.assertNotEqual(updated["expired"], "2020-01-01T00:00:00Z")
        self.assertTrue(updated["last_refresh"])

    def test_refresh_credential_payload_without_refresh_token(self) -> None:
        payload = {"type": "xai", "access_token": "a"}
        updated, note = refresh_credential_payload(payload)
        self.assertEqual(updated["access_token"], "a")
        self.assertIn("无 refresh_token", note)

    def test_refresh_credential_payload_success(self) -> None:
        payload = {
            "type": "xai",
            "access_token": "old",
            "refresh_token": "rt",
            "token_endpoint": "https://auth.x.ai/oauth2/token",
            "email": "a@x.ai",
        }
        fake_resp = {
            "access_token": "new-access",
            "refresh_token": "new-rt",
            "expires_in": 1200,
            "token_type": "Bearer",
        }
        with patch(
            "cpa_inspector.services.token_refresh.refresh_oauth_tokens",
            return_value=fake_resp,
        ) as mocked:
            updated, note = refresh_credential_payload(payload, timeout_seconds=9)
        mocked.assert_called_once()
        kwargs = mocked.call_args.kwargs
        self.assertEqual(kwargs["token_endpoint"], "https://auth.x.ai/oauth2/token")
        self.assertEqual(kwargs["client_id"], DEFAULT_XAI_CLIENT_ID)
        self.assertEqual(kwargs["timeout_seconds"], 9)
        self.assertEqual(updated["access_token"], "new-access")
        self.assertEqual(updated["refresh_token"], "new-rt")
        self.assertIn("已刷新", note)

    def test_refresh_oauth_http_error(self) -> None:
        err = HTTPError(
            "https://auth.x.ai/oauth2/token",
            400,
            "bad",
            hdrs=None,
            fp=BytesIO(b'{"error":"invalid_grant"}'),
        )
        with patch("cpa_inspector.services.token_refresh.urlrequest.urlopen", side_effect=err):
            with self.assertRaises(TokenRefreshError) as ctx:
                refresh_oauth_tokens(
                    "rt",
                    token_endpoint=DEFAULT_XAI_TOKEN_ENDPOINT,
                    client_id=DEFAULT_XAI_CLIENT_ID,
                )
        self.assertIn("HTTP 400", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
