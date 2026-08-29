#!/usr/bin/env python3
"""Safely refresh and rank Mihomo fallback nodes for Hermes Codex traffic.

The probe command never modifies the production Mihomo configuration. The
live-probe command is an explicit maintenance operation: it temporarily swaps
the production configuration, tests real OpenAI endpoints, and restores the
original configuration and services in a finally block. Subscription URLs are
entered without echoing and are never stored in result files.
"""

import argparse
import copy
import getpass
import json
import os
import pathlib
import re
import shutil
import socket
import statistics
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

import yaml


AUTH_URL = "https://auth.openai.com/api/accounts/deviceauth/usercode"
CODEX_URL = "https://chatgpt.com/backend-api/codex"
OLD_MAC_NAME = "MAC-CUTECLOUD"
NEW_MAC_NAME = "MAC-SHADOWROCKET"
METADATA_PATTERN = re.compile(
    r"剩余流量|套餐到期|到期|流量|倍率提示|请使用|最新代理工具|"
    r"官网|客服|订阅|更新地址|过期",
    re.IGNORECASE,
)
REGION_PATTERNS = (
    ("香港", re.compile(r"🇭🇰|香港|hong\s*kong|\bhk\b", re.IGNORECASE)),
    ("台湾", re.compile(r"🇹🇼|台湾|台灣|taiwan|\btw\b", re.IGNORECASE)),
    ("日本", re.compile(r"🇯🇵|日本|japan|\bjp\b", re.IGNORECASE)),
    ("韩国", re.compile(r"🇰🇷|韩国|韓國|korea|\bkr\b", re.IGNORECASE)),
    ("新加坡", re.compile(r"🇸🇬|新加坡|singapore|\bsg\b", re.IGNORECASE)),
    ("美国", re.compile(r"🇺🇸|美国|美國|united\s*states|\busa?\b", re.IGNORECASE)),
    ("加拿大", re.compile(r"🇨🇦|加拿大|canada|\bca\b", re.IGNORECASE)),
    ("英国", re.compile(r"🇬🇧|英国|英國|united\s*kingdom|\buk\b", re.IGNORECASE)),
    ("马来西亚", re.compile(r"🇲🇾|马来西亚|馬來西亞|malaysia|\bmy\b", re.IGNORECASE)),
    ("菲律宾", re.compile(r"🇵🇭|菲律宾|菲律賓|philippines|\bph\b", re.IGNORECASE)),
    ("俄罗斯", re.compile(r"🇷🇺|俄罗斯|俄羅斯|russia|\bru\b", re.IGNORECASE)),
    ("沙特", re.compile(r"🇸🇦|沙特|saudi|\bsa\b", re.IGNORECASE)),
    ("智利", re.compile(r"🇨🇱|智利|chile|\bcl\b", re.IGNORECASE)),
    ("巴西", re.compile(r"🇧🇷|巴西|brazil|\bbr\b", re.IGNORECASE)),
    ("澳大利亚", re.compile(r"🇦🇺|澳洲|澳大利亚|澳大利亞|australia|\bau\b", re.IGNORECASE)),
    ("印度", re.compile(r"🇮🇳|印度|india|\bin\b", re.IGNORECASE)),
    ("法国", re.compile(r"🇫🇷|法国|法國|france|\bfr\b", re.IGNORECASE)),
    ("德国", re.compile(r"🇩🇪|德国|德國|germany|\bde\b", re.IGNORECASE)),
)


def is_real_proxy(proxy):
    if not isinstance(proxy, dict):
        return False
    name = str(proxy.get("name", "")).strip()
    proxy_type = str(proxy.get("type", "")).strip().lower()
    if not name or METADATA_PATTERN.search(name):
        return False
    if proxy_type in {"", "direct", "reject", "reject-drop", "pass"}:
        return False
    return bool(proxy.get("server")) and proxy.get("port") is not None


def acceptable_http_status(status):
    return 200 <= int(status or 0) < 500


def rank_usable(results, limit=5):
    usable = []
    for result in results:
        if not result.get("auth_ok") or not result.get("codex_ok"):
            continue
        item = dict(result)
        item["score_ms"] = max(int(item["auth_ms"]), int(item["codex_ms"]))
        usable.append(item)
    usable.sort(key=lambda item: (item["score_ms"], item["name"]))
    return usable[:limit]


