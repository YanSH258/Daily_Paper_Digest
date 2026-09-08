# 📚 Daily Paper Digest (AI 化学/材料文献日报)

专为科研人员打造的自动化文献追踪工具。自动监控顶刊 RSS，基于摘要利用大模型（DeepSeek/Qwen）进行相关性打分与深度解读，生成日报并多渠道推送。

## ✨ 核心特性

* **多源聚合**：内置 30+ 化学/材料/物理领域顶级期刊 RSS，网页端可自由添加/导入订阅
* **智能相关性过滤**：基于摘要自动打分（0-10分），剔除无关文章
* **分层全文获取**：Unpaywall OA → HTTP轻量请求 → 浏览器渲染 → OpenAlex摘要补全，四层回退策略
* **PDF文本提取**：支持直接下载PDF并提取文本（PyMuPDF）
* **AI深度解读**：基于全文或摘要生成研究亮点、方法创新、个人关联度分析
* **自动方向分类**：内置10+研究方向自动标签（ML势函数、MD、DFT、MOF、催化等）
* **数据大盘**：SQLite数据库 + HTML可视化大盘，支持关键词搜索与CSV导出
* **多渠道推送**：邮件（含附件）+ 飞书 Webhook
* **个人文献库**：星标收藏、阅读笔记、自定义标签，网页端集中管理
* **文献 AI 问答**：在文献详情中基于该文献（标题/摘要/AI解读）与大模型多轮对话，SSE 流式输出，聊天记录按文献持久化
* **网页端管理**：订阅增删/批量导入/OPML、文献检索（收藏/标签/分页）、历史日报预览、任务触发、设置在线编辑（写回 config.yaml 保留注释）；LLM 提供方可任意命名，支持接入任意 OpenAI 兼容 API（自定义 Base URL / 模型名）

## 📁 项目目录结构

```
Daily_Paper_Digest/
├── src/
│   ├── main.py                    # 主入口：8步流水线
│   ├── core/
│   │   ├── fetcher.py             # RSS抓取 + 分层全文获取
│   │   ├── analyzer.py            # LLM相关性评分与深度解读
│   │   ├── db.py                  # SQLite数据库（去重+存储）
│   │   ├── notifier.py            # 报告生成与多渠道推送
│   │   ├── request_manager.py     # HTTP请求管理（UA轮换+速率限制）
│   │   ├── rate_limiter.py        # 域名级速率限制
│   │   └── ua_pool.py             # User-Agent池
│   ├── fetchers/
│   │   ├── html_fetcher.py        # 轻量HTTP HTML获取
│   │   ├── pdf_fetcher.py         # PDF下载与文本提取
│   │   ├── oa_fetcher.py          # Unpaywall + OpenAlex API
│   │   ├── network.py             # 代理/网络检测
│   │   └── models.py              # 共享数据模型与常量
│   └── utils/
│       ├── stat_db.py             # 数据库统计 & HTML大盘生成
│       └── reanalyze.py           # 重新触发AI解读
│
├── data/                          # 数据目录（gitignore）
│   ├── db/                        # SQLite数据库
│   ├── logs/                      # 运行日志（按日期）
│   └── output/                    # 报告与HTML大盘
│
├── config/
│   ├── config_template.yaml       # 配置模板
│   └── config.yaml                # 实际配置（gitignore，勿提交）
│
├── requirement.txt
└── setup.py
```

## 🚀 快速开始

**1. 安装依赖**

```bash
pip install -r requirement.txt
```

**2. 配置**

```bash
cp config/config_template.yaml config/config.yaml
# 编辑 config.yaml，填入 API Key 和研究方向
```

**3. 运行**

```bash
# 立即运行一次
python src/main.py

# 指定日期运行
python src/main.py --date 2025-01-15

# 指定配置文件
python src/main.py --config path/to/cfg.yaml

# 每日定时模式（阻塞，按config中设定的时间运行）
python src/main.py --schedule
```

**工具脚本**

```bash
# 生成/更新HTML数据库大盘
python src/utils/stat_db.py

# 搜索数据库并导出CSV
python src/utils/stat_db.py --search "machine learning" --export results.csv

# 重新分析已入库文章
python src/utils/reanalyze.py
```

**服务器定时运行（cron推荐）**

```bash
# 编辑 crontab
crontab -e

# 每天早上 8:00 运行
0 8 * * * cd /path/to/Daily_Paper_Digest && /path/to/python src/main.py >> data/logs/cron.log 2>&1
```

## ⚙️ 配置详解

### 环境变量（优先级高于config.yaml）

| 变量名 | 说明 |
|--------|------|
| `DEEPSEEK_API_KEY` / `QWEN_API_KEY` | LLM API Key |
| `EMAIL_PASSWORD` | 邮箱SMTP授权码 |
| `FEISHU_WEBHOOK_URL` | 飞书机器人Webhook地址 |
| `HTTP_PROXY` / `HTTPS_PROXY` | 代理设置 |
| `UNPAYWALL_EMAIL` | Unpaywall/OpenAlex联系邮箱 |

### 关键配置项

