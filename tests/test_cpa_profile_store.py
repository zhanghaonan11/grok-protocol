from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from cpa_inspector.models import AppSettings, ConnectionProfile
from cpa_inspector.services.profile_store import ProfileStore


class ProfileStoreTest(unittest.TestCase):
    def test_roundtrip_profiles_and_settings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = ProfileStore(
                config_path=root / "profiles.json",
                settings_path=root / "settings.json",
            )
            profiles = [
                ConnectionProfile(
                    name="local",
                    base_url="http://127.0.0.1:8317",
                    secret_key="secret",
                    last_used_at="2026-07-12T00:00:00Z",
                )
            ]
            store.save_profiles(profiles)
            store.save_app_settings(AppSettings(max_parallel_workers=8, page_size=20))

            loaded_profiles = store.load_profiles()
            loaded_settings = store.load_app_settings()
            self.assertEqual(len(loaded_profiles), 1)
            self.assertEqual(loaded_profiles[0].base_url, "http://127.0.0.1:8317")
            self.assertEqual(loaded_settings.max_parallel_workers, 8)
            self.assertEqual(loaded_settings.page_size, 20)


if __name__ == "__main__":
    unittest.main()
