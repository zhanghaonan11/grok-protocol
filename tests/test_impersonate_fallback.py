# -*- coding: utf-8 -*-
import unittest
from unittest import mock

import turnstile_broker as broker


class ImpersonateFallbackTests(unittest.TestCase):
    def test_major_148_does_not_select_unsupported_chrome148(self):
        # Even if Session construction appears to accept chrome148, Curl.impersonate may reject it.
        with mock.patch.object(broker, "_supported_curl_cffi_impersonate_names", return_value=["chrome142", "chrome136", "chrome"]), mock.patch.object(
            broker, "_impersonate_is_usable", side_effect=lambda name: name in {"chrome142", "chrome136", "chrome"}
        ):
            chosen = broker._impersonate_for_browser_major("148")
        self.assertEqual(chosen, "chrome142")
        self.assertNotEqual(chosen, "chrome148")

    def test_profile_for_148_uses_usable_impersonate(self):
        profile = broker.build_canonical_fingerprint_profile(browser_major=148)
        self.assertEqual(profile.browser_major, "148")
        self.assertNotEqual(profile.impersonate, "chrome148")
        self.assertTrue(str(profile.impersonate).startswith("chrome"))


if __name__ == "__main__":
    unittest.main()
