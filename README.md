# Daily Paper Digest

化学/材料方向的 AI 文献日报：自动抓顶刊 RSS → 大模型打分解读 → 生成报告并推送。自带网页控制台管理文献库。

> 📦 **完整安装与配置指南（macOS / Windows PowerShell / AI Agent 插件）见 [INSTALL.md](INSTALL.md)**

## 功能

- 多期刊 RSS 订阅（可在网页端添加/OPML 导入）
- LLM 相关性打分（0–10）、单段摘要译文与全文深度解读，支持任意 OpenAI 兼容 API
- 分层全文获取（Unpaywall → HTTP → 浏览器 → OpenAlex 摘要）
- 日报/周报生成，邮件与飞书推送
- 网页文献库：检索筛选、收藏笔记标签、AI 问答、阅读清单
- 研究专题、趋势、作者/引文追踪、Zotero 同步、期刊 IF 与分区展示

## 快速开始

```bash
pip install -r requirements.txt            # 工作台主体依赖
pip install -e ".[mcp]"                   # 如需 MCP/AI 客户端接入，安装 MCP SDK
cp config/config_template.yaml config/config.yaml
# 编辑 config.yaml：填入 API Key、research_topics 等

python src/main.py --collect-preview   # 只读采集预览（不调用模型/不入库）
python src/main.py --trial             # 小批量评分试运行（不生成日报/不推送）
python src/main.py                     # 立即跑一次
python src/main.py --schedule          # 每日定时（阻塞）
python src/main.py --weekly            # 生成周报
```

> 采集与评分的管控规则（日期准入、评分预算、来源游标、存量处置）见 `AGENTS.md` 的「采集与评分管控」一节。

从零开始（含 ZCode / Codex 插件接入、初始化、认证与常见故障）的完整步骤见
`plugin/README.md`（受版本控制，克隆后即可阅读）。插件可作为 ZCode 插件或
Codex 插件安装，两种客户端共用同一份 MCP 服务器与 Skill。

### wheel 安装与数据目录

```bash
python -m pip install .             # 或 pip install /path/to/daily_paper_digest-*.whl
# 推荐：直接使用绝对配置路径，程序自动把其所在数据目录作为根目录
mkdir -p "$HOME/dpd-data/config"
daily-paper-digest --init-config --config "$HOME/dpd-data/config/config.yaml"
# 编辑配置后，CLI 与 Web 都只需传同一个绝对配置路径
daily-paper-digest --config "$HOME/dpd-data/config/config.yaml"
daily-paper-web --config "$HOME/dpd-data/config/config.yaml" --host 127.0.0.1 --port 8080
# cron / Docker 等需要强制覆盖时，仍可设置 DPD_DATA_ROOT
```

数据根目录按以下优先级选择（启动进程时确定）：

1. 环境变量 `DPD_DATA_ROOT`，支持 `~`；显式设置时优先级最高。
2. 未设置环境变量且传入绝对 `--config` 时：`<root>/config/config.yaml` 自动推断 `<root>` 为数据根；其他绝对配置文件使用其所在目录。
3. 未设置环境变量且使用相对 `--config` 时，源码/editable 安装使用仓库根；wheel 安装按平台使用默认用户数据目录。

| 内容 | 相对于数据根的默认位置 |
|------|------------------------|
| 配置 | `config/config.yaml` |
| SQLite 数据库及流水线锁 | `data/db/chem_daily.db`、`data/db/.pipeline.lock` |
| 日志 | `data/logs/YYYY-MM-DD.log` |
| 日报 / 周报 | `data/output/` |
| 数据库备份 | `data/backups/`（可用 `backup.directory` 覆盖） |

CLI/Web 共用的配置加载器将 `database.path`、`output.output_dir`、`fetcher.upload_dir` 和 `backup.directory` 中的相对路径锚定到数据根，绝对路径保留。`--config` 的相对路径也基于数据根，不基于当前工作目录或配置所在目录。wheel 内只包含只读配置模板，不包含个人配置或数据库；`--init-config` 不执行抓取、模型调用或推送。

安装不会自动迁移已有数据。已有部署可将 `DPD_DATA_ROOT` 指向原仓库根目录继续使用；切换到其他目录前应自行备份并迁移 `config/` 和 `data/`。旧版备份可能位于 `data/db/backups/`，新的默认位置不会自动搬移或清理旧备份。

### 网页控制台

```bash
PYTHONPATH="src:.deps" python src/web_server.py --config config/config.yaml --host 127.0.0.1 --port 8080
```

默认只监听本机；若显式监听非本机地址，必须配置 `web.api_token` 或 `WEB_API_TOKEN`。设置 `web.readonly: true` 后，服务端会拒绝所有写操作。打开 http://127.0.0.1:8080/

