# -*- coding: utf-8 -*-
"""Tests for GPT-style proxy pool rotator integration."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from proxy_pool import (
    ProxyRotator,
    configure_global_rotator,
    extract_country,
    load_proxy_lines,
    normalize_proxy_line,
    normalize_proxy_pool,
    pick_proxy,
    report_outcome,
    validate_proxy_line,
)


class ProxyNormalizeTests(unittest.TestCase):
    def test_host_port_user_pass(self):
        raw = "us.swiftproxy.net:7878:user_zone_JP:pass"
        self.assertEqual(
            normalize_proxy_line(raw),
            "http://user_zone_JP:pass@us.swiftproxy.net:7878",
        )

    def test_reject_null_host(self):
        raw = "null:10000:USER921375-zone-custom-region-US-session-1:secret"
        normalized, err = validate_proxy_line(raw)
        self.assertEqual(normalized, "")
        self.assertIn("无效代理主机", err)

    def test_reject_none_host(self):
        normalized, err = validate_proxy_line("none:10000:u:p")
        self.assertEqual(normalized, "")
        self.assertTrue(err)

    def test_normalize_pool_drops_invalid(self):
        pool = normalize_proxy_pool(
            [
                "null:10000:u:p",
                "us.swiftproxy.net:7878:user_zone_JP:pass",
                "us.swiftproxy.net:7878:user_zone_JP:pass",  # dup
            ]
        )
        self.assertEqual(len(pool), 1)
        self.assertIn("swiftproxy", pool[0])

    def test_load_proxy_lines_skips_null(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "proxies.txt"
            path.write_text(
                "\n".join(
                    [
                        "null:10000:u:p",
                        "# comment",
                        "gate.example.com:10000:user_zone_US:pwd",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            lines = load_proxy_lines(str(path))
        self.assertEqual(len(lines), 1)
        self.assertIn("gate.example.com", lines[0])

    def test_extract_country_zone_and_host(self):
        self.assertEqual(extract_country("http://u_zone_JP:p@h:1"), "JP")
        self.assertEqual(
            extract_country(
                "http://USER-zone-custom-region-US-session-1:p@host:10000"
            ),
            "US",
        )
        self.assertEqual(extract_country("http://u:p@us.swiftproxy.net:7878"), "US")


class ProxyRotatorTests(unittest.TestCase):
    def test_weighted_next_and_cooldown(self):
        with tempfile.TemporaryDirectory() as tmp:
            stats = os.path.join(tmp, "stats.log")
            pool = [
                "http://u_zone_JP:p@a.example:1000",
                "http://u_zone_US:p@b.example:1000",
            ]
            rot = ProxyRotator(pool, stats_file=stats)
            self.assertEqual(len(rot), 2)
            first = rot.next()
            self.assertIn(first, pool)
            rot.record_result(first, False, "boom")
            rot.mark_bad(first, cooldown_seconds=60)
            second = rot.next()
            self.assertNotEqual(second, first)
            # status exposes cooldown
            statuses = {row["proxy"]: row for row in rot.get_status()}
            self.assertTrue(any(row["status"] == "bad" for row in statuses.values()))
            countries = {row["country"] for row in rot.get_country_stats()}
            self.assertTrue({"JP", "US"} & countries)

    def test_configure_global_and_pick(self):
        pool = [
            "http://u_zone_JP:p@a.example:1000",
            "http://u_zone_US:p@b.example:1000",
        ]
        with tempfile.TemporaryDirectory() as tmp:
            stats = os.path.join(tmp, "stats.log")
            rot = configure_global_rotator(pool, stats_file=stats, force=True)
            self.assertEqual(len(rot), 2)
            chosen = pick_proxy()
            self.assertIn(chosen, pool)
            report_outcome(chosen, False, "proxy_error")
            report_outcome(chosen, True, "ok")


if __name__ == "__main__":
    unittest.main()
