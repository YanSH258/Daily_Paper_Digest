# daily-papers 插件（v0.2）

把本地文献工作台接入 AI 客户端：**MCP 服务器 + Skill 工作流指引**。
本文件是自包含的安装与接入说明，不依赖仓库其他文档。

## 支持范围

| 客户端 | 安装方式 | 状态 |
|--------|----------|------|
| ZCode | 添加市场 → 安装插件（MCP + Skill 一次装齐） | 已实测 |
| Codex CLI (0.152+) | `codex plugin marketplace add` → `codex plugin add` | 已实测 |
| Claude Desktop / Cursor / 其他 MCP 客户端 | 手动配置 MCP 服务器（见第 5 节），Skill 文件手动复制 | 仅手动接入 |

同一份插件目录同时带两套清单：ZCode 读 `.zcode-plugin/plugin.json`，
Codex 读 `.codex-plugin/plugin.json` + `.mcp.json`，Skill 只有一份。

```text
plugin/
├── .zcode-plugin/plugin.json          # ZCode 清单：内联 MCP 服务器 + skills
├── .codex-plugin/plugin.json          # Codex 清单：skills 路径 + 外链 MCP
├── .mcp.json                          # Codex 用的 MCP 服务器声明（stdio）
├── marketplace.json                   # ZCode 市场清单（plugin/ 作为市场）
└── skills/daily-papers/SKILL.md       # 工作流指引（与 25 个工具一一对应）
```

仓库根还有 `.agents/plugins/marketplace.json`（Codex 市场清单）和 `marketplace.json`
（ZCode 市场清单，指向 `plugin/`），用于把**整个仓库**作为市场安装；
两种装法二选一，装出来的插件相同。

## 1. 安装依赖

要求 Python ≥ 3.10（MCP SDK 需要 3.10+）。

```bash
# 在本仓库目录执行
python -m pip install -r requirements.txt   # 工作台主体依赖 + 测试工具
python -m pip install -e ".[mcp]"           # 主体 + MCP SDK（mcp>=1.2,<2）
```

wheel 安装时：`python -m pip install <wheel 文件> "mcp>=1.2,<2"`。
安装后应得到四个命令：`daily-paper-digest`、`daily-paper-web`、
`daily-paper-mcp`、`stat-db`。**插件清单里 MCP 命令写的是裸命令名
`daily-paper-mcp`，所以它必须在 PATH 上**；用 venv 安装时记得先激活 venv，
或改用绝对路径（见第 5 节）。

## 2. 初始化配置与数据目录

所有个人数据（配置、SQLite 数据库、日志、日报）统一放在**数据根目录**。
推荐直接使用绝对配置路径：当路径是 `<root>/config/config.yaml` 时，程序会自动把
`<root>` 作为数据根目录，CLI 与 Web 后续只需传同一个配置路径；环境变量
`DPD_DATA_ROOT` 仍可用于 cron、Docker 或强制覆盖（优先级最高）：

```bash
mkdir -p "$HOME/dpd-data/config"
daily-paper-digest --init-config --config "$HOME/dpd-data/config/config.yaml"
```

然后编辑 `$HOME/dpd-data/config/config.yaml`，最少填：

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
daily-paper-web --config "$HOME/dpd-data/config/config.yaml" --host 127.0.0.1 --port 8080
```

健康检查：`curl http://127.0.0.1:8080/healthz` 返回 `{"ok": true}`。

认证语义：默认写操作需要 Token 时，在「设置」页配置 `web.api_token`；
若开启读取保护（`web.protect_read`），**只读 API 也要求 Token**。
MCP 进程通过环境变量获得这两项配置（见第 6 节）。

## 4. 作为插件安装

### 4.1 ZCode

**方式 A：装 `plugin/` 目录（插件市场就是插件本体）**

1. Settings → Plugin Management → Discover → 添加市场，源选本仓库的 `plugin/` 目录；
2. 在市场里安装 `daily-papers`。

装好后同时获得 MCP 服务器（由 PATH 上的 `daily-paper-mcp` 启动）与 Skill。

**方式 B：装整个仓库**

添加市场时选**仓库根目录**（根目录的 `marketplace.json` 指向 `plugin/`）。
两种方式装出来的插件内容相同，选一种即可，不要同时添加两个市场。

需要 Token 时，在客户端 MCP 配置里给 `daily-papers` 补 `DPD_TOKEN` 环境变量
（清单默认不携带 Token，避免 Token 进仓库）。

升级：`plugin/` 内容变化后需要**重新安装**（ZCode 装的是安装时刻的缓存副本，
不是指向仓库的软链），重新安装后版本号显示为清单里的 `version`。

### 4.2 Codex CLI

```bash
# 添加市场：本地路径或 GitHub 简写均可
codex plugin marketplace add /path/to/Daily_Paper_Digest
# 或：codex plugin marketplace add YanSH258/Daily_Paper_Digest

codex plugin list                        # 应看到 daily-papers@daily-papers
codex plugin add daily-papers@daily-papers
codex plugin list                        # 状态应为 installed, enabled
codex mcp list                           # 应看到 daily-papers 这条 MCP 服务器
```

实测输出（Codex 0.152.0）：

