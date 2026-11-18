#!/usr/bin/env python3
"""Recheck Airport3's current selected feed using Airport1's actual Mihomo criteria.

One Mihomo /delay request per candidate, http://www.gstatic.com/generate_204,
5000 ms cutoff. Failed candidates are excluded. On zero passes, keep the published
files untouched and fail the workflow. Successful output follows Airport1's group
and routing template, but keeps Airport3's source identity.
"""
from __future__ import annotations

import concurrent.futures
import hashlib
import os
import random
import re
import shutil
import socket
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote
from zoneinfo import ZoneInfo

import requests
import yaml

ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "dist"
INPUT = DIST / "clash-selected.yaml"
TEST_URL = "http://www.gstatic.com/generate_204"
TIMEOUT_MS = 5000
MAX_WORKERS = 24


def detect_region(name: str) -> str:
    text = name.lower()
    patterns = {
        "HK": (r"\bhk\b", "hong kong", "香港", "🇭🇰"),
        "JP": (r"\bjp\b", "japan", "日本", "🇯🇵"),
        "US": (r"\bus\b", r"\busa\b", "united states", "america", "美国", "美國", "🇺🇸"),
        "SG": (r"\bsg\b", "singapore", "新加坡", "🇸🇬"),
    }
    for region, tokens in patterns.items():
        for token in tokens:
            if token.startswith(r"\b"):
                if re.search(token, text):
                    return region
            elif token in text:
                return region
    return "OTHER"


def region_bonus(region: str) -> int:
    if region in {"HK", "SG", "JP"}:
        return 3
    if region == "US":
        return 2
    return 1


def health_score(name: str, latency: int, region: str) -> float:
    seed = int(hashlib.sha256(name.encode("utf-8")).hexdigest()[:12], 16)
    stability = random.Random(seed).random()
    return (1 / latency) * 0.6 + region_bonus(region) * 0.3 + stability * 0.1


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def wait_controller(base: str, process: subprocess.Popen, timeout: float = 30) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError("Mihomo exited before its controller became ready")
        try:
            response = requests.get(f"{base}/version", timeout=1)
            if response.status_code == 200:
                return
        except requests.RequestException:
            pass
        time.sleep(0.5)
    raise RuntimeError("Mihomo controller did not become ready")


def test_one(base: str, proxy: dict) -> tuple[dict | None, str | None]:
    name = str(proxy["name"])
    url = f"{base}/proxies/{quote(name, safe='')}/delay"
    try:
        response = requests.get(
            url,
            params={"timeout": str(TIMEOUT_MS), "url": TEST_URL},
            timeout=(TIMEOUT_MS / 1000) + 3,
        )
        if response.status_code != 200:
            return None, f"HTTP {response.status_code}"
        delay = int(response.json().get("delay", 0))
        if delay <= 0 or delay > TIMEOUT_MS:
            return None, "no valid delay"
        return {"proxy": proxy, "latency": delay, "region": detect_region(name),
                "health_score": health_score(name, delay, detect_region(name))}, None
    except Exception as exc:
        return None, type(exc).__name__


def names_for_region(metrics: list[dict], region: str) -> list[str]:
    names = [item["proxy"]["name"] for item in metrics if item["region"] == region]
    if names:
        return names
    if metrics:
        # Match Airport1's fallback for an empty region pool; names do not prove location.
        return [item["proxy"]["name"] for item in metrics[:min(5, len(metrics))]]
    return ["DIRECT"]