def detect_region(name):
    text = str(name or "").strip()
    if not text or METADATA_PATTERN.search(text):
        return None
    for region, pattern in REGION_PATTERNS:
        if pattern.search(text):
            return region
    return None


def select_regions(results, region_count=2, min_usable_nodes=2):
    grouped = {}
    for result in results:
        region = result.get("region") or detect_region(result.get("name"))
        if region is None:
            continue
        bucket = grouped.setdefault(region, {"tested": [], "usable": []})
        bucket["tested"].append(result)
        if result.get("auth_ok") and result.get("codex_ok"):
            item = dict(result)
            item["score_ms"] = max(int(item["auth_ms"]), int(item["codex_ms"]))
            bucket["usable"].append(item)

    summaries = []
    for region, bucket in grouped.items():
        usable = sorted(
            bucket["usable"], key=lambda item: (item["score_ms"], item["name"])
        )
        if len(usable) < min_usable_nodes:
            continue
        tested_count = len(bucket["tested"])
        success_rate = len(usable) / tested_count
        median_ms = int(statistics.median(item["score_ms"] for item in usable))
        summaries.append({
            "region": region,
            "tested_count": tested_count,
            "usable_count": len(usable),
            "success_rate": success_rate,
            "median_ms": median_ms,
            "region_score_ms": int(round(median_ms / success_rate)),
            "node_names": [item["name"] for item in usable],
            "nodes": usable,
        })
    summaries.sort(key=lambda item: (
        item["region_score_ms"], -item["usable_count"], item["region"]
    ))
    return summaries[:region_count]


def create_region_result(results, region_count=2, min_usable_nodes=2):
    selected = select_regions(
        results,
        region_count=region_count,
        min_usable_nodes=min_usable_nodes,
    )
    if len(selected) != region_count:
        raise ValueError(
            "only %d eligible regions; need %d regions with at least %d "
            "usable nodes each" % (
                len(selected), region_count, min_usable_nodes
            )
        )
    return {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "selected_regions": selected,
    }


def build_probe_config(subscription, mixed_port, controller_port, dns_port):
    proxies = [copy.deepcopy(p) for p in subscription.get("proxies", [])
               if is_real_proxy(p)]
    if not proxies:
        raise ValueError("subscription contains no real proxies")
    config = {
        "mixed-port": mixed_port,
        "allow-lan": False,
        "bind-address": "127.0.0.1",
        "mode": "rule",
        "log-level": "warning",
        "ipv6": False,
        "external-controller": "127.0.0.1:%d" % controller_port,
        "proxies": proxies,
        "proxy-groups": [{
            "name": "PROBE",
            "type": "select",
            "proxies": [proxy["name"] for proxy in proxies],
        }],
        "rules": ["MATCH,PROBE"],
    }
    for key in ("unified-delay", "tcp-concurrent"):
        if key in subscription:
            config[key] = bool(subscription[key])
    dns = subscription.get("dns")
    if isinstance(dns, dict) and dns.get("enable"):
        config["dns"] = copy.deepcopy(dns)
        config["dns"]["listen"] = "127.0.0.1:%d" % dns_port
        fallback_filter = config["dns"].get("fallback-filter")
        if isinstance(fallback_filter, dict) and fallback_filter.get("geoip"):
            fallback_filter["geoip"] = False
            fallback_filter.pop("geoip-code", None)
    return config


