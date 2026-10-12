# 建议写进客户端全局说明的兜底约定

把下面这段贴到 Codex 的 `~/.codex/AGENTS.md`（或等价的项目说明）里，
让模型在网关额度耗尽时自动切换而不是反复重试：

```markdown
## Tavily 搜索链路

`tavily` 是自建网关（key 池 + 额度选路 + 401/429/432/433 自动换 key）。

**额度耗尽时的兜底约定**：当 `tavily` 返回 432（套餐额度耗尽）、433（PAYGO 额度耗尽）
或持续 429 时，改用 `tavily-keyless`（官方免 key 远程 MCP，工具名带下划线：
`tavily_search` / `tavily_extract`）继续完成任务，不要对同一个工具反复重试。
```

说明：`mcp_servers.<id>.developer_instructions` 在实测的 Codex 版本里**不生效**
（模型上下文看不到该字段），因此约定要写在 AGENTS.md 这类一定会注入的说明里。
