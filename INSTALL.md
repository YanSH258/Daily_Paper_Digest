# 安装与配置完整指南

Daily Paper Digest（AI 化学/材料文献日报与个人文献工作台）。
覆盖三种使用方式：**网页控制台**（推荐，所有系统）、**命令行 CLI**、**AI Agent 插件**（ZCode / Codex / Claude Desktop，可选）。

---

## 0. 系统要求与方式选择

| 项 | 要求 |
|----|------|
| Python | ≥ 3.10（MCP 插件需要；只跑主体 3.9+ 亦可，建议直接 3.10+） |
| 系统 | macOS 12+ / Windows 10+ / 主流 Linux |
| 网络 | 需访问期刊 RSS / arXiv / OpenAlex；LLM API 按你配置的提供方 |
| 磁盘 | 数据目录几百 MB 起（含全文存档后增长） |

| 你想怎么用 | 需要装什么 |
|-----------|-----------|
| 浏览器看文献库/日报，点按钮操作 | 主体服务（第 1-4 章） |
| 命令行跑流水线/周报/备份 | 同上（CLI 随主体一起装好） |
| 对 AI agent 说"今天读什么"让它替你操作 | 主体 + MCP 插件（第 6 章） |

> **当前默认运行模式（粗筛优先）**：抓取 → LLM 相关性评分 → 标题自动翻译（免费）→ 生成日报。
> **不**自动抓全文、**不**自动 AI 解读。看中哪篇再在页面上点「📥 取全文并解读」或「推送 Zotero」（自动挂 PDF 附件）。
> 想改回"全自动深度模式"：把配置中 `analyzer.analyze_abstract_only` 改为 `true`、`fetcher.use_fulltext` 改为 `true`。

---

## 1. 获取代码

```bash
git clone https://github.com/YanSH258/Daily_Paper_Digest.git
cd Daily_Paper_Digest
```

没有 git 的话：GitHub 页面 → Code → **Download ZIP** → 解压即可。

---

## 2. macOS / Linux 安装

```bash
# 1) 虚拟环境 + 依赖
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirement.txt

# 2) 数据根目录（配置/数据库/日志都在这里，建议写入 ~/.zshrc 持久化）
export DPD_DATA_ROOT="$HOME/dpd-data"

# 3) 生成初始配置（已有配置不会被覆盖）
python src/main.py --init-config

# 4) 编辑配置（见第 4 章），最少填 LLM 的 api_key / model
nano "$DPD_DATA_ROOT/config/config.yaml"

# 5) 启动网页控制台（http://127.0.0.1:8080）
python src/web_server.py --config "$DPD_DATA_ROOT/config/config.yaml" --host 127.0.0.1 --port 8080
```

健康检查：`curl http://127.0.0.1:8080/healthz` → `{"ok": true}`。

可选：导入计算材料方向预设订阅源（MLIP/DFT，共 30+ 条，幂等可重复执行）：

```bash
python src/main.py --import-sources config/source_presets.json --dry-run  # 预览
python src/main.py --import-sources config/source_presets.json            # 导入
```

---

## 3. Windows（PowerShell）安装