def build_live_probe_config(production, subscription, controller_port=19090):
    subscription_proxies = [
        copy.deepcopy(proxy)
        for proxy in subscription.get("proxies", [])
        if isinstance(proxy, dict) and proxy.get("name")
    ]
    proxies = [
        proxy
        for proxy in subscription_proxies
        if is_real_proxy(proxy)
    ]
    if not proxies:
        raise ValueError("subscription contains no real proxies")

    production_proxies = production.get("proxies", [])
    mac_proxy = next((
        copy.deepcopy(proxy)
        for proxy in production_proxies
        if proxy.get("name") in {OLD_MAC_NAME, NEW_MAC_NAME}
    ), None)
    if mac_proxy is None:
        raise ValueError("missing Mac proxy in production config")
    mac_proxy["name"] = NEW_MAC_NAME
    if any(proxy.get("name") == NEW_MAC_NAME for proxy in subscription_proxies):
        raise ValueError("subscription conflicts with Mac proxy name")

    config = copy.deepcopy(subscription)
    for key in (
        "port", "socks-port", "redir-port", "tproxy-port", "tun",
        "listeners", "external-ui", "external-ui-url", "secret",
    ):
        config.pop(key, None)
    config.update({
        "mixed-port": int(production.get("mixed-port", 7890)),
        "allow-lan": False,
        "bind-address": "127.0.0.1",
        "mode": "rule",
        "external-controller": "127.0.0.1:%d" % controller_port,
        "proxies": [mac_proxy] + subscription_proxies,
    })

    probe_group_name = "CODEX-LIVE-PROBE"
    groups = [
        copy.deepcopy(group)
        for group in subscription.get("proxy-groups", [])
        if isinstance(group, dict) and group.get("name") != probe_group_name
    ]
    groups.insert(0, {
        "name": probe_group_name,
        "type": "select",
        "proxies": [proxy["name"] for proxy in proxies],
    })
    config["proxy-groups"] = groups
    config["rules"] = [
        "DOMAIN-SUFFIX,openai.com,%s" % probe_group_name,
        "DOMAIN-SUFFIX,chatgpt.com,%s" % probe_group_name,
    ] + list(subscription.get("rules", []))
    return config


def systemd_service_active(service):
    result = subprocess.run(
        ["systemctl", "is-active", "--quiet", service],
        check=False,
    )
    return result.returncode == 0


def systemd_service_action(action, service):
    try:
        subprocess.run(
            ["sudo", "-n", "systemctl", action, service],
            check=True,
        )
    except subprocess.CalledProcessError as error:
        raise RuntimeError(
            "passwordless sudo failed for systemctl %s %s; add the exact "
            "NOPASSWD rule and validate it with visudo" % (action, service)
        ) from error


def run_live_transaction(config_path, live_text, probe_callback,
                         service_active=systemd_service_active,
                         service_action=systemd_service_action):
    config_path = pathlib.Path(config_path)
    original_text = config_path.read_text(encoding="utf-8")
    original_mode = config_path.stat().st_mode & 0o777
    backup_path = config_path.with_name(
        config_path.name + ".before-live-probe-" +
        time.strftime("%Y%m%d-%H%M%S")
    )
    shutil.copy2(config_path, backup_path)
    gateway_service = "hermes-gateway.service"
    mihomo_service = "mihomo.service"
    gateway_was_active = service_active(gateway_service)
    if gateway_was_active:
        service_action("stop", gateway_service)
    try:
        atomic_write(config_path, live_text, mode=original_mode)
        service_action("restart", mihomo_service)
        return probe_callback()
    finally:
        try:
            atomic_write(config_path, original_text, mode=original_mode)
            service_action("restart", mihomo_service)
        finally:
            if gateway_was_active:
                service_action("start", gateway_service)


def build_candidate_config(production, fresh_proxies, selected_regions,
                           old_mac_name=OLD_MAC_NAME,
                           new_mac_name=NEW_MAC_NAME):
    candidate = copy.deepcopy(production)
    proxy_list = candidate.get("proxies", [])
    old_mac = next((p for p in proxy_list if p.get("name") == old_mac_name), None)
    if old_mac is None:
        old_mac = next((p for p in proxy_list if p.get("name") == new_mac_name), None)
    if old_mac is None:
        raise ValueError("missing Mac proxy in production config")

    selected_names = [
        name
        for region in selected_regions
        for name in region.get("node_names", [])
    ]
    if len(selected_regions) != 2 or any(
        not region.get("region") or not region.get("node_names")
        for region in selected_regions
    ):
        raise ValueError("candidate result must contain two nonempty regions")

    fresh_by_name = {p.get("name"): p for p in fresh_proxies if is_real_proxy(p)}
    missing = [name for name in selected_names if name not in fresh_by_name]
    if missing:
        raise ValueError("missing selected proxy: %s" % ", ".join(missing))

    if old_mac.get("name") != new_mac_name:
        if any(p.get("name") == new_mac_name for p in proxy_list):
            raise ValueError("new Mac proxy name already exists")
        old_mac["name"] = new_mac_name

    selected_set = set(selected_names)
    candidate["proxies"] = [
        p for p in proxy_list if p.get("name") not in selected_set
    ]
    candidate["proxies"].extend(
        copy.deepcopy(fresh_by_name[name]) for name in selected_names
    )

    region_prefix = "CODEX-备用-"
    groups = [
        group for group in candidate.get("proxy-groups", [])
        if not str(group.get("name", "")).startswith(region_prefix)
    ]
    candidate["proxy-groups"] = groups
    codex_group = None
    for group in groups:
        members = group.get("proxies")
        if isinstance(members, list):
            group["proxies"] = [
                new_mac_name if member == old_mac_name else member
                for member in members
            ]
        if group.get("name") == "CODEX":
            codex_group = group
    if codex_group is None:
        raise ValueError("missing CODEX proxy group")
    region_group_names = []
    for region in selected_regions:
        group_name = region_prefix + region["region"]
        region_group_names.append(group_name)
        groups.append({
            "name": group_name,
            "type": "url-test",
            "proxies": list(region["node_names"]),
            "url": CODEX_URL,
            "interval": 180,
            "lazy": True,
            "tolerance": 150,
            "timeout": 10000,
            "expected-status": "200-499",
        })
    codex_group.update({
        "type": "fallback",
        "proxies": [new_mac_name] + region_group_names,
        "url": CODEX_URL,
        "interval": 180,
        "lazy": True,
        "timeout": 10000,
        "expected-status": "200-499",
    })
    return candidate