```yaml
# LLM选择
llm:
  provider: "deepseek"  # 或 "qwen"

# 研究方向（用于相关性评分）
research_topics:
  - "machine learning interatomic potential"
  - "density functional theory DFT"

# 相关性阈值（0-10）
relevance_threshold: 4

# 全文获取开关
fetcher:
  use_fulltext: true      # 是否获取全文
  use_browser: true       # 是否启用浏览器渲染（处理JS/Cloudflare）

# 性能调优
performance:
  concurrency: 5          # 全文抓取并发数
  llm_concurrency: 3      # LLM调用并发数（受API限速影响）
  rate_limit:
    global_rps: 5         # 全局每秒请求上限
```

## 📬 推送配置

### 邮件推送

```yaml
output:
  email:
    enabled: true
    smtp_server: "smtp.163.com"
    smtp_port: 465
    username: "your_email@163.com"
    password: "your_auth_code"
    recipients:
      - "target@example.com"
```

邮件内容包含HTML格式日报，并自动附加 `paper_index.html` 数据库大盘文件。

### 飞书推送

```yaml
output:
  feishu:
    enabled: true
    webhook_url: "https://open.feishu.cn/open-apis/bot/v2/hook/xxx"
```

## 🌐 网页端控制台

轻量网页端（原生 JS 零构建，前端位于 `src/static/`）：个人文献库、订阅管理、历史日报、任务触发、在线设置，以及文献 AI 问答。

```bash
# 启动网页端（默认 8080）
PYTHONPATH="src:.deps" python src/web_server.py --config config/config.yaml --host 0.0.0.0 --port 8080

# 或通过安装后的命令
daily-paper-web --config config/config.yaml --port 8080
```

打开：`http://127.0.0.1:8080/`

五个视图：
- **文献库**：关键词/期刊/方向/标签/评分筛选，全部文献与"我的收藏"两个标签页，分页浏览；点击标题打开详情抽屉
- **详情抽屉**：星标收藏、自定义标签、阅读笔记、摘要与 AI 解读（Markdown 渲染）、**AI 对话区**
- **订阅管理**：添加/OPML 批量导入/测试/启停/删除 RSS 源
- **历史日报**：按日期列表 + 在线预览 HTML 日报
- **运行任务 / 设置**：三种模式触发抓取、任务状态轮询、在线编辑配置

主要接口：
- `GET /paper-index`：在线查看 `data/output/paper_index.html`（只读）
- `GET /api/articles`：文章列表（q/journal/topic/tag/starred/min_score/analyzed_only/limit/offset，返回 total）
- `GET /api/articles/{id}`：单篇详情（含 starred/note/tags/chat_count）
- `POST /api/articles/{id}/star|note|tags`：星标 / 笔记 / 标签（Token 保护）
- `GET|POST|DELETE /api/articles/{id}/chat`：文献 AI 对话（POST 为 SSE 流式返回，Token 保护）
- `GET /api/tags`：全部自定义标签及计数
- `GET /api/reports`：历史日报列表
- `GET /api/status`：任务运行状态与数据库概览
- `POST /api/run`：手动触发一次任务（mode: default/abstract/fulltext）
- `POST /api/llm/test`：测试 LLM API 可用性（按设置页表单值实测一次对话接口，Key 留空则用已保存配置）
- `POST /api/settings`：在线保存配置（白名单字段写回 config.yaml，保留注释）

可选安全设置：
- 在 `config.yaml` 中设置 `web.api_token`（或环境变量 `WEB_API_TOKEN`），启用后所有写操作需要请求头 `X-API-Token`（网页端在侧边栏 Token 框保存一次即可）。

> 说明：网页端任务采用后台线程执行，避免阻塞页面请求。

## 🔄 全文获取策略

系统采用四层回退策略获取全文，按优先级排列：

1. **Unpaywall OA**：查询开放获取链接（免费）
2. **HTTP轻量请求**：直接请求HTML，使用出版商特定CSS选择器提取正文
3. **浏览器渲染**：DrissionPage/Chromium处理JS-heavy页面、Cloudflare验证
4. **OpenAlex摘要**：通过OpenAlex API补全摘要（无需API Key）

支持的出版商：ACS、Wiley、Nature、Elsevier、RSC、Springer、AIP、APS、MDPI、Science、IOP

## 📊 输出示例

### 日报内容结构

1. **文献总览统计表**
   - 按期刊统计（文章数、相关篇数、方向分布）
   - 按研究方向分类（ML势函数、MD、DFT、MOF等）
   - 完整文章列表（可展开）

2. **相关文章深度解读**
   - AI生成的研究亮点
   - 方法创新点
   - 与个人研究的关联度
   - 可信度标注（全文解读 vs 仅摘要）

### 数据库大盘

访问 `data/output/paper_index.html` 查看：
- 所有历史文章列表
- 关键词搜索
- 相关性排序
- CSV导出功能

## 🛡️ 注意事项

1. **Git安全**：`config/config.yaml` 包含敏感信息，已加入 `.gitignore`，请勿提交到公开仓库
2. **GitHub Actions 部署**：不要将 `config/config.yaml` 提交到公开仓库，应将 API Key 和邮件密码配置在 GitHub Secrets 中
3. **API限速**：LLM并发度建议设为2-5，避免触发API限流
4. **浏览器渲染**：首次使用DrissionPage会自动下载Chromium，需要联网
5. **代理设置**：如需通过机构代理访问，设置 `HTTP_PROXY`/`HTTPS_PROXY` 环境变量即可

## 📄 日志

运行日志保存在 `data/logs/YYYY-MM-DD.log`，包含详细的抓取状态和错误信息。
