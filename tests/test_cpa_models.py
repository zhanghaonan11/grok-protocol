from __future__ import annotations

import unittest

from cpa_inspector.models import AppSettings, CredentialRecord, mask_email


class ModelsTest(unittest.TestCase):
    def test_mask_email(self) -> None:
        self.assertEqual(mask_email("alice@example.com"), "al***@example.com")
        self.assertEqual(mask_email(""), "-")

    def test_credential_status_and_export(self) -> None:
        item = CredentialRecord.from_api_payload(
            {
                "name": "a.json",
                "provider": "codex",
                "status": "active",
                "disabled": False,
                "unavailable": False,
                "runtime_only": False,
                "source": "file",
                "email": "bob@example.com",
            }
        )
        self.assertEqual(item.status_display, "活跃")
        self.assertTrue(item.can_export)
        self.assertEqual(item.email_masked, "bo***@example.com")
        self.assertEqual(item.health_status, "untested")

    def test_credential_size_dirty_values_default_to_zero(self) -> None:
        base = {
            "name": "a.json",
            "provider": "codex",
            "status": "active",
            "disabled": False,
            "unavailable": False,
            "runtime_only": False,
            "source": "file",
        }
        for dirty in ("not-a-number", "", None, {}, []):
            with self.subTest(size=dirty):
                item = CredentialRecord.from_api_payload({**base, "size": dirty})
                self.assertEqual(item.size, 0)

    def test_app_settings_clamp_workers(self) -> None:
        settings = AppSettings(max_parallel_workers=99, page_size=50)
        self.assertEqual(settings.max_parallel_workers, 32)

    def test_app_settings_import_refresh_defaults(self) -> None:
        settings = AppSettings()
        self.assertTrue(settings.import_refresh_tokens)
        self.assertEqual(settings.import_refresh_timeout_seconds, 20)
        payload = settings.to_dict()
        self.assertIn("import_refresh_tokens", payload)
        loaded = AppSettings.from_dict(
            {
                "import_refresh_tokens": False,
                "import_refresh_timeout_seconds": 0,
            }
        )
        self.assertFalse(loaded.import_refresh_tokens)
        self.assertEqual(loaded.import_refresh_timeout_seconds, 1)

    def test_app_settings_probe_workers(self) -> None:
        default = AppSettings(max_parallel_workers=6)
        self.assertEqual(default.probe_max_workers, 0)
        self.assertEqual(default.effective_probe_workers, 6)

        custom = AppSettings(max_parallel_workers=6, probe_max_workers=12)
        self.assertEqual(custom.probe_max_workers, 12)
        self.assertEqual(custom.effective_probe_workers, 12)

        clamped = AppSettings(probe_max_workers=99)
        self.assertEqual(clamped.probe_max_workers, 32)


if __name__ == "__main__":
    unittest.main()