def low_latency_pool(metrics: list[dict]) -> list[str]:
    if not metrics:
        return ["DIRECT"]
    ordered = sorted(metrics, key=lambda item: (item["latency"], -item["health_score"]))
    size = min(max(3, len(ordered) // 5), 30, len(ordered))
    return [item["proxy"]["name"] for item in ordered[:size]]


def build_config(metrics: list[dict]) -> dict:
    metrics = sorted(metrics, key=lambda item: item["health_score"], reverse=True)
    proxies = [item["proxy"] for item in metrics]
    names = [item["proxy"]["name"] for item in metrics]
    return {
        "mixed-port": 7890,
        "allow-lan": True,
        "mode": "rule",
        "log-level": "info",
        "ipv6": True,
        "unified-delay": True,
        "tcp-concurrent": True,
        # Current Mihomo has removed Airport1's old global-client-fingerprint key.
        # Omitting it avoids carrying a removed option into the repaired profile.
        "generated-by": "airport3-freeproxyhub-airport1-style",
        "generated-at": datetime.now(timezone.utc).isoformat(),
        "proxies": proxies,
        "proxy-groups": [
            {"name": "AUTO-FAST", "type": "url-test", "proxies": names,
             "url": TEST_URL, "interval": 120},
            {"name": "HK-POOL", "type": "url-test", "proxies": names_for_region(metrics, "HK"),
             "url": TEST_URL, "interval": 120},
            {"name": "JP-POOL", "type": "url-test", "proxies": names_for_region(metrics, "JP"),
             "url": TEST_URL, "interval": 120},
            {"name": "US-POOL", "type": "url-test", "proxies": names_for_region(metrics, "US"),
             "url": TEST_URL, "interval": 120},
            {"name": "AI-POOL", "type": "url-test", "proxies": low_latency_pool(metrics),
             "url": TEST_URL, "interval": 120},
            {"name": "FALLBACK", "type": "fallback",
             "proxies": ["AUTO-FAST", "HK-POOL", "JP-POOL", "US-POOL"],
             "url": TEST_URL, "interval": 120},
            {"name": "PROXY", "type": "select", "proxies": ["AUTO-FAST", "FALLBACK"]},
        ],
        "rules": [
            "DOMAIN-SUFFIX,openai.com,AI-POOL",
            "DOMAIN-SUFFIX,chatgpt.com,AI-POOL",
            "DOMAIN-SUFFIX,claude.ai,AI-POOL",
            "DOMAIN-SUFFIX,anthropic.com,AI-POOL",
            "GEOIP,CN,DIRECT",
            "MATCH,PROXY",
        ],
    }


def validate_with_mihomo(config: dict, binary: Path) -> None:
    with tempfile.TemporaryDirectory(prefix="airport3-airport1-style-") as td:
        directory = Path(td)
        path = directory / "config.yaml"
        path.write_text(yaml.safe_dump(config, allow_unicode=True, sort_keys=False), encoding="utf-8")
        result = subprocess.run([str(binary), "-d", td, "-f", str(path), "-t"],
                                capture_output=True, text=True, timeout=180)
        if result.returncode != 0:
            raise RuntimeError("generated Clash YAML failed Mihomo validation")


def main() -> None:
    if not INPUT.is_file() or INPUT.is_symlink() or INPUT.stat().st_size == 0:
        raise SystemExit("selected input feed is missing or invalid")
    source = yaml.safe_load(INPUT.read_text(encoding="utf-8"))
    proxies = source.get("proxies") if isinstance(source, dict) else None
    if not isinstance(proxies, list) or not proxies:
        raise SystemExit("selected feed contains no proxy candidates")
    if any(not isinstance(p, dict) or not p.get("name") or not p.get("server") or not p.get("port")
           for p in proxies):
        raise SystemExit("selected feed has an incomplete proxy")
    if len({str(p["name"]) for p in proxies}) != len(proxies):
        raise SystemExit("selected feed contains duplicate proxy names")

    binary = Path(os.environ.get("MIHOMO_BIN") or shutil.which("mihomo") or "")
    if not binary.is_file():
        raise SystemExit("Mihomo binary unavailable; refusing to publish an untested feed")

    controller_port = free_port()
    base = f"http://127.0.0.1:{controller_port}"
    with tempfile.TemporaryDirectory(prefix="airport3-airport1-probe-") as td:
        directory = Path(td)
        probe_config = {"log-level": "silent", "mode": "direct",
                        "external-controller": f"127.0.0.1:{controller_port}",
                        "proxies": proxies}
        config_path = directory / "config.yaml"
        config_path.write_text(yaml.safe_dump(probe_config, allow_unicode=True), encoding="utf-8")
        process = subprocess.Popen([str(binary), "-d", td, "-f", str(config_path)],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            wait_controller(base, process)
            workers = max(1, min(MAX_WORKERS, len(proxies)))
            with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
                results = list(executor.map(lambda proxy: test_one(base, proxy), proxies))
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()

    metrics = [metric for metric, _ in results if metric is not None]
    failures = [reason for metric, reason in results if metric is None]
    if not metrics:
        raise SystemExit(f"Airport1-style test: 0/{len(proxies)} passed; keeping published files unchanged")

    config = build_config(metrics)
    validate_with_mihomo(config, binary)
    shanghai_date = datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()
    dated = DIST / f"clash-{shanghai_date}.yaml"
    DIST.mkdir(parents=True, exist_ok=True)
    payload = yaml.safe_dump(config, allow_unicode=True, sort_keys=False)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=DIST, delete=False) as temp:
        temp.write(payload)
        temp_path = Path(temp.name)
    # Replace only after test and final Mihomo validation both pass.
    temp_path.replace(INPUT)
    temp2 = DIST / f".{dated.name}.tmp"
    temp2.write_text(payload, encoding="utf-8")
    temp2.replace(dated)
    delays = [item["latency"] for item in metrics]
    print(f"Airport1-style check: {len(metrics)}/{len(proxies)} passed; "
          f"{len(failures)} failed; latency={min(delays)}–{max(delays)} ms; "
          f"date={shanghai_date}; wrote {INPUT.name} and {dated.name}")


if __name__ == "__main__":
    main()
