#!/usr/bin/env python3
"""Select up to 50 stable, low-latency nodes from a Clash candidate file."""
import argparse
import concurrent.futures
import json
import statistics
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import yaml

URL = "https://www.gstatic.com/generate_204"
TIMEOUT_MS = 5000
API = "http://127.0.0.1:19090"


def get_json(url, timeout=15):
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return json.load(response)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--mihomo", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--report", required=True, type=Path)
    args = parser.parse_args()

    source = yaml.safe_load(args.input.read_text(encoding="utf-8"))
    raw_proxies = source.get("proxies") if isinstance(source, dict) else None
    if not isinstance(raw_proxies, list) or not raw_proxies:
        raise SystemExit("Converter produced no Clash proxy entries; keeping prior publication.")
    proxies = []
    names = set()
    for index, item in enumerate(raw_proxies, 1):
        if not isinstance(item, dict):
            continue
        server, port, kind = item.get("server"), item.get("port"), item.get("type")
        if not isinstance(server, str) or not server.strip() or not isinstance(port, int) or not isinstance(kind, str):
            continue
        node = dict(item)
        label = node.get("name")
        if not isinstance(label, str) or not label.strip():
            label = f"unnamed-{kind}-{server}-{port}"
        label = label.strip()
        base, suffix = label, 2
        while label in names:
            label = f"{base} [{suffix}]"
            suffix += 1
        node["name"] = label
        names.add(label)
        proxies.append(node)
    if not proxies:
        raise SystemExit("No structurally usable named proxies; keeping prior publication.")
    if len(proxies) != len(raw_proxies):
        print(f"Ignored {len(raw_proxies) - len(proxies)} malformed or incomplete candidates.", flush=True)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    data_dir = Path(tempfile.mkdtemp(prefix="airport3-mihomo-"))
    probe_cfg = data_dir / "probe.yaml"
    def valid_batch(batch):
        probe_cfg.write_text(yaml.safe_dump({
            "mixed-port": 17890,
            "allow-lan": False,
            "mode": "rule",
            "log-level": "error",
            "external-controller": "127.0.0.1:19090",
            "proxies": batch,
        }, allow_unicode=True, sort_keys=False, width=120), encoding="utf-8")
        check = subprocess.run([str(args.mihomo), "-t", "-f", str(probe_cfg), "-d", str(data_dir)],
                               capture_output=True, text=True)
        if check.returncode == 0:
            return batch
        diagnostic = check.stdout + "\n" + check.stderr
        if len(batch) == 1:
            print(f"Excluded Mihomo-invalid proxy: {batch[0].get('name')} ({diagnostic.strip()[-300:]})", flush=True)
            return []
        middle = len(batch) // 2
        return valid_batch(batch[:middle]) + valid_batch(batch[middle:])

    # Public aggregation feeds can contain malformed credentials/fields. Validate bounded
    # batches using Mihomo itself, recursively isolating bad entries without repeatedly parsing
    # the entire 10k+ candidate list.
    mihomo_valid = []
    batch_size = 256
    for offset in range(0, len(proxies), batch_size):
        mihomo_valid.extend(valid_batch(proxies[offset:offset + batch_size]))
    removed = len(proxies) - len(mihomo_valid)
    proxies = mihomo_valid
    names = {p["name"] for p in proxies}
    if removed:
        print(f"Excluded {removed} Mihomo-invalid proxies; {len(proxies)} candidates remain.", flush=True)
    if not proxies:
        raise SystemExit("No Mihomo-valid candidate proxies; keeping prior publication.")
    probe_cfg.write_text(yaml.safe_dump({
        "mixed-port": 17890,
        "allow-lan": False,
        "mode": "rule",
        "log-level": "error",
        "external-controller": "127.0.0.1:19090",
        "proxies": proxies,
    }, allow_unicode=True, sort_keys=False, width=120), encoding="utf-8")
    print(f"Mihomo config validation passed for {len(proxies)} candidates.", flush=True)

    log_path = data_dir / "mihomo.log"
    log = log_path.open("w", encoding="utf-8")
    proc = subprocess.Popen([str(args.mihomo), "-f", str(probe_cfg), "-d", str(data_dir)],
                            stdout=log, stderr=subprocess.STDOUT)
    try:
        for _ in range(90):
            if proc.poll() is not None:
                raise RuntimeError(f"Mihomo exited {proc.returncode}: {log_path.read_text(errors='replace')[-5000:]}")
            try:
                version = get_json(API + "/version", timeout=2)
                break
            except Exception:
                time.sleep(1)
        else:
            raise RuntimeError("Mihomo controller did not become ready.")
        loaded = get_json(API + "/proxies", timeout=60).get("proxies", {})
        missing = sorted(n for n in names if n not in loaded)
        if missing:
            print(f"Excluded {len(missing)} config-valid candidates not loaded by Mihomo.", flush=True)
            names.difference_update(missing)
            proxies = [p for p in proxies if p["name"] in names]
        if not names:
            raise RuntimeError("Mihomo loaded no candidate proxies; keeping prior publication.")
        print(f"Mihomo {version.get('version')} loaded {len(names)} candidates; testing {URL} with {TIMEOUT_MS} ms timeout.", flush=True)

        def probe(name):
            endpoint = API + "/proxies/" + urllib.parse.quote(name, safe="") + "/delay?timeout=5000&url=" + urllib.parse.quote(URL, safe="")
            try:
                result = get_json(endpoint, timeout=10)
                delay = result.get("delay")
                if isinstance(delay, int) and delay > 0:
                    return {"name": name, "delay": delay, "ok": True}
                return {"name": name, "delay": None, "ok": False, "error": "no_latency"}
            except urllib.error.HTTPError as exc:
                return {"name": name, "delay": None, "ok": False, "error": f"HTTP_{exc.code}"}
            except Exception as exc:
                return {"name": name, "delay": None, "ok": False, "error": type(exc).__name__}

        started = time.monotonic()
        first = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=128) as pool:
            futures = [pool.submit(probe, name) for name in names]
            for i, future in enumerate(concurrent.futures.as_completed(futures), 1):
                first.append(future.result())
                if i % 1000 == 0 or i == len(futures):
                    print(f"First pass {i}/{len(futures)}; {sum(r['ok'] for r in first)} passed.", flush=True)
        passing = sorted((r for r in first if r["ok"]), key=lambda r: (r["delay"], r["name"]))
        shortlist = passing[:min(150, len(passing))]
        delays = {r["name"]: [r["delay"]] for r in shortlist}
        for round_no in (2, 3):
            rows = []
            with concurrent.futures.ThreadPoolExecutor(max_workers=64) as pool:
                futures = [pool.submit(probe, r["name"]) for r in shortlist]
                for future in concurrent.futures.as_completed(futures):
                    rows.append(future.result())
            for row in rows:
                if row["ok"]:
                    delays[row["name"]].append(row["delay"])
            print(f"Stability pass {round_no}: {sum(r['ok'] for r in rows)}/{len(rows)} passed.", flush=True)

        stable = []
        for row in shortlist:
            samples = delays[row["name"]]
            if len(samples) == 3:
                stable.append({"name": row["name"], "median_ms": statistics.median(samples), "samples_ms": samples})
        stable.sort(key=lambda r: (r["median_ms"], r["name"]))
        selected = stable[:50]
        if not selected:
            raise SystemExit("No nodes passed all three Mihomo probes; keeping prior publication.")
        proxy_by_name = {p["name"]: p for p in proxies}
        selected_names = [r["name"] for r in selected]
        output = {
            "mixed-port": 7890,
            "allow-lan": False,
            "mode": "rule",
            "log-level": "info",
            "proxies": [proxy_by_name[n] for n in selected_names],
            "proxy-groups": [{"name": "PROXY", "type": "select", "proxies": selected_names}],
            "rules": ["GEOIP,CN,DIRECT", "MATCH,PROXY"],
        }
        args.output.write_text(yaml.safe_dump(output, allow_unicode=True, sort_keys=False, width=120), encoding="utf-8")
        report = {
            "mihomo_version": version.get("version"), "target": URL, "timeout_ms": TIMEOUT_MS,
            "input_count": len(mihomo_valid), "mihomo_unloaded_count": len(missing),
            "probe_candidate_count": len(names), "first_pass_count": len(passing),
            "first_failed_count": len(first) - len(passing), "stability_shortlist": len(shortlist),
            "stable_3_of_3": len(stable), "selected_count": len(selected),
            "median_latency_ms": {"min": min(r["median_ms"] for r in selected), "max": max(r["median_ms"] for r in selected)},
            "selected": selected, "elapsed_seconds": round(time.monotonic() - started, 1),
        }
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print("SELECTION " + json.dumps({k: v for k, v in report.items() if k != "selected"}, ensure_ascii=False), flush=True)
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
        log.close()


if __name__ == "__main__":
    main()
