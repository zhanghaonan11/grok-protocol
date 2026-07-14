from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from cpa_inspector.services.api_client import ApiError, ManagementApiClient
from cpa_inspector.services.parallel_jobs import run_ordered_parallel


class ApiClientParallelTest(unittest.TestCase):
    @patch("cpa_inspector.services.api_client.requests.Session.request")
    def test_fetch_credentials(self, mock_request: MagicMock) -> None:
        response = MagicMock()
        response.status_code = 200
        response.json.return_value = {
            "files": [
                {
                    "name": "a.json",
                    "provider": "codex",
                    "status": "active",
                    "disabled": False,
                    "unavailable": False,
                    "runtime_only": False,
                    "source": "file",
                    "email": "a@example.com",
                }
            ]
        }
        mock_request.return_value = response
        client = ManagementApiClient("http://127.0.0.1:8317", "secret")
        items = client.fetch_credentials()
        self.assertEqual(items[0].name, "a.json")

    @patch("cpa_inspector.services.api_client.requests.Session.request")
    def test_auth_error(self, mock_request: MagicMock) -> None:
        response = MagicMock()
        response.status_code = 401
        response.text = "nope"
        response.json.return_value = {"error": "unauthorized"}
        mock_request.return_value = response
        client = ManagementApiClient("http://127.0.0.1:8317", "bad")
        with self.assertRaises(ApiError):
            client.fetch_credentials()

    def test_run_ordered_parallel_preserves_order(self) -> None:
        result = run_ordered_parallel([3, 1, 2], lambda x: x * 10, max_workers=2)
        self.assertEqual(result, [30, 10, 20])


if __name__ == "__main__":
    unittest.main()
