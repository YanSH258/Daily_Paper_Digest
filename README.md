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
