# 部署与加固

示例中 `<LAN_ADDR>` 表示路由器所在主机的局域网地址（文档地址用 `192.0.2.10`），
`user@<LAN_ADDR>` 表示登录该主机的 SSH 目标。

## 拓扑

```
客户端 ──▶ nginx (LAN:8080) ──▶ tavily-router (127.0.0.1:8080) ──▶ api.tavily.com
                 │                       │
                 └ /api/* → 403           └ key 池 / 缓存 / 日志 / 控制台
```

两侧的安全边界：

- **数据面**（`/mcp` 与 Tavily 兼容 REST）对外开放给局域网客户端，需 Bearer 凭据。
- **管理面**（`/api/*`、Web 控制台）只在 loopback；局域网请求被 nginx 直接 403。

## 前置

- 常驻 Linux 主机（x86_64 / arm64 均可，取上游产物的架构），nginx ≥ 1.20，
  Python 3.9+（仅运行仓库里的脚本时需要）。
- 上游路由器程序（本仓库不分发）：从你自己的来源获取，并核对 release 的 `checksums.txt`。
  注意该上游仓库**未声明开源协议**，使用时请自行评估合规性。
- 若希望避免把令牌落到磁盘：macOS 客户端侧用 Keychain，或任何密钥管理器 + 取令牌命令。

## 安装

```bash
# 1) 程序与数据目录
mkdir -p ~/tavily-router/dist ~/tavily-router/data
tar xzf tavily-proxy-<os>-<arch>.tar.gz -C ~/tavily-router/dist
chmod 700 ~/tavily-router/data   # 数据库里有真实上游 key，别让同机其他用户读到

# 2) 服务（unit 里的 LISTEN_ADDR 是 loopback）
cp deploy/systemd/tavily-router.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now tavily-router

# 3) 首次 Master Key（只打印一次）
journalctl --user -u tavily-router | grep -i "master key"

# 4) 把 Master Key 存进密钥管理器（示例：macOS Keychain）
security add-generic-password -a "$USER" -s tavily-router-master-key -w '<master-key>' -A -U

# 5) 加 key 并同步额度（管理面只在 loopback 可用）
MK=$(security find-generic-password -a "$USER" -s tavily-router-master-key -w)
curl -X POST http://127.0.0.1:8080/api/keys -H "Authorization: Bearer $MK" \
  -H 'Content-Type: application/json' \
  -d '{"key":"tvly-...","alias":"account-a","total_quota":1000}'
curl -X POST http://127.0.0.1:8080/api/keys/sync -H "Authorization: Bearer $MK"

# 6) 入口拆分（先改 <LAN_ADDR> 与端口）
sudo cp deploy/nginx/tavily-router.conf /etc/nginx/conf.d/
sudo nginx -t && sudo systemctl reload nginx
```

验证：

```bash
curl -s -o /dev/null -w '%{http_code}\n' http://<LAN_ADDR>:8080/healthz   # 期望 200
curl -s -o /dev/null -w '%{http_code}\n' http://<LAN_ADDR>:8080/api/keys  # 期望 403
```

## 必要的设置项

| 设置 | 建议 | 原因 |
| --- | --- | --- |
| 响应缓存 | 开，TTL 1 小时（仅 `POST /search`） | 重复查询本地命中，不消耗上游额度与限速额度 |
| 额度自动同步 | 开，15 分钟 | 选路基于 `/usage` 真值；`/usage` 每 key 限 10 次/10 分钟，15 分钟间隔远低于上限 |
| 请求日志保留 | 7 天 | 日志会记录 query 与响应正文，缩短保留期可缩小落盘面 |

> 注意：缓存与请求日志共用一个开关（`logging_enabled`）。关掉日志会连带关掉缓存。

## 客户端接入（Codex）

1. 放 helper：把 `deploy/codex/tavily-router-headers.sh` 放到 `~/.codex/bin/` 并 `chmod +x`。
2. 合并 `deploy/codex/config.snippet.toml` 到 `~/.codex/config.toml`（替换 `<LAN_ADDR>`）。
3. 按 `deploy/codex/AGENTS-convention.md` 写入兜底约定。
4. 重启客户端会话（MCP server 在会话启动时加载）。

## 加固清单

- [ ] `config.toml` 里**没有**任何明文令牌（只有 helper 路径）。
- [ ] Master Key 存在密钥管理器，且已从配置文件、备份与会话记录里清除；泄漏过的值要**轮换**。
- [ ] 路由器只监听 `127.0.0.1`，`/api/*` 在 nginx 上 403。
- [ ] `data/` 目录 0700、数据库文件 0600。
- [ ] 入口 `limit_req` 已配置（参数见 `docs/tuning.md`）。
- [ ] 定时健康检查（`scripts/health-check.py`）已挂到告警通道。

## 回滚

| 改动 | 回滚 |
| --- | --- |
| 客户端 MCP 配置 | 还原配置备份，或删掉 `http_headers_helper` 与 `tavily-keyless` 段 |
| 密钥管理器条目 | `security delete-generic-password -s tavily-router-master-key` |
| Master Key 轮换 | `POST /api/settings/master-key/reset`（旧值不可恢复） |
| nginx 入口拆分 | 删除 `/etc/nginx/conf.d/tavily-router.conf` 并 `reload`，把 unit 的 `LISTEN_ADDR` 改回 `:8080` |
| 服务本体 | `systemctl --user disable --now tavily-router` |

## 凭据轮换

```bash
# 1) 生成新 Master Key
NEW=$(curl -sS -X POST http://127.0.0.1:8080/api/settings/master-key/reset \
      -H "Authorization: Bearer $OLD" | python3 -c 'import sys,json;print(json.load(sys.stdin)["master_key"])')
# 2) 覆盖写入密钥管理器
security add-generic-password -a "$USER" -s tavily-router-master-key -w "$NEW" -A -U
# 3) 验证：旧值应 401，新值应 200
```

轮换后客户端无需改配置——helper 每次连接前重新取值，客户端在 401/403 后会自动刷新。
