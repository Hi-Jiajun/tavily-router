#!/usr/bin/python3
"""tavily-router 入口压测 —— 统计 429 比例与延迟分布。

会真实消耗上游额度：每次成功的 /search 计 1 credit，被 429 的请求不计费。
默认每次请求使用唯一 query，避免命中网关缓存（否则测不出上游限速）。

用法：
    scripts/loadtest.py --entry http://192.0.2.10:8080 --waves 2 --concurrency 20
    scripts/loadtest.py --entry ... --same-query     # 测缓存命中
    scripts/loadtest.py --entry ... --dry            # 只打印计划，不发请求

凭据：macOS Keychain（--keychain-service）或任意取令牌命令（--token-cmd）。
"""
import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor


def parse_args():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--entry", default=os.environ.get("TVR_ENTRY", ""),
                   help="入口地址（或环境变量 TVR_ENTRY）")
    p.add_argument("--waves", type=int, default=2, help="轮数（默认 2）")
    p.add_argument("--concurrency", type=int, default=20, help="每轮并发数（默认 20）")
    p.add_argument("--timeout", type=float, default=90, help="单请求超时秒（默认 90）")
    p.add_argument("--same-query", action="store_true", help="每轮用同一个 query（用于验证缓存）")
    p.add_argument("--keychain-service", default=os.environ.get("TVR_KEYCHAIN_SERVICE", "tavily-router-master-key"))
    p.add_argument("--token-cmd", default=os.environ.get("TVR_TOKEN_CMD", ""))
    p.add_argument("--dry", action="store_true", help="只打印计划")
    return p.parse_args()


def get_token(args):
    if args.token_cmd:
        out = subprocess.run(args.token_cmd, shell=True, capture_output=True, text=True)
        return out.stdout.strip() if out.returncode == 0 else ""
    out = subprocess.run(
        ["/usr/bin/security", "find-generic-password", "-a", os.environ.get("USER", ""),
         "-s", args.keychain_service, "-w"],
        capture_output=True, text=True)
    return out.stdout.strip() if out.returncode == 0 else ""


def percentile(values, p):
    if not values:
        return None
    ordered = sorted(values)
    return round(ordered[min(len(ordered) - 1, int(len(ordered) * p))], 2)


def main():
    args = parse_args()
    if not args.entry:
        print("ERR: 需要 --entry 或 TVR_ENTRY", file=sys.stderr)
        return 2

    total_requests = args.waves * args.concurrency
    if args.dry:
        print(json.dumps({"entry": args.entry, "waves": args.waves, "concurrency": args.concurrency,
                          "requests": total_requests,
                          "worst_case_credits": total_requests}, ensure_ascii=False))
        return 0

    token = get_token(args)
    if not token:
        print("ERR: 取不到令牌（Keychain 或 --token-cmd）", file=sys.stderr)
        return 2

    fixed_query = "loadtest " + uuid.uuid4().hex

    def one(_):
        query = fixed_query if args.same_query else "loadtest " + uuid.uuid4().hex
        body = json.dumps({"query": query, "max_results": 1}).encode()
        req = urllib.request.Request(args.entry + "/search", data=body,
                                     headers={"Authorization": "Bearer " + token,
                                              "Content-Type": "application/json"})
        start = time.time()
        try:
            with urllib.request.urlopen(req, timeout=args.timeout) as resp:
                resp.read()
                return resp.status, time.time() - start
        except urllib.error.HTTPError as exc:
            exc.read()
            return exc.code, time.time() - start
        except Exception:  # noqa: BLE001
            return -1, time.time() - start

    codes, latencies = [], []
    for wave in range(1, args.waves + 1):
        started = time.time()
        with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
            results = list(pool.map(one, range(args.concurrency)))
        wave_codes = [c for c, _ in results]
        wave_latency = [round(l, 2) for _, l in results]
        codes += wave_codes
        latencies += wave_latency
        print("  wave %d: %.1fs  状态=%s  最大延迟=%.2fs"
              % (wave, time.time() - started,
                 {str(c): wave_codes.count(c) for c in sorted(set(wave_codes))}, max(wave_latency)),
              flush=True)

    summary = {
        "requests": len(codes),
        "status_counts": {str(c): codes.count(c) for c in sorted(set(codes))},
        "rate_429_pct": round(100.0 * codes.count(429) / len(codes), 1),
        "latency_p50_s": percentile(latencies, 0.50),
        "latency_p95_s": percentile(latencies, 0.95),
    }
    print("  SUMMARY " + json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