def free_port():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def atomic_write(path, data, mode=0o600):
    path = pathlib.Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp-%d" % os.getpid())
    with open(temp, "w", encoding="utf-8") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(temp, mode)
    os.replace(temp, path)


def fetch_subscription(url, proxy_url=None, attempts=3):
    if attempts < 1:
        raise ValueError("subscription fetch attempts must be positive")
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "clash.meta", "Accept": "text/yaml,*/*"},
    )
    handlers = [urllib.request.ProxyHandler({})]
    if proxy_url:
        handlers.append(urllib.request.ProxyHandler({
            "http": proxy_url,
            "https": proxy_url,
        }))
    last_error = None
    payload = None
    for handler in handlers:
        opener = urllib.request.build_opener(handler)
        for attempt in range(attempts):
            try:
                with opener.open(request, timeout=90) as response:
                    payload = response.read()
                break
            except (OSError, urllib.error.URLError) as error:
                last_error = error
                if attempt + 1 < attempts:
                    time.sleep(attempt + 1)
        if payload is not None:
            break
    if payload is None:
        raise last_error or RuntimeError("subscription fetch failed")
    data = yaml.safe_load(payload.decode("utf-8-sig"))
    if not isinstance(data, dict) or not isinstance(data.get("proxies"), list):
        raise ValueError("subscription did not return a Clash YAML configuration")
    return data


def controller_request(controller_port, method, path, body=None, timeout=20):
    url = "http://127.0.0.1:%d%s" % (controller_port, path)
    data = None
    headers = {}
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, method=method, headers=headers)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = response.read()
    return json.loads(payload.decode("utf-8")) if payload else {}


def wait_for_controller(controller_port, process, deadline_seconds=15):
    deadline = time.monotonic() + deadline_seconds
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError("temporary Mihomo exited before becoming ready")
        try:
            controller_request(controller_port, "GET", "/version", timeout=1)
            return
        except (OSError, urllib.error.URLError):
            time.sleep(0.2)
    raise RuntimeError("temporary Mihomo controller did not become ready")


def proxied_request(proxy_port, url, method="GET", body=None, timeout=15):
    proxy_url = "http://127.0.0.1:%d" % proxy_port
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url})
    )
    data = None
    headers = {"User-Agent": "clash.meta"}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, method=method, headers=headers)
    started = time.monotonic()
    try:
        with opener.open(request, timeout=timeout) as response:
            status = response.status
            response.read(1)
    except urllib.error.HTTPError as error:
        status = error.code
    except (OSError, urllib.error.URLError, TimeoutError):
        return False, None, 0
    elapsed_ms = int((time.monotonic() - started) * 1000)
    return acceptable_http_status(status), elapsed_ms, status


