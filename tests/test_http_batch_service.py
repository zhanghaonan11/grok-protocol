import json
import tempfile
import unittest
from pathlib import Path

import http_batch_service as svc


class HttpBatchServiceSmokeTests(unittest.TestCase):
    def test_build_plan_local_caps_workers(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            cfg = root / "config.json"
            cfg.write_text(
                json.dumps(
                    {
                        "email_provider": "yyds",
                        "yyds_api_key": "k",
                        "turnstile_provider": "local",
                        "turnstile_headless": True,
                        "register_count": 10,
                        "concurrent_workers": 10,
                    }
                ),
                encoding="utf-8",
            )
            settings = svc.Settings(
                config_path=cfg,
                count=10,
                workers=10,
                output_dir=root / "creds",
                run_mode=svc.RUN_MODE_REGISTER_SSO,
                turnstile_provider="local",
                turnstile_headless=True,
                config=svc._read_config(cfg),
            )
            plan = svc.build_plan(settings)
            self.assertLessEqual(plan.workers, svc.MAX_LOCAL_TURNSTILE_WORKERS)


if __name__ == "__main__":
    unittest.main()
