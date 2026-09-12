# Daily Paper Digest

化学/材料方向的 AI 文献日报：自动抓顶刊 RSS → 大模型打分解读 → 生成报告并推送。自带网页控制台管理文献库。

## 功能

- 多期刊 RSS 订阅（可在网页端添加/OPML 导入）
- LLM 相关性打分（0–10）与深度解读，支持任意 OpenAI 兼容 API
- 分层全文获取（Unpaywall → HTTP → 浏览器 → OpenAlex 摘要）
- 日报/周报生成，邮件与飞书推送
- 网页文献库：检索筛选、收藏笔记标签、AI 问答、阅读清单
- 研究专题、趋势、作者/引文追踪、Zotero 同步、期刊 IF 与分区展示

## 快速开始

```bash
pip install -r requirement.txt
cp config/config_template.yaml config/config.yaml
# 编辑 config.yaml：填入 API Key、research_topics 等

python src/main.py                 # 立即跑一次
python src/main.py --schedule      # 每日定时（阻塞）
python src/main.py --weekly        # 生成周报
```

### wheel 安装与数据目录

```bash
python -m pip install .             # 或 pip install /path/to/daily_paper_digest-*.whl
# 可选；必须在启动进程前设置，CLI 与 Web 使用相同值
export DPD_DATA_ROOT="$HOME/.local/share/daily-paper-digest"
daily-paper-digest --init-config    # 从内置模板创建配置，已有文件不会覆盖
# 编辑 $DPD_DATA_ROOT/config/config.yaml，填入自己的配置后再运行
daily-paper-digest
# Web 的设置保存使用传入路径，请使用绝对 --config 路径
daily-paper-web --config "$DPD_DATA_ROOT/config/config.yaml" --host 127.0.0.1 --port 8080
```

数据根目录按以下优先级选择（启动进程时确定）：

1. 环境变量 `DPD_DATA_ROOT`，支持 `~`；相对值在启动时转为绝对路径，建议始终使用绝对值。
2. 源码或 editable 安装：仓库根目录，兼容现有 `config/`、`data/` 布局。
3. wheel 安装：Linux 使用 `${XDG_DATA_HOME:-$HOME/.local/share}/daily-paper-digest`（相对 `XDG_DATA_HOME` 无效）；macOS 使用 `~/Library/Application Support/daily-paper-digest`；Windows 使用 `%LOCALAPPDATA%/daily-paper-digest`。

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
PYTHONPATH="src:.deps" python src/web_server.py --config config/config.yaml --port 8080
```

打开 http://127.0.0.1:8080/

主要页面：今日精选、文献库、阅读清单、研究专题、趋势、日报与周报、追踪、订阅、任务、设置。

写操作默认不校验；可在「设置」启用 API Token（保存后浏览器会记住）。详见 `AGENTS.md`。

## 配置要点

| 项 | 说明 |
|----|------|
| `llm.provider` / `api_key` / `base_url` / `model` | DeepSeek / Qwen / 任意兼容接口 |
| `research_topics` | 用于相关性打分的研究方向关键词 |
| `relevance_threshold` | 入库阈值（0–10） |
| `fetcher.use_fulltext` / `use_browser` | 全文与浏览器渲染开关 |
| `zotero.api_key` / `collection` | 推送到 Zotero（collection 可用名称或 key） |
| `output.email` / `output.feishu` | 邮件 / 飞书推送 |

环境变量可覆盖敏感项：`DEEPSEEK_API_KEY`、`QWEN_API_KEY`、`ZOTERO_API_KEY`、`EMAIL_PASSWORD`、`FEISHU_WEBHOOK_URL` 等。

## 注意

- `config/config.yaml` 与本地数据库已在 `.gitignore`，**不要提交到公开仓库**
- 运行日志：`data/logs/YYYY-MM-DD.log`
- 详细架构与流水线说明见 `AGENTS.md` / 源码注释
