#!/usr/bin/python3
"""tavily-router 健康检查 —— 适合作为定时告警的触发点。

检查项：入口可达性、服务存活、池内剩余额度、key 有效性、额度同步新鲜度。
退出码：0=健康；1=有问题（stdout 给出可读原因）；2=参数/凭据错误。

用法：
    TVR_ENTRY=http://192.0.2.10:8080 TVR_SSH=user@192.0.2.10 scripts/health-check.py
    scripts/health-check.py --entry ... --ssh ... --min-ratio 0.2 --json

管理面（/api/*）只在路由器 loopback 上开放，所以池状态经 --ssh 到目标主机再用
curl 打 127.0.0.1 获取；不给 --ssh 时跳过管理面检查（仍校验入口可达性）。
凭据来源：macOS Keychain（--keychain-service）或任意取令牌命令（--token-cmd）。
"""
import argparse
import datetime
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request


def parse_args():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--entry", default=os.environ.get("TVR_ENTRY", ""),
                   help="入口地址，例如 http://192.0.2.10:8080（或环境变量 TVR_ENTRY）")
    p.add_argument("--ssh", default=os.environ.get("TVR_SSH", ""),
                   help="管理面所在主机，例如 user@192.0.2.10（或环境变量 TVR_SSH）")
    p.add_argument("--keychain-service", default=os.environ.get("TVR_KEYCHAIN_SERVICE", "tavily-router-master-key"),
                   help="macOS Keychain 条目名")
    p.add_argument("--token-cmd", default=os.environ.get("TVR_TOKEN_CMD", ""),
                   help="取 Master Key 的命令（优先于 Keychain）")
    p.add_argument("--min-ratio", type=float, default=float(os.environ.get("TVR_MIN_RATIO", 0.20)),
                   help="剩余额度低于总额度的该比例即告警（默认 0.20）")
    p.add_argument("--max-sync-age-min", type=float, default=float(os.environ.get("TVR_MAX_SYNC_AGE_MIN", 90)),
                   help="额度同步超过该分钟数未成功即告警（默认 90）")
    p.add_argument("--json", action="store_true", help="以 JSON 输出结果")
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


def http_code(url, timeout=10):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return resp.status
    except urllib.error.HTTPError as exc:
        return exc.code
    except Exception:  # noqa: BLE001
        return 0


def remote_api(ssh_target, token, path):
    """经 ssh 在目标主机上走 loopback 调管理接口。"""
    cmd = "curl -sS -m 10 http://127.0.0.1:8080%s -H 'Authorization: Bearer %s'" % (path, token)
    try:
        out = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", ssh_target, cmd],
                             capture_output=True, text=True, timeout=20)
    except subprocess.TimeoutExpired:
        return None
    if out.returncode != 0:
        return None
    try:
        return json.loads(out.stdout)
    except json.JSONDecodeError:
        return None


def main():
    args = parse_args()
    if not args.entry:
        print("ERR: 需要 --entry 或 TVR_ENTRY", file=sys.stderr)
        return 2

    problems, notes = [], []

    if http_code(args.entry + "/healthz") != 200:
        problems.append("入口 /healthz 不通（nginx 或主机不可达）")

    if args.ssh:
        token = get_token(args)
        if not token:
            print("ERR: 取不到 Master Key（Keychain 或 --token-cmd）", file=sys.stderr)
            return 2

        stats = remote_api(args.ssh, token, "/api/stats")
        if stats is None:
            problems.append("管理接口不可达（服务可能已挂，或 ssh 失败）")
        else:
            total = stats.get("total_quota", 0)
            remaining = stats.get("total_remaining", 0)
            notes.append("池子剩余 %s/%s，key %s 个（活跃 %s）"
                         % (remaining, total, stats.get("key_count"), stats.get("active_key_count")))
            if total and remaining / total < args.min_ratio:
                problems.append("剩余额度 %s/%s 低于 %.0f%% 阈值" % (remaining, total, args.min_ratio * 100))
            if not stats.get("active_key_count"):
                problems.append("池内没有可用的活跃 key")

        keys = remote_api(args.ssh, token, "/api/keys")
        if isinstance(keys, list):
            bad = [k.get("alias") for k in keys if k.get("is_invalid") or not k.get("is_active")]
            if bad:
                problems.append("存在失效/停用 key: " + ", ".join(str(b) for b in bad))

        sync = remote_api(args.ssh, token, "/api/settings/auto-sync")
        if isinstance(sync, dict):
            if not sync.get("enabled"):
                notes.append("额度自动同步处于关闭状态")
            last = sync.get("last_success_at") or sync.get("last_run_at")
            if last:
                try:
                    ts = datetime.datetime.fromisoformat(last.replace("Z", "+00:00"))
                    age = (datetime.datetime.now(datetime.timezone.utc) - ts).total_seconds() / 60
                    notes.append("上次同步成功 %.0f 分钟前" % age)
                    if age > args.max_sync_age_min:
                        problems.append("额度同步已 %.0f 分钟未成功（阈值 %.0f）" % (age, args.max_sync_age_min))
                except ValueError:
                    pass
    else:
        notes.append("未提供 --ssh，跳过管理面检查（池状态/同步新鲜度未验证）")

    if args.json:
        print(json.dumps({"ok": not problems, "problems": problems, "notes": notes}, ensure_ascii=False))
    else:
        for line in notes:
            print("  · " + line)
        if problems:
            print("FAIL:")
            for item in problems:
                print("  - " + item)
        else:
            print("OK: tavily-router 健康")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
