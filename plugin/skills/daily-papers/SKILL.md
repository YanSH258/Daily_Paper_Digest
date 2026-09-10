---
name: daily-papers
description: 使用当用户提到文献、论文、日报、周报、今天读什么、阅读清单、研究专题、引文追踪、Zotero 推送，或想运行/查询文献抓取任务时。通过 daily-papers MCP 服务器操作用户的个人文献工作台（化学/材料方向）。
---

# 文献工作台操作手册

通过 `daily-papers` MCP 服务器的工具操作用户的个人文献库。所有工具失败时先向用户转述错误，不要盲目重试。

## 前置条件

工作台服务需在运行（默认 `http://127.0.0.1:8080`）：

```bash
daily-paper-web --config config/config.yaml --port 8080
```

未启动时提示用户先启动，不要盲目重试工具调用。

## 工具地图

| 意图 | 工具 |
|------|------|
| 今天读什么 | `today_top_n` |
| 找文献 | `search_papers` |
| 深入某篇 | `get_paper`（全文默认不返回，需要引用原文时传 include_fulltext=true） |
| 管理阅读 | `set_reading_status` / `star_paper` / `add_note` / `add_tags` |
| 组织课题 | `list_topics` / `create_topic` / `add_papers_to_topic` / `get_topic` |
| 归档 | `push_to_zotero` |
| 追踪 | `watch_paper` / `watch_author` |
| 跑任务 | `run_pipeline` / `task_status` |
| 统计 | `trends` / `list_reports` |
| AI 生成 | `compare_papers` / `related_work_draft` / `chat_with_paper` |

## 工作流

### 晨间简报（用户说"今天有什么文献 / 今天读什么"）
1. `today_top_n` 取今日精选，按 rank 顺序汇报，每篇一句话理由（用 relevance_reason + title_zh）
2. 有 `watch_paper` 或星标的文章如有新引用，单独提醒
3. 结尾问一句："要把哪几篇加入待读清单？"——用户点名后批量 `set_reading_status(id, "queued")`

### 周回顾（用户说"这周怎么样 / 周报"）
1. `list_reports` 看本周周报是否已生成；没有则提示用户可运行 `run_pipeline(mode="weekly")`（先征得同意）
2. `trends` 给出方向分布变化的三条观察

### 整理仪式（用户说"帮我整理文献"）
1. `search_papers(read_status="read")` 已读的 → 问是否 `push_to_zotero` 归档
2. `search_papers(min_score=8)` 高分未处理的 → 建议加入阅读清单或专题
3. 专题操作前 `list_topics` 确认已有专题，避免重复创建

## 硬性规则

1. **成本**：`run_pipeline`、`compare_papers`、`related_work_draft`、`chat_with_paper` 都会调用 LLM 或触发全量抓取——**必须先向用户说明并获得同意**，不得主动调用。
2. **推送**：`push_to_zotero` 影响用户外部账号，批量推送前列出将推送的文献清单征得确认。
3. **上下文卫生**：`get_paper` 默认不带全文；只有当用户要求引用原文细节时才传 `include_fulltext=true`，且注意全文可达数万字。
4. **诚实汇报**：任务失败（如 LLM 余额不足、服务未启动）原样转述错误，不要编造结果。服务未启动时提示：`daily-paper-web --config config/config.yaml`。
