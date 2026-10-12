#!/bin/sh
# 为 Codex 的 tavily MCP server 输出 HTTP 头 JSON（http_headers_helper 协议）。
# 令牌存在 macOS Keychain，本文件不含凭据。
#
# 准备：security add-generic-password -a "$USER" -s tavily-router-master-key -w '<token>' -A -U
# 说明：-A 允许任意本地进程读取以避免后台调用被交互弹窗卡死；
#       若要更严，可去掉 -A，改用 -T <允许的二进制> 精确定义 ACL。
#
# 非 macOS 环境：把下面的取令牌命令换成你的密钥管理器（如 pass / sops / 1Password CLI）。
set -e

KEYCHAIN_SERVICE="${TVR_KEYCHAIN_SERVICE:-tavily-router-master-key}"
export KEYCHAIN_SERVICE

exec /usr/bin/python3 - <<'PY'
import json, os, subprocess, sys

service = os.environ["KEYCHAIN_SERVICE"]
try:
    proc = subprocess.run(
        ["/usr/bin/security", "find-generic-password", "-a", os.environ.get("USER", ""),
         "-s", service, "-w"],
        capture_output=True, text=True, check=True)
except subprocess.CalledProcessError as exc:
    sys.stderr.write("keychain lookup failed: %s\n" % (exc.stderr or "").strip())
    sys.exit(1)

token = proc.stdout.strip()
if not token:
    sys.stderr.write("empty token in keychain item %s\n" % service)
    sys.exit(1)

print(json.dumps({"Authorization": "Bearer " + token}))
PY
