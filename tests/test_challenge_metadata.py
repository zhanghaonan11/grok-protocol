import unittest

import xai_http_flow as flow


class ChallengeMetadataTests(unittest.TestCase):
    def test_camel_case_json_sitekey_is_returned_when_unique(self):
        metadata = flow.BrowserlessXAIClient.challenge_metadata(
            r'{"siteKey":"0x4AAAA-camel","action":"sign-up","cData":"opaque"}'
        )
        self.assertEqual(metadata["turnstile_sitekey"], "0x4AAAA-camel")
        self.assertEqual(metadata["turnstile_sitekey_conflict"], "")
        self.assertEqual(metadata["turnstile_action"], "sign-up")
        self.assertEqual(metadata["turnstile_cdata"], "opaque")

    def test_conflicting_json_sitekey_candidates_fail_closed(self):
        metadata = flow.BrowserlessXAIClient.challenge_metadata(
            r'{"sitekey":"0x-first"}{"siteKey":"0x-second"}'
        )
        self.assertEqual(metadata["turnstile_sitekey"], "")
        self.assertEqual(metadata["turnstile_sitekey_conflict"], "true")

    def test_explicit_html_sitekey_has_priority_over_serialized_copy(self):
        metadata = flow.BrowserlessXAIClient.challenge_metadata(
            '<div class="cf-turnstile" data-siteKey="0x-explicit"></div>'
            '<script>window.data={"sitekey":"0x-stale"}</script>'
        )
        self.assertEqual(metadata["turnstile_sitekey"], "0x-explicit")
        self.assertEqual(metadata["turnstile_sitekey_conflict"], "")


if __name__ == "__main__":
    unittest.main()
