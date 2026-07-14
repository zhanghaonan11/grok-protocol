from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from cpa_inspector.services.profile_store import ProfileStore
from cpa_inspector.web.app import create_app


class PagesTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        store = ProfileStore(
            config_path=root / "profiles.json",
            settings_path=root / "settings.json",
        )
        self.client = TestClient(create_app(profile_store=store))

    def test_index_contains_workbench_sections(self) -> None:
        resp = self.client.get("/cpa")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("text/html", resp.headers.get("content-type", ""))
        html = resp.text
        for needle in (
            "CPA Web 巡检台",
            'id="connection-status"',
            'id="base-url"',
            'id="secret-key"',
            'id="setting-probe-model"',
            'id="setting-probe-timeout"',
            'id="setting-import-refresh"',
            'id="setting-import-refresh-timeout"',
            'id="btn-connect"',
            'id="btn-refresh"',
            'id="btn-import"',
            'id="btn-import-path"',
            'id="btn-import-paste"',
            'id="btn-export"',
            'id="btn-export-delete"',
            'id="btn-health"',
            'id="btn-health-page"',
            'id="btn-health-filtered"',
            'id="btn-health-all"',
            'id="probe-workers-once"',
            'id="setting-probe-workers"',
            'id="filter-search"',
            'id="filter-status"',
            'id="filter-provider"',
            'id="filter-exportable"',
            'id="filter-health"',
            'id="filter-page-size"',
            'id="credentials-table"',
            'id="pagination"',
            'id="detail-panel"',
            'id="job-panel"',
            'id="btn-open-job-workbench"',
            'id="btn-open-job-workbench-top"',
            'id="btn-open-job-workbench-2"',
            'id="job-workbench-modal"',
            'id="job-filter-text"',
            'id="btn-job-delete-selected"',
            'id="auto-cleanup-scope"',
            'id="auto-cleanup-keyword"',
            'id="btn-auto-cleanup-start"',
            'id="btn-auto-cleanup-stop"',
            'id="import-modal"',
            'id="paste-import-modal"',
            'id="paste-import-text"',
            'id="path-import-modal"',
            'id="path-import-input"',
            'id="path-dropzone"',
            "/static/cpa/app.css",
            "/static/cpa/app.js",
            'class="main-nav"',
            'href="/"',
            'href="/config"',
            'href="/credentials"',
            'href="/cpa"',
            'aria-current="page"',
        ):
            self.assertIn(needle, html)

    def test_static_assets_served(self) -> None:
        css = self.client.get("/static/cpa/app.css")
        js = self.client.get("/static/cpa/app.js")
        self.assertEqual(css.status_code, 200)
        self.assertEqual(js.status_code, 200)
        self.assertTrue("workbench" in css.text.lower() or ".app-shell" in css.text)
        self.assertIn("loadSettings", js.text)


if __name__ == "__main__":
    unittest.main()
