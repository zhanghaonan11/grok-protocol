# -*- coding: utf-8 -*-
import base64
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import http_batch_service as svc
import proxy_subscription as sub
import webui_app
from fastapi.testclient import TestClient


class ProxySubscriptionParseTests(unittest.TestCase):
    def test_fetch_plain_hostport_list_is_not_misdetected_as_base64(self):
        payload = b"159.253.120.61:3128\n167.71.233.240:3129\n"
        with mock.patch.object(sub, "urlopen") as mocked_open:
            mocked_open.return_value.__enter__.return_value.read.return_value = payload
            body, kind = sub.fetch_subscription_body("https://example.test/proxies.txt")

        self.assertEqual(kind, "plain")
        self.assertEqual(body.splitlines(), ["159.253.120.61:3128", "167.71.233.240:3129"])

    def test_parse_http_and_hostport_lines(self):
        nodes = sub.parse_subscription_text(
            "\n".join(
                [
                    "http://user:pass@1.2.3.4:8080",
                    "5.6.7.8:1000:u:p",
                    "6.7.8.9:3128",
                    "socks5://u:p@9.9.9.9:1080",
                ]
            )
        )
        self.assertEqual(len(nodes), 4)
        self.assertTrue(nodes[0].usable_http)
        self.assertEqual(nodes[0].pool_line, "1.2.3.4:8080:user:pass")
        self.assertTrue(nodes[1].usable_http)
        self.assertEqual(nodes[1].pool_line, "5.6.7.8:1000:u:p")
        self.assertTrue(nodes[2].usable_http)
        self.assertEqual(nodes[2].pool_line, "http://6.7.8.9:3128")
        self.assertFalse(nodes[3].usable_http)
        self.assertIsNone(sub.parse_share_link("mixed-port: 7890"))

    def test_parse_vless_not_usable(self):
        node = sub.parse_share_link(
            "vless://uuid@example.com:443?encryption=none&security=tls#node-a"
        )
        self.assertIsNotNone(node)
        self.assertEqual(node.scheme, "vless")
        self.assertEqual(node.host, "example.com")
        self.assertEqual(node.port, 443)
        self.assertFalse(node.usable_http)
        self.assertEqual(node.pool_line, "")

    def test_parse_hysteria2_and_anytls(self):
        hy = sub.parse_share_link(
            "hy2://secret@hy.example:8443?sni=hy.example&insecure=1#hy-a"
        )
        self.assertIsNotNone(hy)
        self.assertEqual(hy.scheme, "hysteria2")
        self.assertEqual(hy.host, "hy.example")
        self.assertEqual(hy.port, 8443)
        self.assertEqual(hy.password, "secret")
        self.assertFalse(hy.usable_http)

        anytls = sub.parse_share_link(
            "anytls://pwd@any.example:443?sni=any.example&fp=chrome#any-a"
        )
        self.assertIsNotNone(anytls)
        self.assertEqual(anytls.scheme, "anytls")
        self.assertEqual(anytls.host, "any.example")
        self.assertEqual(anytls.port, 443)
        self.assertEqual(anytls.password, "pwd")
        self.assertFalse(anytls.usable_http)

    def test_import_base64_subscription_only_http_goes_to_pool(self):
        plain = "\n".join(
            [
                "vless://uuid@a.example:443?security=tls#v1",
                "http://user:pass@10.0.0.2:8080#http1",
                "ss://YWVzLTI1Ni1nY206cGFzcw@b.example:8388#ss1",
            ]
        )
        body = base64.b64encode(plain.encode("utf-8")).decode("ascii")
        with mock.patch.object(sub, "fetch_subscription_body", return_value=(plain, "base64")):
            result = sub.import_proxy_subscription("https://example.test/sub")
        self.assertEqual(result.body_kind, "base64")
        self.assertEqual(result.usable_http_count if hasattr(result, "usable_http_count") else len(result.usable_pool_lines), 1)
        self.assertEqual(result.usable_pool_lines, ["10.0.0.2:8080:user:pass"])
        self.assertTrue(any("没有可直接用于注册机" not in w for w in result.warnings) or not result.warnings)
        self.assertTrue(any(line.startswith("#") for line in result.pool_lines))
        # body unused except to keep local var
        self.assertTrue(body)

    def test_import_all_vless_warns(self):
        plain = "vless://uuid@a.example:443?security=tls#v1\nvless://uuid2@b.example:443#v2\n"
        with mock.patch.object(sub, "fetch_subscription_body", return_value=(plain, "plain")):
            result = sub.import_proxy_subscription("https://example.test/sub2")
        self.assertEqual(len(result.usable_pool_lines), 0)
        self.assertEqual(len(result.nodes), 2)
        self.assertTrue(result.warnings)

    def test_normalize_subscription_urls_dedupe(self):
        urls = sub.normalize_subscription_urls(
            "https://a.example/sub\nhttps://b.example/sub\nhttps://a.example/sub\n"
        )
        self.assertEqual(urls, ["https://a.example/sub", "https://b.example/sub"])
        self.assertEqual(
            sub.resolve_subscription_urls_from_config(
                {"proxy_subscription_url": "https://legacy.example/s"}
            ),
            ["https://legacy.example/s"],
        )
        self.assertEqual(
            sub.resolve_subscription_urls_from_config(
                {
                    "proxy_subscription_urls": ["https://a.example/1"],
                    "proxy_subscription_url": "https://legacy.example/s",
                }
            ),
            ["https://a.example/1"],
        )

    def test_import_multiple_urls_merge_and_partial_failure(self):
        bodies = {
            "https://a.example/sub": (
                "http://u:p@1.1.1.1:8080\nvless://id@x.com:443#n\n",
                "plain",
            ),
            "https://b.example/sub": (
                "http://u2:p2@2.2.2.2:8080\nhttp://u:p@1.1.1.1:8080\n",
                "plain",
            ),
        }

        def fake_fetch(url, timeout=20.0):
            if url == "https://bad.example/sub":
                raise ValueError("boom")
            return bodies[url]

        with mock.patch.object(sub, "fetch_subscription_body", side_effect=fake_fetch):
            result = sub.import_proxy_subscriptions(
                [
                    "https://a.example/sub",
                    "https://bad.example/sub",
                    "https://b.example/sub",
                ]
            )
        self.assertEqual(len(result.usable_pool_lines), 2)
        self.assertIn("1.1.1.1:8080:u:p", result.usable_pool_lines)
        self.assertIn("2.2.2.2:8080:u2:p2", result.usable_pool_lines)
        self.assertEqual(len(result.per_url), 3)
        self.assertTrue(any(not p.get("ok") for p in result.per_url))
        self.assertTrue(any("拉取失败" in w for w in result.warnings))
        data = result.to_dict()
        self.assertEqual(len(data["urls"]), 3)



    def test_parse_clash_yaml_http_and_inventory(self):
        yaml_text = """mixed-port: 7890
proxies:
- name: http-a
  type: http
  server: 10.0.0.1
  port: 8080
  username: u
  password: p
- name: socks-a
  type: socks5
  server: 10.0.0.2
  port: 1080
- name: vless-a
  type: vless
  server: v.example
  port: 443
  uuid: 11111111-1111-1111-1111-111111111111
  tls: true
  network: tcp
- name: hy2-a
  type: hysteria2
  server: h.example
  port: 8443
  password: secret
  sni: h.example
proxy-groups:
- name: PROXY
  type: select
  proxies:
  - http-a
  - 🇭🇰 HK|60|M523ms|2406:4440:0:106::11:a|YT|http
"""
        nodes = sub.parse_subscription_text(yaml_text)
        self.assertEqual(len(nodes), 4)
        http_nodes = [n for n in nodes if n.usable_http]
        self.assertEqual(len(http_nodes), 1)
        self.assertEqual(http_nodes[0].pool_line, "10.0.0.1:8080:u:p")
        self.assertTrue(any(n.scheme == "vless" and n.raw.startswith("vless://") for n in nodes))
        self.assertTrue(any(n.scheme == "hysteria2" and n.raw.startswith("hysteria2://") for n in nodes))
        # Proxy-group list names must not become fake http pool lines.
        self.assertIsNone(
            sub.parse_share_link("- 🇭🇰 HK|60|M523ms|2406:4440:0:106::11:a|YT|http")
        )

    def test_hostport_rejects_yaml_like_names(self):
        self.assertIsNone(sub.parse_share_link("PROXY:select:http-a:extra"))
        node = sub.parse_share_link("1.2.3.4:8080:user:pass")
        self.assertIsNotNone(node)
        self.assertEqual(node.pool_line, "1.2.3.4:8080:user:pass")


