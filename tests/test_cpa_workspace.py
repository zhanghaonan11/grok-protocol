from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from cpa_inspector.models import AppSettings, CredentialRecord
from cpa_inspector.services.profile_store import ProfileStore
from cpa_inspector.services.workspace import WorkspaceService
from cpa_inspector.state import AppState


class WorkspaceTest(unittest.TestCase):
    def test_connect_and_list(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = ProfileStore(
                config_path=Path(tmp) / "profiles.json",
                settings_path=Path(tmp) / "settings.json",
            )
            state = AppState(settings=AppSettings(page_size=20))
            client_factory = MagicMock()
            client = MagicMock()
            client.fetch_credentials.return_value = [
                CredentialRecord.from_api_payload(
                    {
                        "name": "b.json",
                        "provider": "codex",
                        "status": "active",
                        "disabled": False,
                        "unavailable": False,
                        "runtime_only": False,
                        "source": "file",
                        "email": "b@example.com",
                    }
                ),
                CredentialRecord.from_api_payload(
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
                ),
            ]
            client_factory.return_value = client
            ws = WorkspaceService(state=state, store=store, client_factory=client_factory)
            items = ws.connect("http://127.0.0.1:8317", "secret")
            self.assertEqual([i.name for i in items], ["a.json", "b.json"])
            page = ws.list_credentials(page=1, page_size=1)
            self.assertEqual(page["items"][0].name, "a.json")
            self.assertEqual(page["total"], 2)
            self.assertTrue(state.connected)
            self.assertEqual(len(store.load_profiles()), 1)
