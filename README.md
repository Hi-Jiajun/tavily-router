# tavily-router

> **English summary** — A thin gateway that pools multiple Tavily API keys behind one endpoint:
> MCP (Streamable HTTP) and Tavily-compatible REST, credit-aware upstream selection, automatic
> failover on 401/429/432/433, response cache, and a loopback-only admin plane. This repository
> ships **deployment config, scripts and docs only** — no binaries, no credentials.

把多个 Tavily API key 汇成一个入口：统一鉴权、按剩余额度选上游、额度/限速错误自动换 key，
并同时对外提供 **MCP（Streamable HTTP）** 与 **Tavily 兼容 REST** 两种接口。

面向的场景：在自己局域网里跑一层薄网关，让 Codex / Claude / Cursor 之类的客户端只连一个地址，
不用把真实 API key 写进各客户端的配置里，也让多把 key 的额度与故障转移变得透明。

> 本仓库只包含**部署配置、脚本与运维文档**，不分发任何上游二进制。

## 架构

```
客户端 (MCP / REST)
   │  单一入口 + Bearer 凭据
   ▼
nginx  (:8080, LAN 侧)
   ├─ /api/*        → 403          管理面不出局域网
   └─ 其它          → 127.0.0.1:8080
                        │
                        ▼
                   tavily-router (仅监听 loopback)
                        ├─ key 池：按剩余额度选上游
                        ├─ 401 / 429 / 432 / 433 自动换 key
                        ├─ 响应缓存（仅 POST /search，TTL 可配）
                        └─ 请求日志 / 用量统计 / Web 控制台
                              │
                              ▼
                      https://api.tavily.com
```

设计要点：

- **凭据不进配置文件**：客户端的 Bearer 令牌由外部命令提供（见 `deploy/codex/`），
  配置文件里只出现 helper 路径。
- **管理面与数据面分离**：路由器只监听 loopback，`/api/*` 由 nginx 挡在局域网之外。
- **入口整形**：nginx `limit_req` 把突发排队而不是直接失败（参数见 `docs/tuning.md`）。

## 快速开始

前置：一台常驻的 Linux 主机（Debian / Ubuntu 等均可，架构随上游产物）、nginx，
以及自行获取的上游预编译二进制（也可自行打包成容器运行）。