def actual_probe(controller_port, mixed_port, result, rounds=2,
                 group_name="PROBE"):
    name = result["name"]
    encoded = urllib.parse.quote(group_name, safe="")
    controller_request(
        controller_port, "PUT", "/proxies/%s" % encoded, {"name": name}
    )
    time.sleep(0.15)
    auth_samples = []
    codex_samples = []
    auth_statuses = []
    codex_statuses = []
    for _ in range(rounds):
        auth_ok, auth_ms, auth_status = proxied_request(
            mixed_port, AUTH_URL, method="POST", body={}
        )
        codex_ok, codex_ms, codex_status = proxied_request(
            mixed_port, CODEX_URL
        )
        if not auth_ok or not codex_ok:
            return {
                **result,
                "auth_ok": auth_ok,
                "codex_ok": codex_ok,
                "auth_ms": auth_ms,
                "codex_ms": codex_ms,
                "auth_status": auth_status,
                "codex_status": codex_status,
            }
        auth_samples.append(auth_ms)
        codex_samples.append(codex_ms)
        auth_statuses.append(auth_status)
        codex_statuses.append(codex_status)
    return {
        **result,
        "auth_ok": True,
        "codex_ok": True,
        "auth_ms": max(auth_samples),
        "codex_ms": max(codex_samples),
        "auth_status": auth_statuses[-1],
        "codex_status": codex_statuses[-1],
    }


def probe_command(args):
    mihomo = shutil.which(args.mihomo)
    if not mihomo:
        raise RuntimeError("mihomo executable not found")
    subscription_url = getpass.getpass("Subscription URL (hidden): ").strip()
    if not subscription_url.startswith("https://"):
        raise ValueError("subscription URL must use HTTPS")
    subscription = fetch_subscription(
        subscription_url,
        proxy_url=args.subscription_proxy,
    )
    real_proxies = [p for p in subscription["proxies"] if is_real_proxy(p)]
    recognized = [p for p in real_proxies if detect_region(p.get("name"))]
    skipped = len(real_proxies) - len(recognized)
    if not recognized:
        raise RuntimeError("no region-labelled real proxies found")
    print(
        "Found %d real proxies (%d region-labelled, %d skipped); "
        "running real endpoint checks..." % (
            len(real_proxies), len(recognized), skipped
        )
    )

    mixed_port, controller_port, dns_port = free_port(), free_port(), free_port()
    probe_config = build_probe_config(
        subscription, mixed_port, controller_port, dns_port
    )
    with tempfile.TemporaryDirectory(prefix="mihomo-codex-refresh-") as temp_dir:
        config_path = pathlib.Path(temp_dir) / "config.yaml"
        config_path.write_text(
            yaml.safe_dump(probe_config, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )
        log_path = pathlib.Path(temp_dir) / "mihomo.log"
        with open(log_path, "wb") as log_handle:
            process = subprocess.Popen(
                [mihomo, "-d", temp_dir, "-f", str(config_path)],
                stdout=log_handle,
                stderr=subprocess.STDOUT,
            )
            try:
                wait_for_controller(controller_port, process)
                actual = []
                for index, proxy in enumerate(recognized, 1):
                    result = {
                        "name": proxy["name"],
                        "type": proxy.get("type", ""),
                        "region": detect_region(proxy["name"]),
                    }
                    verified = actual_probe(
                        controller_port, mixed_port, result, rounds=args.rounds
                    )
                    actual.append(verified)
                    print(
                        "%02d/%02d. [%s] %s | AUTH=%s HTTP=%s %sms | "
                        "CODEX=%s HTTP=%s %sms" % (
                            index,
                            len(recognized),
                            result["region"],
                            result["name"],
                            "OK" if verified["auth_ok"] else "FAIL",
                            verified.get("auth_status"),
                            verified.get("auth_ms"),
                            "OK" if verified["codex_ok"] else "FAIL",
                            verified.get("codex_status"),
                            verified.get("codex_ms"),
                        )
                    )
                result_payload = create_region_result(
                    actual,
                    region_count=args.regions,
                    min_usable_nodes=args.min_region_nodes,
                )
            finally:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)

    output_dir = pathlib.Path(args.config_dir).expanduser()
    subscription_path = output_dir / "subscription.candidate.yaml"
    result_path = output_dir / "codex-region-candidates.json"
    atomic_write(
        subscription_path,
        yaml.safe_dump(subscription, allow_unicode=True, sort_keys=False),
    )
    atomic_write(result_path, json.dumps(
        result_payload, ensure_ascii=False, indent=2
    ) + "\n")
    print("\nSelected regional fallback groups:")
    for index, region in enumerate(result_payload["selected_regions"], 1):
        print(
            "%02d. %s | usable=%d/%d | median=%dms | "
            "adjusted-score=%dms" % (
                index,
                region["region"],
                region["usable_count"],
                region["tested_count"],
                region["median_ms"],
                region["region_score_ms"],
            )
        )
        for node_name in region["node_names"]:
            print("    - %s" % node_name)
    print("RESULT_FILE=%s" % result_path)
    print("PRODUCTION_CONFIG_CHANGED=NO")