前提：安装 [Python 3.10+](https://www.python.org/downloads/)，**勾选 "Add python.exe to PATH"**。

### 方式一：一键脚本（推荐）

```powershell
cd C:\你的解压路径\Daily_Paper_Digest
powershell -ExecutionPolicy Bypass -File scripts\start.ps1
```

脚本会自动：创建虚拟环境 → 装依赖 → 生成配置并**弹出记事本**让你填 key → 启动服务。
之后每次启动都是同一条命令（或做成桌面快捷方式）。停止：服务窗口按 `Ctrl+C`。

### 方式二：手动逐步

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirement.txt

# 数据根目录（永久生效，之后新开的终端也能读到）
setx DPD_DATA_ROOT "$HOME\dpd-data"
$env:DPD_DATA_ROOT = "$HOME\dpd-data"   # 当前窗口立即生效

python src\main.py --init-config
notepad "$env:DPD_DATA_ROOT\config\config.yaml"   # 编辑配置

python src\web_server.py --config "$env:DPD_DATA_ROOT\config\config.yaml" --host 127.0.0.1 --port 8080
```

### Windows 注意事项

- 不要直接双击 `scripts\start.ps1`（默认策略禁止），用上面的 `-ExecutionPolicy Bypass` 方式
- 脚本已设置 `PYTHONUTF8=1`，中文/emoji 日志不会乱码
- 进程锁在 Windows 上自动降级为进程内锁：**不要同时启动两个服务窗口**
- 每日定时用任务计划程序（管理员 PowerShell 执行一次）：

```powershell
schtasks /Create /TN "DailyPaperDigest" /SC DAILY /ST 08:00 `
  /TR "powershell -ExecutionPolicy Bypass -File C:\你解压的路径\Daily_Paper_Digest\scripts\start.ps1"
```

- 备份 = 备份整个数据根目录（默认 `%USERPROFILE%\dpd-data`）

---

## 4. 配置详解（`$DPD_DATA_ROOT/config/config.yaml`）

### 必填

| 字段 | 说明 |
|------|------|
| `llm.<provider>.api_key` | LLM 的 API Key。provider 可任意命名（deepseek / qwen / zhipu / siliconflow / 中转站名…），只要 `base_url` 是 OpenAI 兼容接口 |
| `llm.<provider>.base_url` / `model` | 接口地址与模型名 |
| `research_topics` | 你的研究方向关键词列表，评分依据 |

### 常用可调

| 字段 | 默认 | 说明 |
|------|------|------|
| `relevance_threshold` | 4 | 相关性入选线（0-10）。粗筛模式建议 5-7 |
| `fetcher.date_filter_days` | 3 | 只抓近 N 天发布的文章（期刊 RSS 滞后 1-2 周，建议 7-14） |
| `fetcher.use_fulltext` | false | 流水线是否自动抓全文（粗筛模式保持 false） |
| `analyzer.analyze_abstract_only` | true | 是否对"仅有摘要"的文章做 AI 解读。**粗筛模式改为 false**（跳过全部自动解读） |
| `performance.llm_concurrency` | 3 | LLM 并发数；中转站限流频繁时降到 1-2 |
| `scheduler.run_time` + `web.enable_scheduler` | 08:00 / false | 网页端每日定时（也可以用系统 cron/任务计划） |
| `web.api_token` | 空 | 配置后所有写操作需请求头 `X-API-Token`（网页端在侧边栏保存一次） |
| `web.protect_read` | false | 开启后**读取接口也要求 Token**（公网/局域网暴露时建议开启） |

### 可选集成

| 字段 | 说明 |
|------|------|
| `zotero.api_key` / `user_id` / `collection` | Zotero 推送（key 在 zotero.org/settings/keys 申请；user_id 留空可自动解析）。`attach_oa_pdf: true` 时推送自动挂 PDF（arXiv 直接构造，正式出版物查 Unpaywall） |
| `wos.api_key` | Web of Science Starter API：自动补全缺失元数据与被引数 |
| `tracking.enabled` | 引文追踪 + 作者追踪（关注的文献有新引用、关注作者有新文章时自动入库评分） |
| `output.email` / `output.feishu` | 日报推送渠道 |
| `unpaywall_email` | Unpaywall/OpenAlex 礼貌池联系邮箱 |

### 环境变量（优先级高于配置文件）

`DEEPSEEK_API_KEY` / `QWEN_API_KEY`、`EMAIL_PASSWORD`、`FEISHU_WEBHOOK_URL`、`ZOTERO_API_KEY`、`ZOTERO_USER_ID`、`WOS_API_KEY`、`DPD_DATA_ROOT`（数据根目录）、`DPD_API` / `DPD_TOKEN` / `DPD_TIMEOUT`（仅 MCP 服务器使用）。

### 网页端认证语义

- 配置 `web.api_token` 后：**写操作**（收藏/笔记/运行任务/设置保存…）需要 Token
- `web.protect_read: true`：**读操作也要求 Token**；新浏览器第一次使用时，在侧边栏 Token 框填入并保存即可

---

## 5. 每日定时运行

三选一：

```bash
# macOS/Linux crontab（每天 08:00）
0 8 * * * cd /path/to/Daily_Paper_Digest && DPD_DATA_ROOT=$HOME/dpd-data .venv/bin/python src/main.py --config $HOME/dpd-data/config/config.yaml >> data/logs/cron.log 2>&1
```

```powershell
# Windows 任务计划（见第 3 章）
schtasks /Create /TN "DailyPaperDigest" /SC DAILY /ST 08:00 /TR "..."
```

或使用网页端自带调度：设置页 `web.enable_scheduler: true` + `scheduler.run_time`（服务运行期间每日触发）。

其他常用命令：

```bash
python src/main.py --weekly                 # 生成本周周报
python src/main.py --push-only 2026-09-12   # 只补发当天日报推送
python src/main.py --backup                 # 备份数据库（滚动保留 7 份）
python src/main.py --digest-dry-run         # 预览每日 Top-N 选文
```

---

## 6. AI Agent 插件（可选）

> 不使用 agent 软件可跳过本章。前置条件：完成第 1-4 章，并追加安装 MCP SDK：
> `pip install -e ".[mcp]"`

安装后获得命令 `daily-paper-mcp`（stdio 传输），提供 **25 个语义化工具**：检索、阅读管理、
研究专题、引文/作者追踪、Zotero 推送、流水线触发、趋势、AI 对比与草稿、单篇对话。

### ZCode（插件安装，推荐）

Settings → Plugin Management → Discover → 添加本仓库的 `plugin/` 目录作为
marketplace → 安装 `daily-papers`。MCP 服务器与工作流技能（晨间简报/周回顾/整理仪式）一起装好。

也可以手动注册（用户级，`~/.zcode/cli/config.json`）：

```json
{
  "mcp": {
    "servers": {
      "daily-papers": {
        "command": "/path/to/venv/bin/daily-paper-mcp",
        "env": { "DPD_API": "http://127.0.0.1:8080", "DPD_TOKEN": "你的Token" }
      }
    }
  }
}
```

技能文件手动安装：复制 `plugin/skills/daily-papers/SKILL.md` 到 `~/.zcode/skills/daily-papers/SKILL.md`。

### Codex CLI（`~/.codex/config.toml`）

```toml
[mcp_servers.daily-papers]
command = "/path/to/venv/bin/daily-paper-mcp"
env = { "DPD_API" = "http://127.0.0.1:8080", "DPD_TOKEN" = "你的Token" }
```

### Claude Desktop（`claude_desktop_config.json`）

```json
{
  "mcpServers": {
    "daily-papers": {
      "command": "/path/to/venv/bin/daily-paper-mcp",
      "env": { "DPD_API": "http://127.0.0.1:8080", "DPD_TOKEN": "你的Token" }
    }
  }
}
```

### 验证

对 agent 说 **"今天有什么文献"** —— 它应调用 `preview_daily_digest` / `today_top_n`
并按优先级汇报。手动验证：`python tests/mcp_e2e.py http://127.0.0.1:8080`（需工作台在运行）。

---

## 7. 常见问题对照

| 现象 | 原因与处理 |
|------|-----------|
| `无法连接文献工作台` / 浏览器打不开 | 服务没启动；确认 `healthz`，查看日志 `$DPD_DATA_ROOT/data/logs/` |
| 写操作返回 `unauthorized` | 服务端配置了 Token，网页侧边栏 / MCP 的 `DPD_TOKEN` 需填同一 Token |
| 开启读取保护后页面数据 401 | 同上：侧边栏填 Token 后刷新 |
| 评分全部失败：`Your api key ... is invalid` | LLM key 是占位符或错误：设置页填真实 key（或环境变量），重跑即可——失败的评分会自动重试，不丢数据 |
| 评分大量失败：`429 Too Many Requests` | LLM 提供方限流：调低 `performance.llm_concurrency`（如 1-2），失败的下次运行自动补齐 |
| 启动报 YAML `mapping values are not allowed here` | 手工编辑 config 时冒号后少了空格（如 `key:value`），改为 `key: value` |
| 某些期刊一直 0 篇 | 出版商反爬（如 ACS 系 Cloudflare）。用 OpenAlex ISSN 订阅替代：`journals` 表 `source_type=openalex`、`query=filter:locations.source.issn:<ISSN>`（参考 `config/source_presets.json`） |
| 日期乱/今天抓的算昨天 | 确认系统时区正常；库内 UTC 时间已按本地时区比较 |

## 8. 升级与备份

```bash
git pull
pip install -r requirement.txt      # 有新依赖时
# 重启服务。数据库结构变更会在启动时自动迁移，迁移前自动备份到
# $DPD_DATA_ROOT/data/db/*.backup-<时间戳>.db（可重复执行）
```

手动备份：`python src/main.py --backup`（滚动保留 7 份）；或直接整目录复制
`$DPD_DATA_ROOT`（含配置、数据库、日志、报告）。
