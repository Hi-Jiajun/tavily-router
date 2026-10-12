# 安全策略

## 报告问题

如果发现与本仓库有关的安全问题（配置默认值不安全、helper 泄露令牌、脚本会把凭据写盘等），
请优先通过 GitHub 的 [Private vulnerability reporting](https://docs.github.com/en/code-security/security-advisories/guidance-on-reporting-and-writing-information-about-vulnerabilities/privately-reporting-a-security-vulnerability)
提交，或在 issue 里**只描述问题、不附带任何真实凭据**。

## 本项目处理凭据的约定

- 仓库内**不含任何凭据**：示例地址使用文档用途地址（RFC 5737），令牌一律经外部密钥管理器获取。
- 客户端侧令牌通过 `http_headers_helper` 从 Keychain 等密钥管理器注入，
  配置文件里只出现 helper 路径。
- 网关的管理面（`/api/*`，含导出全部上游 key 的接口）只监听 loopback，
  局域网侧由 nginx 直接 403。

## 使用者需要注意的几点

1. **Master Key 等同于全部上游 key 的读取权**：它既能走数据面，也能调管理面导出 key 池。
   请把它当作高价值凭据（存密钥管理器、不入库、不贴聊天记录/截图）。
   一旦泄漏：先轮换（`POST /api/settings/master-key/reset`），再确认旧值已失效。
2. **请求日志会记录 query 与响应正文**（默认保留 7 天），其中可能含业务敏感内容；
   注意保留期设置，并确保数据库文件权限为 0600、目录为 0700。
3. **缓存与日志共用一个开关**：关闭日志会连带关闭缓存，反之亦然。
4. **不要把示例中的文档地址直接上线**：部署时替换为你的实际入口地址，并确认没有把
   管理面暴露到局域网或公网。