1. 自行获取上游程序（本仓库**不分发**它，也不对其做任何担保）。本项目配套的网关实现是
   [xuncv/TavilyProxyManager](https://github.com/xuncv/TavilyProxyManager)：

   ```bash
   # 取 release 产物并核对校验和（请按你自己的来源与版本替换 URL/TAG）
   URL=https://github.com/xuncv/TavilyProxyManager/releases/download/<TAG>
   curl -sSLO "$URL/tavily-proxy-linux-amd64.tar.gz" -O "$URL/checksums.txt"
   sha256sum -c <(grep linux-amd64 checksums.txt)
   ```

   > ⚠️ 该上游仓库**未声明开源协议**（`gh api repos/xuncv/TavilyProxyManager --jq .license`
   > 返回 null），release 也没有签名。校验和只能证明传输完整、不能证明来源可信。
   > 因此本仓库只做**引用与配置**，不复制其代码、不再分发其二进制；
   > 是否使用、以及由此产生的合规责任，由你自行评估与承担（见下方「许可证与第三方」）。

2. 安装为 user 级 systemd 服务：

   ```bash
   mkdir -p ~/tavily-router/data
   cp deploy/systemd/tavily-router.service ~/.config/systemd/user/
   systemctl --user daemon-reload
   systemctl --user enable --now tavily-router
   ```

3. 取首次启动生成的 Master Key（只打印一次，请立刻转存到密钥管理器）：

   ```bash
   journalctl --user -u tavily-router | grep -i "master key"
   ```

4. 加 key 与同步额度（管理面只在 loopback 可用）：

   ```bash
   MK=<master-key>
   curl -X POST http://127.0.0.1:8080/api/keys \
     -H "Authorization: Bearer $MK" -H 'Content-Type: application/json' \
     -d '{"key":"<TAVILY_API_KEY>","alias":"account-a","total_quota":1000}'
   curl -X POST http://127.0.0.1:8080/api/keys/sync -H "Authorization: Bearer $MK"
   ```

5. 入口拆分 + 突发整形：

   ```bash
   sudo cp deploy/nginx/tavily-router.conf /etc/nginx/conf.d/
   sudo nginx -t && sudo systemctl reload nginx
   ```

6. 客户端接入 MCP（以 Codex 为例，见 `deploy/codex/config.snippet.toml`）。

完整步骤、加固清单与回滚见 [`docs/deployment.md`](docs/deployment.md)。

## 运维

```bash
# 服务
systemctl --user status tavily-router
journalctl --user -u tavily-router -n 50

# 池状态 / 额度同步（管理面仅 loopback）
curl -s http://127.0.0.1:8080/api/stats -H "Authorization: Bearer $MK"
curl -X POST http://127.0.0.1:8080/api/keys/sync -H "Authorization: Bearer $MK"

# 健康检查（退出码 0=健康，非 0 可用作告警触发）
TVR_ENTRY=http://<router-host>:8080 TVR_SSH=user@<router-host> scripts/health-check.py

# 入口压测（会消耗上游额度；先用 --dry 看计划）
scripts/loadtest.py --entry http://<router-host>:8080 --waves 2 --concurrency 20
```

Web 控制台不再从局域网直连，用 SSH 隧道访问：

```bash
ssh -L 8080:127.0.0.1:8080 user@<router-host>
# 然后浏览器打开 http://127.0.0.1:8080/
```

## 上游错误码对照

| 码 | 含义 | 网关行为 |
| --- | --- | --- |
| 401 | key 无效/被吊销 | 标记失效并换下一把 |
| 429 | 请求过快 | 换 key 重试；`429` 不计费 |
| 432 | 套餐额度耗尽 | 换 key；池内全耗尽时向客户端透出 432 |
| 433 | PAY-GO 额度耗尽 | 同上 |

## 免责与已知风险

- 上游程序来自第三方仓库且**未声明开源协议**、release 无签名；校验和只能证明传输完整，
  不能证明来源可信。使用前请自行评估。
- **多账号合并额度可能违反 Tavily 的服务条款**（其 API Agreement 明确禁止
  "circumvent or bypass any rate or service limits, including through the creation of
  multiple accounts"）。本仓库只提供网关配置与运维方法，是否使用及其后果由使用者自负；
  用官方免 key 通道或个人付费额度同样可以接入这套网关。
- 本仓库不含任何凭据；示例中的地址均为文档用途地址（RFC 5737）。

## 目录

```
deploy/   入口 nginx 配置、systemd 单元、Codex 客户端接入片段
scripts/  健康检查与压测
docs/     部署与加固、调优记录、限速实测
```

## 许可证与第三方

- **本仓库自己的内容**（配置文件、脚本、文档，即 `deploy/`、`scripts/`、`docs/`、`README.md`）
  采用 **MIT**，见 [`LICENSE`](LICENSE)。你可以自由使用、修改、再分发。
- **上游程序不适用 MIT**：本仓库**不包含、不附带、也不转分发**上游二进制或其源码。
  上游仓库未声明任何开源协议（默认保留所有权利），其授权状态与本仓库无关；
  请自行向来源确认使用许可，风险自负。
- **与本项目无隶属关系**：本仓库不是 Tavily 官方项目，也与上述上游仓库的维护者无隶属或背书关系。
- **商标**：Tavily 及相关名称、标识归其权利人所有，本项目仅在描述互操作性时使用这些名称。
- **合规提示**：把多个账号的免费额度合并使用可能违反 Tavily 的服务条款
  （API Agreement 明确禁止 "circumvent or bypass any rate or service limits, including
  through the creation of multiple accounts"）。用**单个**账号的多把 key、官方免 key 通道，
  或自己的付费额度接入本网关，都是不受该限制影响的用法。

## 安全

发现安全问题请见 [`SECURITY.md`](SECURITY.md)（**不要在 issue 里贴任何真实 API key 或 Master Key**）。
