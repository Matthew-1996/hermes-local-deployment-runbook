import importlib.util
import pathlib
import subprocess
import sys
import tempfile
import types
import urllib.error
import unittest
from unittest import mock


try:
    import yaml  # noqa: F401
except ModuleNotFoundError:
    sys.modules["yaml"] = types.SimpleNamespace(YAMLError=Exception)


SCRIPT = (
    pathlib.Path(__file__).parents[1]
    / "scripts"
    / "guest"
    / "mihomo_refresh_fallbacks.py"
)
SPEC = importlib.util.spec_from_file_location("mihomo_refresh_fallbacks", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class ProxyFilteringTests(unittest.TestCase):
    def test_excludes_subscription_metadata_entries(self):
        proxy = {
            "name": "请使用最新代理工具 | new.example",
            "type": "trojan",
            "server": "example.invalid",
            "port": 443,
        }
        self.assertFalse(MODULE.is_real_proxy(proxy))

    def test_accepts_a_real_proxy(self):
        proxy = {
            "name": "香港 A2 | 中转",
            "type": "vless",
            "server": "example.invalid",
            "port": 443,
        }
        self.assertTrue(MODULE.is_real_proxy(proxy))


class SystemdServiceTests(unittest.TestCase):
    def test_reports_passwordless_sudo_failure_as_an_actionable_error(self):
        failure = subprocess.CalledProcessError(
            1,
            [
                "sudo",
                "-n",
                "systemctl",
                "stop",
                "hermes-gateway.service",
            ],
        )

        with mock.patch.object(MODULE.subprocess, "run", side_effect=failure):
            with self.assertRaisesRegex(
                RuntimeError,
                "passwordless sudo failed.*stop hermes-gateway.service",
            ):
                MODULE.systemd_service_action(
                    "stop", "hermes-gateway.service"
                )


class RankingTests(unittest.TestCase):
    def test_requires_both_endpoints_and_ranks_by_worst_latency(self):
        results = [
            {"name": "unstable", "auth_ok": True, "codex_ok": False,
             "auth_ms": 100, "codex_ms": None},
            {"name": "slow", "auth_ok": True, "codex_ok": True,
             "auth_ms": 900, "codex_ms": 700},
            {"name": "fast", "auth_ok": True, "codex_ok": True,
             "auth_ms": 300, "codex_ms": 450},
        ]
        ranked = MODULE.rank_usable(results, limit=5)
        self.assertEqual([item["name"] for item in ranked], ["fast", "slow"])
        self.assertEqual(ranked[0]["score_ms"], 450)

    def test_accepts_only_non_server_error_http_statuses(self):
        self.assertTrue(MODULE.acceptable_http_status(200))
        self.assertTrue(MODULE.acceptable_http_status(403))
        self.assertFalse(MODULE.acceptable_http_status(500))
        self.assertFalse(MODULE.acceptable_http_status(0))

    def test_selects_two_regions_by_stability_adjusted_latency(self):
        results = [
            {"name": "TW-1", "region": "台湾", "auth_ok": True,
             "codex_ok": True, "auth_ms": 180, "codex_ms": 220},
            {"name": "TW-2", "region": "台湾", "auth_ok": True,
             "codex_ok": True, "auth_ms": 210, "codex_ms": 240},
            {"name": "HK-1", "region": "香港", "auth_ok": True,
             "codex_ok": True, "auth_ms": 260, "codex_ms": 280},
            {"name": "HK-2", "region": "香港", "auth_ok": True,
             "codex_ok": True, "auth_ms": 290, "codex_ms": 310},
            {"name": "JP-1", "region": "日本", "auth_ok": True,
             "codex_ok": True, "auth_ms": 100, "codex_ms": 120},
            {"name": "JP-2", "region": "日本", "auth_ok": False,
             "codex_ok": False, "auth_ms": None, "codex_ms": None},
        ]

        selected = MODULE.select_regions(
            results, region_count=2, min_usable_nodes=2
        )

        self.assertEqual(
            [item["region"] for item in selected], ["台湾", "香港"]
        )
        self.assertEqual(selected[0]["node_names"], ["TW-1", "TW-2"])
        self.assertEqual(selected[0]["usable_count"], 2)
        self.assertEqual(selected[0]["tested_count"], 2)

    def test_region_detection_supports_flags_and_chinese_names(self):
        self.assertEqual(MODULE.detect_region("🇹🇼 台湾 B1 | 原生"), "台湾")
        self.assertEqual(MODULE.detect_region("香港 A2 | 中转"), "香港")
        self.assertEqual(MODULE.detect_region("US-West"), "美国")
        self.assertEqual(MODULE.detect_region("🇩🇪 德国 X2 | 专线"), "德国")
        self.assertEqual(MODULE.detect_region("澳洲 X1 | 专线"), "澳大利亚")
        self.assertIsNone(MODULE.detect_region("剩余流量"))

    def test_region_result_requires_two_eligible_regions(self):
        with self.assertRaisesRegex(ValueError, "only 1 eligible regions"):
            MODULE.create_region_result(
                [
                    {"name": "TW-1", "region": "台湾", "auth_ok": True,
                     "codex_ok": True, "auth_ms": 180, "codex_ms": 220},
                    {"name": "TW-2", "region": "台湾", "auth_ok": True,
                     "codex_ok": True, "auth_ms": 210, "codex_ms": 240},
                    {"name": "HK-1", "region": "香港", "auth_ok": False,
                     "codex_ok": False, "auth_ms": None, "codex_ms": None},
                ],
                region_count=2,
                min_usable_nodes=2,
            )


class ProbeConfigTests(unittest.TestCase):
    def test_proxied_request_uses_a_compatible_user_agent(self):
        class Response:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self, _size=-1):
                return b"{}"

        class UserAgentAwareOpener:
            def open(self, request, timeout):
                if request.get_header("User-agent") == "clash.meta":
                    return Response()
                raise urllib.error.HTTPError(
                    request.full_url, 530, "blocked user agent", {}, None
                )

        with mock.patch.object(
            MODULE.urllib.request,
            "build_opener",
            return_value=UserAgentAwareOpener(),
        ):
            result = MODULE.proxied_request(
                17890,
                MODULE.AUTH_URL,
                method="POST",
                body={},
            )

        self.assertEqual(result[0], True)
        self.assertEqual(result[2], 200)

    def test_subscription_fetch_retries_direct_then_uses_proxy(self):
        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return b"proxies: []\n"

        direct = mock.Mock()
        direct.open.side_effect = urllib.error.URLError("temporary TLS EOF")
        proxy = mock.Mock()
        proxy.open.return_value = Response()

        with mock.patch.object(
            MODULE.urllib.request,
            "build_opener",
            side_effect=[direct, proxy],
        ), mock.patch.object(MODULE.time, "sleep"), mock.patch.object(
            MODULE.yaml,
            "safe_load",
            return_value={"proxies": []},
            create=True,
        ):
            result = MODULE.fetch_subscription(
                "https://subscription.example/link",
                proxy_url="http://127.0.0.1:17890",
                attempts=3,
            )

        self.assertEqual(result, {"proxies": []})
        self.assertEqual(direct.open.call_count, 3)
        self.assertEqual(proxy.open.call_count, 1)

    def test_builds_isolated_probe_config_without_geo_rules(self):
        subscription = {
            "unified-delay": True,
            "tcp-concurrent": True,
            "dns": {
                "enable": True,
                "listen": "127.0.0.1:1053",
                "nameserver": ["https://dns.example/dns-query"],
                "fallback-filter": {
                    "geoip": True,
                    "geoip-code": "CN",
                    "ipcidr": ["240.0.0.0/4"],
                },
            },
            "proxies": [
                {"name": "real", "type": "trojan",
                 "server": "real.invalid", "port": 443},
                {"name": "剩余流量：100 GB", "type": "trojan",
                 "server": "meta.invalid", "port": 443},
            ],
            "rules": ["GEOIP,CN,DIRECT", "MATCH,漏网之鱼"],
        }

        probe = MODULE.build_probe_config(
            subscription,
            mixed_port=17891,
            controller_port=19091,
            dns_port=11053,
        )

        self.assertEqual(probe["mixed-port"], 17891)
        self.assertEqual(probe["external-controller"], "127.0.0.1:19091")
        self.assertEqual(probe["dns"]["listen"], "127.0.0.1:11053")
        self.assertFalse(probe["dns"]["fallback-filter"]["geoip"])
        self.assertNotIn(
            "geoip-code", probe["dns"]["fallback-filter"]
        )
        self.assertEqual(
            probe["dns"]["fallback-filter"]["ipcidr"], ["240.0.0.0/4"]
        )
        self.assertTrue(probe["unified-delay"])
        self.assertTrue(probe["tcp-concurrent"])
        self.assertEqual([item["name"] for item in probe["proxies"]], ["real"])
        self.assertEqual(probe["rules"], ["MATCH,PROBE"])

    def test_builds_localhost_only_live_probe_from_full_subscription(self):
        production = {
            "mixed-port": 7890,
            "proxies": [{
                "name": "MAC-CUTECLOUD",
                "type": "http",
                "server": "127.0.0.1",
                "port": 17890,
            }],
        }
        subscription = {
            "mixed-port": 7890,
            "allow-lan": True,
            "bind-address": "0.0.0.0",
            "external-controller": "0.0.0.0:9090",
            "dns": {"enable": True, "listen": "127.0.0.1:1053"},
            "proxies": [
                {"name": "台湾-1", "type": "trojan",
                 "server": "tw.example", "port": 443},
                {"name": "香港-1", "type": "vless",
                 "server": "hk.example", "port": 443},
                {"name": "剩余流量：100 GB", "type": "trojan",
                 "server": "meta.example", "port": 443},
            ],
            "proxy-groups": [{
                "name": "AI服务",
                "type": "select",
                "proxies": ["剩余流量：100 GB", "台湾-1", "香港-1"],
            }],
            "rules": ["MATCH,AI服务"],
        }

        live = MODULE.build_live_probe_config(
            production, subscription, controller_port=19090
        )

        self.assertEqual(live["mixed-port"], 7890)
        self.assertFalse(live["allow-lan"])
        self.assertEqual(live["bind-address"], "127.0.0.1")
        self.assertEqual(live["external-controller"], "127.0.0.1:19090")
        self.assertEqual(
            [proxy["name"] for proxy in live["proxies"]],
            [
                "MAC-SHADOWROCKET",
                "台湾-1",
                "香港-1",
                "剩余流量：100 GB",
            ],
        )
        probe_group = next(
            group for group in live["proxy-groups"]
            if group["name"] == "CODEX-LIVE-PROBE"
        )
        self.assertEqual(probe_group["proxies"], ["台湾-1", "香港-1"])
        self.assertEqual(
            live["rules"][:2],
            [
                "DOMAIN-SUFFIX,openai.com,CODEX-LIVE-PROBE",
                "DOMAIN-SUFFIX,chatgpt.com,CODEX-LIVE-PROBE",
            ],
        )
        self.assertEqual(live["rules"][2:], ["MATCH,AI服务"])
        self.assertEqual(live["dns"], subscription["dns"])

    def test_live_transaction_restores_config_and_services_on_probe_error(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = pathlib.Path(temp_dir) / "config.yaml"
            config_path.write_text("original\n", encoding="utf-8")
            service_calls = []

            def service_active(service):
                return service == "hermes-gateway.service"

            def service_action(action, service):
                service_calls.append((action, service))

            def failing_probe():
                self.assertEqual(
                    config_path.read_text(encoding="utf-8"), "live\n"
                )
                raise RuntimeError("probe failed")

            with self.assertRaisesRegex(RuntimeError, "probe failed"):
                MODULE.run_live_transaction(
                    config_path,
                    "live\n",
                    failing_probe,
                    service_active=service_active,
                    service_action=service_action,
                )

            self.assertEqual(
                config_path.read_text(encoding="utf-8"), "original\n"
            )
            self.assertEqual(
                service_calls,
                [
                    ("stop", "hermes-gateway.service"),
                    ("restart", "mihomo.service"),
                    ("restart", "mihomo.service"),
                    ("start", "hermes-gateway.service"),
                ],
            )
            self.assertEqual(len(list(pathlib.Path(temp_dir).glob(
                "config.yaml.before-live-probe-*"
            ))), 1)

    def test_actual_probe_preserves_each_endpoint_result(self):
        responses = [
            (True, 300, 200),
            (False, 350, 502),
        ]
        with mock.patch.object(MODULE, "controller_request"), \
                mock.patch.object(MODULE.time, "sleep"), \
                mock.patch.object(MODULE, "proxied_request", side_effect=responses):
            result = MODULE.actual_probe(
                19091,
                17891,
                {"name": "node", "auth_ok": True, "codex_ok": True,
                 "auth_ms": 100, "codex_ms": 100},
                rounds=1,
            )
        self.assertTrue(result["auth_ok"])
        self.assertFalse(result["codex_ok"])
        self.assertEqual(result["auth_status"], 200)
        self.assertEqual(result["codex_status"], 502)

    def test_actual_probe_can_select_the_live_probe_group(self):
        with mock.patch.object(
            MODULE, "controller_request"
        ) as controller, mock.patch.object(
            MODULE.time, "sleep"
        ), mock.patch.object(
            MODULE,
            "proxied_request",
            side_effect=[(True, 200, 200), (True, 250, 403)],
        ):
            MODULE.actual_probe(
                19090,
                7890,
                {"name": "台湾-1", "type": "trojan", "region": "台湾"},
                rounds=1,
                group_name="CODEX-LIVE-PROBE",
            )

        self.assertEqual(
            controller.call_args.args[2],
            "/proxies/CODEX-LIVE-PROBE",
        )


class CandidateConfigTests(unittest.TestCase):
    def test_builds_two_regional_groups_and_renames_mac_proxy(self):
        production = {
            "proxies": [
                {"name": "MAC-CUTECLOUD", "type": "http",
                 "server": "127.0.0.1", "port": 17890},
                {"name": "old fallback", "type": "trojan",
                 "server": "old.invalid", "port": 443},
            ],
            "proxy-groups": [{
                "name": "CODEX",
                "type": "fallback",
                "proxies": ["MAC-CUTECLOUD", "old fallback"],
                "url": "https://chatgpt.com/backend-api/codex",
                "interval": 180,
                "lazy": True,
            }],
            "rules": ["DOMAIN-SUFFIX,chatgpt.com,CODEX", "MATCH,DIRECT"],
        }
        fresh = [
            {"name": f"node-{index}", "type": "trojan",
             "server": f"node-{index}.invalid", "port": 443}
            for index in range(1, 7)
        ]
        selected_regions = [
            {"region": "台湾", "node_names": ["node-1", "node-2"]},
            {"region": "香港", "node_names": ["node-3", "node-4", "node-5"]},
        ]

        candidate = MODULE.build_candidate_config(
            production,
            fresh,
            selected_regions,
            old_mac_name="MAC-CUTECLOUD",
            new_mac_name="MAC-SHADOWROCKET",
        )

        names = [proxy["name"] for proxy in candidate["proxies"]]
        self.assertIn("MAC-SHADOWROCKET", names)
        self.assertNotIn("MAC-CUTECLOUD", names)
        group = next(
            group for group in candidate["proxy-groups"]
            if group["name"] == "CODEX"
        )
        self.assertEqual(
            group["proxies"],
            ["MAC-SHADOWROCKET", "CODEX-备用-台湾", "CODEX-备用-香港"],
        )
        self.assertEqual(group["type"], "fallback")
        self.assertEqual(group["interval"], 180)
        self.assertTrue(group["lazy"])
        self.assertEqual(group["expected-status"], "200-499")

        taiwan = next(
            item for item in candidate["proxy-groups"]
            if item["name"] == "CODEX-备用-台湾"
        )
        self.assertEqual(taiwan["type"], "url-test")
        self.assertEqual(taiwan["proxies"], ["node-1", "node-2"])
        self.assertEqual(taiwan["tolerance"], 150)
        self.assertEqual(taiwan["expected-status"], "200-499")
        self.assertTrue(taiwan["lazy"])

    def test_rejects_missing_selected_proxy(self):
        with self.assertRaisesRegex(ValueError, "missing selected proxy"):
            MODULE.build_candidate_config(
                {
                    "proxies": [{"name": "MAC-CUTECLOUD", "type": "http",
                                 "server": "127.0.0.1", "port": 17890}],
                    "proxy-groups": [{"name": "CODEX", "proxies": []}],
                },
                [],
                [
                    {"region": "香港", "node_names": ["missing"]},
                    {"region": "台湾", "node_names": ["also-missing"]},
                ],
                old_mac_name="MAC-CUTECLOUD",
                new_mac_name="MAC-SHADOWROCKET",
            )


if __name__ == "__main__":
    unittest.main()
