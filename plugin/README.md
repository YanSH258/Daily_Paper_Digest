# daily-papers 插件（v0.1 候选版）

把本地文献工作台接入 AI 客户端：**MCP 服务器 + Skill 工作流指引**。
本文件是自包含的安装与接入说明，不依赖仓库其他文档。

## 支持范围（v0.1）

| 客户端 | 支持方式 | 状态 |
|--------|----------|------|
| ZCode | 本插件完整安装（MCP + Skill） | **v0.1 验收对象** |
| Codex CLI / Claude Desktop / Cursor 等其他 MCP 客户端 | 手动配置 MCP 服务器（见下方"手动接入"），Skill 文件可手动复制 | 仅手动接入，不宣称插件兼容 |

插件清单使用 `.zcode-plugin/plugin.json` 格式；其他客户端不要直接改目录名来"兼容"。

## 1. 安装依赖

要求 Python ≥ 3.10（MCP SDK 需要 3.10+）。

```bash
# 在本仓库目录执行
python -m pip install -r requirement.txt   # 工作台主体依赖
python -m pip install -e ".[mcp]"          # 主体 + MCP SDK（mcp>=1.2,<2）
```

wheel 安装时：`python -m pip install <wheel 文件> "mcp>=1.2,<2"`。
安装后应得到四个命令：`daily-paper-digest`、`daily-paper-web`、
`daily-paper-mcp`、`stat-db`。

## 2. 初始化配置与数据目录

所有个人数据（配置、SQLite 数据库、日志、日报）统一放在**数据根目录**，
由环境变量 `DPD_DATA_ROOT` 指定（启动进程前设置，CLI 与 Web 用同一值；
路径含空格或中文没有问题，建议绝对路径）：

```bash
export DPD_DATA_ROOT="$HOME/dpd-data"     # 任意你喜欢的目录
daily-paper-digest --init-config          # 已有配置不会被覆盖（重复执行报错并保留原文件）
```

然后编辑 `$DPD_DATA_ROOT/config/config.yaml`，最少填：

- `llm.<provider>.api_key` / `base_url` / `model`（OpenAI 兼容接口；也支持
  `DEEPSEEK_API_KEY` 等环境变量）
- `research_topics`（你的研究方向关键词，用于相关性打分）

模板中的密钥/邮箱/webhook 均为占位符，替换成自己的值。
工作台启动时会校验必要配置，缺什么会指出具体字段（如
`配置缺失必填项: llm -> provider`）。**当前启动仍要求模型配置**
（`llm.provider` 与非空 `api_key`）；不过检索、阅读管理、专题、
日报查询等本地操作本身不调用模型，配置一个可用的模型服务主要是
让自动评分、AI 解读与 AI 生成功能工作。

## 3. 启动工作台并认证

```bash
daily-paper-web --config "$DPD_DATA_ROOT/config/config.yaml" --host 127.0.0.1 --port 8080
```

健康检查：`curl http://127.0.0.1:8080/healthz` 返回 `{"ok": true}`。

认证语义：默认写操作需要 Token 时，在「设置」页配置 `web.api_token`；
若开启读取保护（`web.protect_read`），**只读 API 也要求 Token**。
MCP 进程通过环境变量获得这两项配置（见下）。

## 4. 连接 AI 客户端

MCP 通过环境变量找到工作台：

| 变量 | 默认 | 说明 |
|------|------|------|
| `DPD_API` | `http://127.0.0.1:8080` | 工作台地址 |
| `DPD_TOKEN` | 空 | 服务端配置了 `web.api_token` / 读取保护时必填，值需与服务端一致 |
| `DPD_TIMEOUT` | `120` | 单请求超时秒数（上限 600） |

### ZCode（插件安装）

Settings → Plugin Management → Discover → 添加本仓库的 `plugin/`
目录作为 marketplace，安装 `daily-papers`。安装后同时获得 MCP 服务器
（经 PATH 上的 `daily-paper-mcp` 启动）与 Skill。
需要 Token 时在客户端 MCP 配置中给 `daily-papers` 补 `"DPD_TOKEN"` 环境变量
（默认清单不携带，避免 Token 进仓库）。

### 其他客户端（手动接入，未做插件兼容验证）

Claude Desktop（`claude_desktop_config.json`）：

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

Codex CLI（`~/.codex/config.toml`）：

```toml
[mcp_servers.daily-papers]
command = "/path/to/your/venv/bin/daily-paper-mcp"
env = { "DPD_API" = "http://127.0.0.1:8080", "DPD_TOKEN" = "你的Token" }
```

Skill 文件可手动复制到客户端的技能目录
（ZCode：`~/.zcode/skills/daily-papers/SKILL.md`）。

## 5. 验证安装

1. `curl http://127.0.0.1:8080/healthz` → `{"ok": true}`
2. 浏览器打开 `http://127.0.0.1:8080/` 能看到网页控制台
3. 对 AI 客户端说"今天有什么文献"——应调用 `preview_daily_digest` 并按顺序汇报

可选：导入计算材料方向来源预设（MLIP / DFT / 分子动力学，每条带覆盖
方向、已知限制与核验日期，见仓库 `config/source_presets.json`）：

```bash
daily-paper-digest --import-sources <仓库>/config/source_presets.json --dry-run  # 预览
daily-paper-digest --import-sources <仓库>/config/source_presets.json            # 导入（幂等，失败逐条报告）
```

## 6. 常见故障

| 现象 | 处理 |
|------|------|
| MCP 工具报"无法连接文献工作台 …" | 工作台未运行或 `DPD_API` 地址/端口不对；先 `curl …/healthz`，再按第 3 节启动 |
| MCP 工具报 `HTTP 401 认证失败` | 服务端 `web.api_token` 与 MCP 端 `DPD_TOKEN` 不一致（开启读取保护后只读也需要 Token） |
| 启动报"配置缺失必填项: …" | 按报错字段补 `$DPD_DATA_ROOT/config/config.yaml`，不是仓库里的模板 |
| `daily-paper-mcp` 提示缺少 MCP SDK | `pip install "mcp>=1.2,<2"` |
| 工作台返回非 JSON 内容 | `DPD_API` 指向的不是 daily-paper-web 服务，核对地址 |

## 7. 卸载

在客户端中移除插件只会删除插件本身（清单、Skill、MCP 注册项）。
文献库、笔记、标签、专题、日报等用户数据保存在 `DPD_DATA_ROOT`
数据目录，卸载插件不会删除；只有手动删除数据目录才会丢数据。

## 包内容

```
plugin/
├── .zcode-plugin/plugin.json    # 清单：MCP 服务器注册 + Skill 目录
└── skills/daily-papers/SKILL.md # 工作流指引（与 25 个工具一一对应）
```