def live_probe_command(args):
    if not args.confirm_live:
        raise ValueError(
            "live probe requires --confirm-live because it temporarily stops "
            "Hermes Gateway"
        )
    mihomo = shutil.which(args.mihomo)
    systemctl = shutil.which("systemctl")
    if not mihomo or not systemctl:
        raise RuntimeError("mihomo and systemctl are required")

    config_dir = pathlib.Path(args.config_dir).expanduser()
    production_path = config_dir / "config.yaml"
    production = yaml.safe_load(
        production_path.read_text(encoding="utf-8")
    ) or {}
    if not systemd_service_active("mihomo.service"):
        raise RuntimeError("production Mihomo is not active")

    mac_auth_ok, _, _ = proxied_request(
        17890, AUTH_URL, method="POST", body={}
    )
    mac_codex_ok, _, _ = proxied_request(17890, CODEX_URL)
    if not mac_auth_ok or not mac_codex_ok:
        raise RuntimeError("Mac Shadowrocket recovery path is not healthy")

    subscription_url = getpass.getpass("Subscription URL (hidden): ").strip()
    if not subscription_url.startswith("https://"):
        raise ValueError("subscription URL must use HTTPS")
    subscription = fetch_subscription(
        subscription_url,
        proxy_url=args.subscription_proxy,
    )
    real_proxies = [
        proxy for proxy in subscription.get("proxies", [])
        if is_real_proxy(proxy) and detect_region(proxy.get("name"))
    ]
    if not real_proxies:
        raise RuntimeError("no region-labelled real proxies found")

    live_config = build_live_probe_config(
        production,
        subscription,
        controller_port=args.controller_port,
    )
    live_path = config_dir / "config.yaml.live-probe-candidate"
    live_text = yaml.safe_dump(
        live_config, allow_unicode=True, sort_keys=False
    )
    atomic_write(live_path, live_text)
    validation = subprocess.run(
        [mihomo, "-d", str(config_dir), "-t", "-f", str(live_path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=180,
        check=False,
    )
    if validation.returncode != 0:
        print(validation.stdout.rstrip(), file=sys.stderr)
        raise RuntimeError("live probe candidate validation failed")
    print("LIVE_CANDIDATE_VALIDATION=OK")
    print("LIVE_NODE_COUNT=%d" % len(real_proxies))

    def execute_live_checks():
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if not systemd_service_active("mihomo.service"):
                raise RuntimeError("Mihomo stopped while starting live probe")
            try:
                controller_request(
                    args.controller_port, "GET", "/version", timeout=1
                )
                break
            except (OSError, urllib.error.URLError):
                time.sleep(0.25)
        else:
            raise RuntimeError("live Mihomo controller did not become ready")

        actual = []
        print("LIVE_PROBE_STARTED=YES")
        for index, proxy in enumerate(real_proxies, 1):
            result = {
                "name": proxy["name"],
                "type": proxy.get("type", ""),
                "region": detect_region(proxy["name"]),
            }
            verified = actual_probe(
                args.controller_port,
                int(live_config["mixed-port"]),
                result,
                rounds=args.rounds,
                group_name="CODEX-LIVE-PROBE",
            )
            actual.append(verified)
            print(
                "%02d/%02d. [%s] %s | AUTH=%s HTTP=%s %sms | "
                "CODEX=%s HTTP=%s %sms" % (
                    index,
                    len(real_proxies),
                    result["region"],
                    result["name"],
                    "OK" if verified["auth_ok"] else "FAIL",
                    verified.get("auth_status"),
                    verified.get("auth_ms"),
                    "OK" if verified["codex_ok"] else "FAIL",
                    verified.get("codex_status"),
                    verified.get("codex_ms"),
                )
            )
        return actual

    actual = run_live_transaction(
        production_path,
        live_text,
        execute_live_checks,
    )
    print("PRODUCTION_CONFIG_RESTORED=YES")
    print("MIHOMO_ACTIVE=%s" % str(
        systemd_service_active("mihomo.service")
    ).lower())
    print("GATEWAY_ACTIVE=%s" % str(
        systemd_service_active("hermes-gateway.service")
    ).lower())

    raw_path = config_dir / "codex-live-probe-results.json"
    atomic_write(raw_path, json.dumps({
        "generated_at": time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime()
        ),
        "nodes": actual,
    }, ensure_ascii=False, indent=2) + "\n")

    result_payload = create_region_result(
        actual,
        region_count=args.regions,
        min_usable_nodes=args.min_region_nodes,
    )
    subscription_path = config_dir / "subscription.candidate.yaml"
    result_path = config_dir / "codex-region-candidates.json"
    atomic_write(
        subscription_path,
        yaml.safe_dump(subscription, allow_unicode=True, sort_keys=False),
    )
    atomic_write(result_path, json.dumps(
        result_payload, ensure_ascii=False, indent=2
    ) + "\n")
    print("\nSelected regional fallback groups:")
    for index, region in enumerate(result_payload["selected_regions"], 1):
        print(
            "%02d. %s | usable=%d/%d | median=%dms | "
            "adjusted-score=%dms" % (
                index,
                region["region"],
                region["usable_count"],
                region["tested_count"],
                region["median_ms"],
                region["region_score_ms"],
            )
        )
        for node_name in region["node_names"]:
            print("    - %s" % node_name)
    print("RAW_RESULT_FILE=%s" % raw_path)
    print("RESULT_FILE=%s" % result_path)
    print("FINAL_CANDIDATE_INSTALLED=NO")


def build_candidate_command(args):
    config_dir = pathlib.Path(args.config_dir).expanduser()
    production_path = config_dir / "config.yaml"
    subscription_path = config_dir / "subscription.candidate.yaml"
    result_path = config_dir / "codex-region-candidates.json"
    production = yaml.safe_load(production_path.read_text(encoding="utf-8")) or {}
    subscription = yaml.safe_load(
        subscription_path.read_text(encoding="utf-8")
    ) or {}
    result = json.loads(result_path.read_text(encoding="utf-8"))
    selected_regions = result.get("selected_regions", [])
    candidate = build_candidate_config(
        production,
        subscription.get("proxies", []),
        selected_regions,
    )
    candidate_path = config_dir / "config.yaml.codex-candidate"
    atomic_write(candidate_path, yaml.safe_dump(
        candidate, allow_unicode=True, sort_keys=False
    ))
    print("CANDIDATE=%s" % candidate_path)
    print("PRIMARY=%s" % NEW_MAC_NAME)
    for index, region in enumerate(selected_regions, 1):
        print("REGION_%d=%s" % (index, region["region"]))
        for node_name in region["node_names"]:
            print("  - %s" % node_name)
    print("PRODUCTION_CONFIG_CHANGED=NO")


def parse_args(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--config-dir", default="~/.config/mihomo",
        help="Mihomo configuration directory",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    probe = subparsers.add_parser("probe")
    probe.add_argument("--mihomo", default="mihomo")
    probe.add_argument("--rounds", type=int, default=2)
    probe.add_argument("--regions", type=int, default=2)
    probe.add_argument("--min-region-nodes", type=int, default=2)
    probe.add_argument(
        "--subscription-proxy",
        default="http://127.0.0.1:17890",
        help="fallback proxy used only when direct subscription fetch fails",
    )
    probe.set_defaults(func=probe_command)
    live_probe = subparsers.add_parser("live-probe")
    live_probe.add_argument("--mihomo", default="mihomo")
    live_probe.add_argument("--rounds", type=int, default=2)
    live_probe.add_argument("--regions", type=int, default=2)
    live_probe.add_argument("--min-region-nodes", type=int, default=2)
    live_probe.add_argument("--controller-port", type=int, default=19090)
    live_probe.add_argument(
        "--subscription-proxy",
        default="http://127.0.0.1:17890",
        help="fallback proxy used only when direct subscription fetch fails",
    )
    live_probe.add_argument("--confirm-live", action="store_true")
    live_probe.set_defaults(func=live_probe_command)
    build = subparsers.add_parser("build-candidate")
    build.set_defaults(func=build_candidate_command)
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    try:
        args.func(args)
    except (OSError, ValueError, RuntimeError, yaml.YAMLError) as error:
        print("ERROR: %s" % error, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
