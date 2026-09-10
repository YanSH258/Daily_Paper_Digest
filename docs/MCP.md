# MCP 服务器：把文献工作台接入 AI Agent

`daily-paper-mcp` 把工作台的 HTTP API 包装成 **21 个语义化 MCP 工具**，
让 ZCode / Codex / Claude Desktop / Cursor 等任何支持 MCP 的 AI agent
直接操作你的文献库——检索、阅读管理、研究专题、引文追踪、Zotero 推送、AI 对比。

## 前置条件

```bash
pip install -e ".[mcp]"          # 安装 MCP SDK（可选依赖，锁定 mcp 1.x）
daily-paper-web --config config/config.yaml --port 8080   # 工作台服务需在运行
```

## 工具一览

| 类别 | 工具 | 说明 |
|------|------|------|
| 检索 | `search_papers` / `get_paper` | 关键词+多维筛选；详情默认不含全文（按需返回，保护 agent 上下文） |
| 精选 | `today_top_n` | 每日 Top-N（digest 模块），支持 dry-run 预览与正式落盘 |
| 阅读 | `set_reading_status` / `star_paper` / `add_note` / `add_tags` | 待读/在读/已读、收藏、笔记、标签 |
| 专题 | `list_topics` / `get_topic` / `create_topic` / `add_papers_to_topic` | 研究专题管理 |
| 集成 | `push_to_zotero` / `watch_paper` / `watch_author` | Zotero 推送（DOI 幂等）、引文/作者追踪 |
| 任务 | `run_pipeline` / `task_status` / `list_reports` | 触发流水线、轮询状态（含 LLM token 用量）、报告列表 |
| 统计 | `trends` | 月度入库、期刊相关率、发现来源、阅读分布 |
| 生成 | `compare_papers` / `related_work_draft` / `chat_with_paper` | LLM 高成本工具，docstring 声明仅在用户明确要求时调用 |

## 配置（环境变量）

| 变量 | 默认 | 说明 |
|------|------|------|
| `DPD_API` | `http://127.0.0.1:8080` | 工作台地址 |
| `DPD_TOKEN` | 空 | 写操作 Token（服务端启用 `web.api_token` 时必填） |
| `DPD_TIMEOUT` | `120` | 单请求超时秒数（LLM 生成类工具耗时较长） |

## ZCode

用户级 MCP（所有工作区可用）：编辑 `~/.zcode/cli/config.json`：

```json
{
  "mcp": {
    "servers": {
      "daily-papers": {
        "command": "/path/to/your/venv/bin/daily-paper-mcp",
        "env": { "DPD_API": "http://127.0.0.1:8080", "DPD_TOKEN": "你的Token" }
      }
    }
  }
}
```

技能（工作流指引：晨间简报/周回顾/整理仪式）安装到 `~/.zcode/skills/daily-papers/SKILL.md`
（源文件在本仓库 `plugin/skills/daily-papers/SKILL.md`）。

也可以整目录分发：ZCode → Settings → Plugin Management → Discover → 添加本仓库 `plugin/`
目录作为 marketplace，即可同时安装 MCP 服务器与技能。

## Codex CLI

`~/.codex/config.toml`：

```toml
[mcp_servers.daily-papers]
command = "/path/to/your/venv/bin/daily-paper-mcp"
env = { "DPD_API" = "http://127.0.0.1:8080", "DPD_TOKEN" = "你的Token" }
```

## Claude Desktop

`claude_desktop_config.json`：

```json
{
  "mcpServers": {
    "daily-papers": {
      "command": "/path/to/your/venv/bin/daily-paper-mcp",
      "env": { "DPD_API": "http://127.0.0.1:8080", "DPD_TOKEN": "你的Token" }
    }
  }
}
```

## 验证安装

对 agent 说"今天有什么文献"——它应调用 `today_top_n` 并按优先级汇报。
或手动验证：

```bash
DPD_API=http://127.0.0.1:8080 python tests/mcp_e2e.py http://127.0.0.1:8080
```

## 设计边界

- MCP 层只做协议转换与上下文裁剪，业务规则（状态机/去重/防覆盖）全部留在工作台服务
- 高成本工具在 docstring 声明调用约束；`tests/mcp_flows.py` 审计各工具返回体积
- 写操作与读操作遵循服务端统一的 Token 语义（含 `protect_read`）