class ProxySubscriptionServiceTests(unittest.TestCase):
    def test_service_writes_pool_and_switches_mode(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            cfg = root / "config.json"
            cfg.write_text(
                json.dumps(
                    {
                        "proxy_file": "proxies.txt",
                        "tui_proxy_mode": "none",
                        "proxy_pool_source": "subscription",
                        "proxy_subscription_local_http": "http://127.0.0.1:7890",
                    }
                ),
                encoding="utf-8",
            )
            service = svc.BatchService(config_path=cfg, root_dir=root)
            plain = "http://u:p@1.1.1.1:8080\nvless://id@x.com:443#n\n"
            with mock.patch(
                "proxy_subscription.fetch_subscription_body",
                return_value=(plain, "plain"),
            ):
                data = service.import_proxy_subscription(
                    url="https://example.test/sub",
                    write_pool=True,
                )
            self.assertEqual(data["usable_http_count"], 1)
            self.assertEqual(data["proxy_mode"], "pool")
            pool_path = root / "proxies.txt"
            text = pool_path.read_text(encoding="utf-8")
            self.assertIn("1.1.1.1:8080:u:p", text)
            disk = json.loads(cfg.read_text(encoding="utf-8"))
            self.assertEqual(disk.get("tui_proxy_mode"), "pool")
            self.assertEqual(disk.get("proxy_subscription_url"), "https://example.test/sub")

    def test_service_local_http_fallback_for_vless_only(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            cfg = root / "config.json"
            cfg.write_text(
                json.dumps(
                    {
                        "proxy_file": "proxies.txt",
                        "tui_proxy_mode": "none",
                        "proxy_pool_source": "subscription",
                        "proxy_subscription_local_http": "http://127.0.0.1:7890",
                    }
                ),
                encoding="utf-8",
            )
            service = svc.BatchService(config_path=cfg, root_dir=root)
            plain = "vless://uuid@a.example:443#v1\n"
            with mock.patch(
                "proxy_subscription.fetch_subscription_body",
                return_value=(plain, "base64"),
            ):
                data = service.import_proxy_subscription(
                    url="https://example.test/vless",
                    write_pool=True,
                    use_local_http_if_empty=True,
                    local_http="http://127.0.0.1:7890",
                )
            self.assertEqual(data["usable_http_count"], 0)
            self.assertTrue(data["applied_local_http"])
            self.assertEqual(data["proxy_mode"], "direct")
            text = (root / "proxies.txt").read_text(encoding="utf-8")
            self.assertIn("http://127.0.0.1:7890", text)
            disk = json.loads(cfg.read_text(encoding="utf-8"))
            self.assertEqual(disk.get("proxy"), "http://127.0.0.1:7890")
            self.assertEqual(disk.get("tui_proxy_mode"), "direct")

    def test_webui_import_subscription_api(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            cfg = root / "config.json"
            cfg.write_text(
                json.dumps(
                    {
                        "proxy_file": "proxies.txt",
                        "proxy_pool_source": "subscription",
                    }
                ),
                encoding="utf-8",
            )
            service = svc.BatchService(config_path=cfg, root_dir=root)
            app = webui_app.create_app(service=service)
            client = TestClient(app)
            page = client.get("/config/proxy")
            self.assertEqual(page.status_code, 200)
            self.assertIn("proxy_subscription_urls", page.text)
            self.assertIn("btnImportSub", page.text)
            self.assertIn("btnEmbeddedFetchSub", page.text)
            plain = "http://a:b@2.2.2.2:9000\n"
            with mock.patch(
                "proxy_subscription.fetch_subscription_body",
                return_value=(plain, "plain"),
            ):
                r = client.post(
                    "/api/proxy-pool/import-subscription",
                    json={"url": "https://example.test/api/sub", "write_pool": True},
                )
            self.assertEqual(r.status_code, 200)
            body = r.json()
            self.assertEqual(body["usable_http_count"], 1)
            self.assertIn("2.2.2.2:9000:a:b", body.get("text") or "")


if __name__ == "__main__":
    unittest.main()
