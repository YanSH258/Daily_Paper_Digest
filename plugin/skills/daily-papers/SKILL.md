---
name: daily-papers
description: 使用当用户提到文献、论文、日报、周报、今天读什么、阅读清单、研究专题、引文追踪、Zotero 推送，或想运行/查询文献抓取任务时。通过 daily-papers MCP 服务器操作用户的个人文献工作台（化学/材料方向）。
---

# 文献工作台操作手册

通过 `daily-papers` MCP 服务器的 25 个工具操作用户的个人文献库。
所有工具失败时把错误原样转述给用户，不要盲目重试。

## 前置条件

工作台服务需在运行（默认 `http://127.0.0.1:8080`，由 MCP 环境变量
`DPD_API` 指定）：

```bash
daily-paper-web --config <用户的配置文件> --port 8080
```

未启动时，工具会返回"无法连接文献工作台 …"并附启动提示——把它转述给用户即可。
若报 `HTTP 401 认证失败`，提示用户检查工作台 `web.api_token` 与 MCP 端
`DPD_TOKEN` 是否一致。空文献库是正常状态（返回空列表），不要当成故障。

## 工具地图

| 意图 | 工具 |
|------|------|
| 今天读什么 | `preview_daily_digest`（只读预览，含推荐理由） |
| 找文献 | `search_papers` |
| 深入某篇 | `get_paper`（默认不含全文） |
| 管理阅读 | `set_reading_status` / `star_paper` / `add_note` / `add_tags`（均为覆盖式写操作） |
| 组织课题 | `list_topics` / `create_topic` / `add_papers_to_topic` / `get_topic` |
| 正式日报 | `get_digest_history` 选版本 → `get_digest` 看快照；`publish_daily_digest` 发布（写操作） |
| 归档 | `push_to_zotero`（写用户外部账号） |
| 追踪 | `watch_paper` / `watch_author`（只注册关注，不会立即返回新引用列表） |
| 跑任务 | `run_pipeline` / `task_status` |
| 统计 | `trends` / `list_reports` |
| AI 生成 | `compare_papers` / `related_work_draft` / `chat_with_paper`（调用 LLM） |

## 工作流

### 今天读什么（用户说"今天有什么文献 / 今天读什么"）

1. 调 `preview_daily_digest`（只读：不写库、不发布、不发送）。
2. **保留服务端 rank 顺序**，逐篇展示：题名（`title`）、方向
   （`topic`/`category`）、推荐依据（`reason` 字段，来自文章库已有评分）。
   `reason` 为空时如实说"暂无推荐理由"，**不要自己编造研究关联**。
3. 结尾问一句："要把哪几篇加入待读清单？"——用户点名后才批量
   `set_reading_status(id, "queued")`，写入后读回核验（见下）。
4. 只有用户明确说"正式发布日报"时才用 `publish_daily_digest`（见下）。

### 查看论文（用户点开某篇）

1. `get_paper(paper_id)`：默认返回元数据、摘要、评分理由、AI 解读、笔记标签。
2. 汇报时明确区分四类内容：**原文摘要**（abstract）、**机器翻译题名**
   （title_zh，若有）、**AI 解读**（analysis，模型生成）、**个人笔记**
   （note，用户自己写的）。`evidence_level` 表示只有摘要依据时如实说明
   "该篇仅有摘要级证据，未抓取全文"。
3. 完整阅读请给用户网页入口（工作台地址下的文献详情页），**不要默认把
   全文塞进上下文**；只有用户明确要求引用原文细节时才传
   `include_fulltext=true`（全文可达数万字，超 3 万字会被截断）。

### 加入待读 / 收藏 / 笔记（用户明确要求后执行）

1. 写入后**必须读回核验**：`get_paper(paper_id)` 确认
   `read_status`/`starred`/`note`/`tags` 已生效，再向用户报告结果。
2. 覆盖语义（已锁定，不得改口）：
   - `add_note` **覆盖**整篇笔记，不是追加。
   - `add_tags` **覆盖**全部标签；传空列表 = 清空标签。
   - 用户说"补充一点笔记/再加个标签"时，先 `get_paper` 读出旧值，
     合并后整体写回；不要让模型凭空猜。
3. `set_reading_status` 取值：queued(待读)/reading(在读)/read(已读)/空(移出清单)。

### 新引用提醒（目前没有的能力，不要承诺）

`watch_paper`/`watch_author` 只是把文献/作者加入关注，让新引用和新文章
进入后续采集评分流程。**当前没有工具能列出"新增引用事件"**，因此不要
在晨间简报或其他场景承诺"有新引用会提醒你"；用户问起新引用时，
说明需要运行采集流程后通过检索查看。

### 正式发布日报（用户明确要求时）

1. 先向用户确认**日期**和**推送渠道**。渠道只允许 `email` / `feishu`；
   用户未指定渠道时传省略 channels = 只发布不发送。
2. 同日重复请求会幂等复用已有版本（`created=false`，不重算不重发）。
   用户明确要求"重新生成"时换新的 `request_key`。
3. 返回中渠道发送状态为 `unknown` 时，如实告知"发送结果未知，请人工核对
   （网页端日报页可查推送状态）"，**不要自动重发**。
4. `publish_daily_digest` 是写操作且配置了渠道时会真实对外发送——
   调用前必须获得用户对日期+渠道的明确确认。

### 周回顾（用户说"这周怎么样 / 周报"）

1. `list_reports` 看本周周报是否已生成；没有则提示可运行
   `run_pipeline(mode="weekly")`（先征得同意）。
2. `trends` 给出方向分布变化的观察。

### 整理仪式（用户说"帮我整理文献"）

1. `search_papers(read_status="read")` 已读的 → 问是否 `push_to_zotero`
   归档（影响用户 Zotero 账号，批量前列清单确认）。
2. `search_papers(min_score=8)` 高分未处理的 → 建议加入阅读清单或专题。
3. 专题操作前 `list_topics` 确认已有专题，避免重复创建。

## 硬性规则

1. **成本**：`run_pipeline`、`compare_papers`、`related_work_draft`、
   `chat_with_paper` 都会调用 LLM 或触发全量抓取——**必须先向用户说明
   并获得同意**，不得主动调用。
2. **推送与发布**：`push_to_zotero`、`publish_daily_digest`（带渠道）是
   外部写操作，调用前征得确认；发送结果 `unknown` 时保留人工核对。
3. **上下文卫生**：`get_paper` 默认不带全文；`search_papers` 与
   `get_digest` 的大字段已截断，需要完整内容用网页预览。
4. **诚实汇报**：失败（模型余额不足、服务未启动、认证失败）原样转述
   错误，不要编造结果；写入后读回核验再报成功。