主要页面：今日精选、文献库、阅读清单、研究专题、趋势、日报与周报、追踪、订阅、任务、设置。

本机默认可直接使用；API Token 可在「设置」中启用。调度设置支持严格的 `HH:MM` 和 IANA 时区（如 `Asia/Shanghai`）。详见 `AGENTS.md`。

**「设置」页可改的内容**：模型（提供方/Base URL/模型名/API Key）、相关性阈值与研究方向、抓取参数、推送渠道（邮件开关、SMTP 服务器/端口/发件账号/授权码、收件人，飞书开关与 Webhook）、Zotero、追踪间隔、OpenAlex API Key，以及服务端 Token。所有密钥只保存在本机 `config.yaml`，页面只回显掩码；留空表示保持不变，授权码与 Token 另有「清除」入口。SMTP 授权码是邮箱服务商生成的专用密码，不是邮箱登录密码。

**Token 与访问地址**：Token 只在需要时配置。只监听本机（`--host 127.0.0.1`）时无需 Token；显式监听非本机地址时**必须**配置 `web.api_token` 或 `WEB_API_TOKEN`，否则拒绝启动。浏览器把 Token 存在 localStorage，而 localStorage 按**源**隔离——`http://localhost:8080` 与 `http://127.0.0.1:8080` 属于不同源，所以请固定用一个地址打开控制台，否则需要重新连接。Token 不匹配时页面会明确提示去「设置 → 输入服务端 Token」重新连接。

### 开发环境

依赖清单已合并为单个 `requirements.txt`：上半段是运行时依赖（wheel 打包只声明这一段），下半段是开发/测试工具（pytest 等），生产部署可忽略下半段：

```bash
python -m pip install -r requirements.txt   # 运行时 + 开发/测试工具
python -m pytest -q
python -m unittest discover -s tests -v
```

MCP SDK 和需要运行中 Web 服务的验收脚本仍按 `.[mcp]` 与 `tests/mcp_*.py` 单独执行。

## 配置要点

| 项 | 说明 |
|----|------|
| `llm.provider` / `api_key` / `base_url` / `model` | DeepSeek / Qwen / 任意兼容接口 |
| `research_topics` | 用于相关性打分的研究方向关键词 |
| `relevance_threshold` | 入库阈值（0–10） |
| `fetcher.use_fulltext` / `use_browser` | 全文与浏览器渲染开关 |
| `zotero.api_key` / `collection` | 推送到 Zotero（collection 可用名称或 key） |
| `output.email` / `output.feishu` | 邮件 / 飞书推送 |
| `openalex.api_key` | 可选。OpenAlex 列表查询消耗额度（10 credits/次，免费 10,000/天），也可用环境变量 `OPENALEX_API_KEY` |
| `processing.max_score_articles_per_run` / `trial_max_score_articles` | 每轮评分预算（默认 100 / 试运行 30）；超额文章延期到下轮，不会丢 |
| `fetcher.collection_budget_seconds` | Step 1 采集总时长上限（默认 300 秒，超时跳过剩余来源并保留游标） |

环境变量可覆盖敏感项：`DEEPSEEK_API_KEY`、`QWEN_API_KEY`、`ZOTERO_API_KEY`、`EMAIL_PASSWORD`、`FEISHU_WEBHOOK_URL` 等。

评分按论文与**最匹配的一个研究方向**判断，方向的排列顺序不代表优先级。实质研究任一方向通常给 6–7 分，方法或体系高度相关给 8–9 分，10 分需有直接契合研究问题的明确依据；只提到关键词或使用常规辅助计算不自动算高度相关。新规则只影响之后的评分，不会因为提示词更新而批量重评旧文献。

原来只按标题评分的文章，在采集或补全时拿到摘要后，只要还在当前日期窗口内，就会随下一轮任务重新评分，占用原有单轮预算。重评失败保留原分数与笔记，并在任务页显示失败；已成功的解读不重复生成。手动添加仍默认 8 分并收藏，但明确标记为**手动评分**，不会混作模型结果自动重评。

仅有摘要时，AI 内容是一段连续、忠实的中文摘要译文；只有取得全文时才生成分节的深度解读。摘要翻译与全文解读在页面和日报中分别标注，截断或混入模型思考过程的译文不会作为成功结果保存。

Nature 官方 RSS 的发表日期有时放在 `dc:date` 中，程序会保留这一来源标记并读取日期；普通文章的更新时间不会冒充发表日期。没有完整日期的文章继续保留待核验。再次采到同一篇文章的可信发表日期时，可以恢复正常处理；升级本身不会把所有历史待核验文章改成当天。

## 注意

- `config/config.yaml` 与本地数据库已在 `.gitignore`，**不要提交到公开仓库**
- 运行日志：`data/logs/YYYY-MM-DD.log`
- 详细架构与流水线说明见 `AGENTS.md` / 源码注释
