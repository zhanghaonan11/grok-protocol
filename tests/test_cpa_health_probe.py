from __future__ import annotations

import json
import unittest
from unittest.mock import MagicMock, patch

from cpa_inspector.models import CredentialRecord
from cpa_inspector.services.health_probe import classify_probe_response, probe_credentials
from cpa_inspector.services.token_refresh import TokenRefreshError


class HealthProbeTest(unittest.TestCase):
    def test_classify_healthy(self) -> None:
        self.assertEqual(classify_probe_response(200)[0], "healthy")

    def test_classify_spending_limit_is_uncertain(self) -> None:
        body = (
            '{"code":"personal-team-blocked:spending-limit",'
            '"error":"You have run out of credits or need a Grok subscription."}'
        )
        status, detail = classify_probe_response(403, body_text=body)
        self.assertEqual(status, "uncertain")
        self.assertIn("无额度", detail)
        self.assertIn("spending-limit", detail.casefold())

    def test_classify_invalid_credentials_is_failed(self) -> None:
        body = (
            "额度获取失败：401 Invalid or expired credentials "
            "(auth_kind=bearer, x_xai_token_auth=none, upstream=PermissionDenied, reason=no auth context)"
        )
        status, detail = classify_probe_response(401, body_text=body)
        self.assertEqual(status, "failed")
        self.assertIn("Invalid or expired credentials", detail)

    def test_classify_bad_credentials_is_failed(self) -> None:
        body = (
            '{"code":"unauthenticated:bad-credentials",'
            '"error":"The OAuth2 access token could not be validated."}'
        )
        status, detail = classify_probe_response(403, body_text=body)
        self.assertEqual(status, "failed")
        self.assertIn("凭证无效", detail)

    def test_probe_marks_no_quota_as_uncertain(self) -> None:
        item = CredentialRecord.from_api_payload(
            {
                "name": "xai-a.json",
                "provider": "xai",
                "status": "active",
                "disabled": False,
                "unavailable": False,
                "runtime_only": False,
                "source": "file",
                "email": "a@example.com",
                "auth_index": "auth-1",
            }
        )
        client = MagicMock()
        del client.base_url
        client.download_credential.return_value = json.dumps(
            {
                "type": "xai",
                "access_token": "old",
                "refresh_token": "rt",
                "email": "a@example.com",
            }
        ).encode("utf-8")
        client.management_api_call.return_value = {
            "status_code": 403,
            "header": {},
            "body": (
                '{"code":"personal-team-blocked:spending-limit",'
                '"error":"You have run out of credits or need a Grok subscription."}'
            ),
        }
        with patch(
            "cpa_inspector.services.health_probe.refresh_credential_payload",
            return_value=(
                {
                    "type": "xai",
                    "access_token": "new",
                    "refresh_token": "rt",
                    "email": "a@example.com",
                },
                "已刷新 token",
            ),
        ):
            results = probe_credentials(
                client,
                [item],
                model="gpt-5",
                timeout_seconds=15,
                max_workers=1,
                refresh_before_probe=True,
            )
        self.assertEqual(results[0].result, "不确定")
        self.assertEqual(item.health_status, "uncertain")
        self.assertIn("无额度", item.health_detail)
        self.assertIn("已刷新 token", item.health_detail)
        client.upload_credential.assert_called_once()

    def test_probe_refresh_revoked_is_failed(self) -> None:
        item = CredentialRecord.from_api_payload(
            {
                "name": "xai-b.json",
                "provider": "xai",
                "status": "active",
                "disabled": False,
                "unavailable": False,
                "runtime_only": False,
                "source": "file",
                "email": "b@example.com",
                "auth_index": "auth-2",
            }
        )
        client = MagicMock()
        del client.base_url
        client.download_credential.return_value = json.dumps(
            {
                "type": "xai",
                "access_token": "old",
                "refresh_token": "rt",
                "email": "b@example.com",
            }
        ).encode("utf-8")
        with patch(
            "cpa_inspector.services.health_probe.refresh_credential_payload",
            side_effect=TokenRefreshError(
                'HTTP 400: {"error":"invalid_grant","error_description":"Refresh token has been revoked"}'
            ),
        ):
            results = probe_credentials(
                client,
                [item],
                model="gpt-5",
                timeout_seconds=15,
                max_workers=1,
                refresh_before_probe=True,
            )
        self.assertEqual(results[0].result, "失败")
        self.assertEqual(item.health_status, "failed")
        self.assertIn("刷新失败", item.health_detail)
        client.management_api_call.assert_not_called()

    def test_probe_without_auth_index_fails(self) -> None:
        item = CredentialRecord.from_api_payload(
            {
                "name": "xai-c.json",
                "provider": "xai",
                "status": "active",
                "disabled": False,
                "unavailable": False,
                "runtime_only": False,
                "source": "file",
            }
        )
        client = MagicMock()
        del client.base_url
        results = probe_credentials(
            client,
            [item],
            model="gpt-5",
            timeout_seconds=15,
            max_workers=1,
            refresh_before_probe=False,
        )
        self.assertEqual(results[0].result, "失败")
        self.assertEqual(item.health_status, "failed")
        self.assertIn("auth_index", item.health_detail)


if __name__ == "__main__":
    unittest.main()