```text
Added marketplace `daily-papers` from /path/to/Daily_Paper_Digest.
Added plugin `daily-papers` from marketplace `daily-papers`.
Installed plugin root: $CODEX_HOME/plugins/cache/daily-papers/daily-papers/0.2.0

PLUGIN                     STATUS              VERSION  PATH
daily-papers@daily-papers  installed, enabled  0.2.0    /path/to/Daily_Paper_Digest/plugin

Name          Command          Args  Env            Status   Auth
daily-papers  daily-paper-mcp  -     DPD_API=*****  enabled  Unsupported
```

`codex mcp list` 里 `Auth` 显示 `Unsupported` 是正常的（本地 stdio 服务器不需要
OAuth）；`Env` 只回显掩码。

升级：`codex plugin marketplace upgrade daily-papers` 后重新
`codex plugin add daily-papers@daily-papers`（本地路径市场直接重新 add 一次即可）。

### 4.3 环境变量覆盖

两个客户端都支持在客户端侧覆盖 MCP 进程的环境变量（ZCode 在 MCP 配置里，
Codex 用 `~/.codex/config.toml` 的同名小节，会覆盖插件提供的同名服务器）：

```toml
[mcp_servers.daily-papers]
command = "daily-paper-mcp"
env = { "DPD_API" = "http://127.0.0.1:8080", "DPD_TOKEN" = "你的Token" }
```

## 5. 其他客户端（手动接入）

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

Codex CLI 也可以不用插件、直接写 `~/.codex/config.toml`：

```toml
[mcp_servers.daily-papers]
command = "/path/to/your/venv/bin/daily-paper-mcp"
env = { "DPD_API" = "http://127.0.0.1:8080", "DPD_TOKEN" = "你的Token" }
```

Skill 文件可手动复制到客户端的技能目录
（ZCode：`~/.zcode/skills/daily-papers/SKILL.md`，
Codex：`~/.codex/skills/daily-papers/SKILL.md`）。

## 6. MCP 环境变量

| 变量 | 默认 | 说明 |
|------|------|------|
| `DPD_API` | `http://127.0.0.1:8080` | 工作台地址 |
| `DPD_TOKEN` | 空 | 服务端配置了 `web.api_token` / 读取保护时必填，值需与服务端一致 |
| `DPD_TIMEOUT` | `120` | 单请求超时秒数（上限 600） |

## 7. 验证安装

1. `curl http://127.0.0.1:8080/healthz` → `{"ok": true}`
2. 浏览器打开 `http://127.0.0.1:8080/` 能看到网页控制台
3. 直接探一次 MCP 服务器（不依赖客户端，判断是插件问题还是服务问题）：

```bash
printf '%s\n' \
  '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"probe","version":"0"}}}' \
  '{"jsonrpc":"2.0","method":"notifications/initialized"}' \
  '{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}' \
  | DPD_API=http://127.0.0.1:8080 daily-paper-mcp
```

应输出 `serverInfo: daily-papers` 与 25 个工具。
4. 对 AI 客户端说"今天有什么文献"——应调用 `preview_daily_digest` 并按顺序汇报

可选：导入计算材料方向来源预设（MLIP / DFT / 分子动力学，每条带覆盖
方向、已知限制与核验日期，见仓库 `config/source_presets.json`）：

```bash
daily-paper-digest --import-sources <仓库>/config/source_presets.json --dry-run  # 预览
daily-paper-digest --import-sources <仓库>/config/source_presets.json            # 导入（幂等，失败逐条报告）
```

订阅增删后可以把数据库里的清单导回预设文件（只写来源元数据，不含密钥与邮箱）：

```bash
daily-paper-digest --export-sources <仓库>/config/source_presets.json --dry-run  # 只列清单
daily-paper-digest --export-sources <仓库>/config/source_presets.json            # 写回文件
```

## 8. 常见故障

| 现象 | 处理 |
|------|------|
| MCP 工具报"无法连接文献工作台 …" | 工作台未运行或 `DPD_API` 地址/端口不对；先 `curl …/healthz`，再按第 3 节启动 |
| MCP 工具报 `HTTP 401 认证失败` | 服务端 `web.api_token` 与 MCP 端 `DPD_TOKEN` 不一致（开启读取保护后只读也需要 Token） |
| 客户端里看不到 MCP 服务器 | 先跑第 7 节第 3 步的探测命令：能返回工具说明插件和服务都正常，问题在客户端侧环境变量；报"命令不存在"则是 `daily-paper-mcp` 不在 PATH（改绝对路径） |
| 插件装了但 Skill 没触发 | 确认安装后客户端里有 `daily-papers` 技能；Codex 可看 `codex plugin list` 的 STATUS 是否为 enabled |
| 改了仓库里的 `plugin/` 但客户端没变化 | 插件是安装时的缓存副本，需重新安装/升级（ZCode 重新安装，Codex 重新 `plugin add`） |
| 启动报"配置缺失必填项: …" | 按报错字段补 `<数据根>/config/config.yaml`，不是仓库里的模板 |
| `daily-paper-mcp` 提示缺少 MCP SDK | `pip install "mcp>=1.2,<2"` |
| 工作台返回非 JSON 内容 | `DPD_API` 指向的不是 daily-paper-web 服务，核对地址 |

## 9. 卸载

在客户端中移除插件只会删除插件本身（清单、Skill、MCP 注册项）。
文献库、笔记、标签、专题、日报等用户数据保存在数据根目录，
卸载插件不会删除；只有手动删除数据目录才会丢数据。

Codex 下：`codex plugin remove daily-papers@daily-papers`，
再 `codex plugin marketplace remove daily-papers`。
