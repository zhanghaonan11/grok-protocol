import unittest

import xai_http_flow as flow


class SafeErrorTextTests(unittest.TestCase):
    def assert_secrets_absent(self, text, *secrets):
        for secret in secrets:
            self.assertNotIn(secret, text)

    def test_structured_values_are_cleaned_recursively(self):
        text = flow._safe_error_text(
            {
                "errorCode": "ERROR_INVALID_TOKEN",
                "status": "failed",
                "token": "dict-secret-value",
                "token_length": 123,
                "authorization": "Bearer structured-auth-secret-value",
                "nested": [
                    {"cookie": "cookie-secret-value", "detail": "upstream timeout"},
                    ({"clientSecret": "client-secret-value"},),
                ],
            }
        )

        self.assert_secrets_absent(
            text,
            "dict-secret-value",
            "cookie-secret-value",
            "client-secret-value",
            "structured-auth-secret-value",
        )
        self.assertIn("ERROR_INVALID_TOKEN", text)
        self.assertIn('"status": "failed"', text)
        self.assertIn('"token_length": 123', text)
        self.assertIn('"authorization": "Bearer <redacted>"', text)
        self.assertIn("upstream timeout", text)

    def test_json_and_python_repr_sensitive_values_are_cleaned(self):
        cases = [
            (
                '{"errorCode":"E_JSON","api_key":"json-secret-value","status":"processing"}',
                "json-secret-value",
                "E_JSON",
            ),
            (
                "{'errorCode': 'E_PYTHON', 'password': 'python-secret-value', "
                "'detail': 'provider timeout'}",
                "python-secret-value",
                "provider timeout",
            ),
            (
                'HTTP 400: {"cookie": "quoted-secret-value", "errorCode": "E_QUOTED"}',
                "quoted-secret-value",
                "E_QUOTED",
            ),
            (
                "request failed api_key=equals-secret-value status=processing",
                "equals-secret-value",
                "status=processing",
            ),
        ]
        for raw, secret, context in cases:
            with self.subTest(raw=raw):
                text = flow._safe_error_text(raw)
                self.assertNotIn(secret, text)
                self.assertIn(context, text)

    def test_auth_headers_and_proxy_userinfo_are_cleaned(self):
        raw = (
            "status=failed Authorization: Bearer bearer-secret-value-123 "
            "Proxy-Authorization=Basic dXNlcjpwYXNzd29yZA== "
            "proxy=http://proxy-user:proxy-password@[2001:db8::9]:8080 "
            "errorCode=E_PROVIDER"
        )
        text = flow._safe_error_text(raw)

        self.assert_secrets_absent(
            text,
            "bearer-secret-value-123",
            "dXNlcjpwYXNzd29yZA==",
            "proxy-user",
            "proxy-password",
        )
        self.assertIn("Bearer <redacted>", text)
        self.assertIn("Basic <redacted>", text)
        self.assertIn("http://[2001:db8::9]:8080", text)
        self.assertIn("status=failed", text)
        self.assertIn("errorCode=E_PROVIDER", text)

    def test_output_limit_is_preserved(self):
        text = flow._safe_error_text(
            {"status": "failed", "detail": "x" * 200, "token": "limit-secret-value"},
            limit=48,
        )
        self.assertLessEqual(len(text), 48)
        self.assertNotIn("limit-secret-value", text)


if __name__ == "__main__":
    unittest.main()
