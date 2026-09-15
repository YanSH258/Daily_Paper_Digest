# P0：采集准入、预算与存量恢复

## 行为约定

采集窗口按 `scheduler.timezone`（默认 `Asia/Shanghai`）和本次报告日计算。`date_filter_days: 3` 表示今天及前两天：报告日 `2026-09-13` 的闭区间为 `2026-09-11` 至 `2026-09-13`。带时区时间先转本地日期；不带时区时间按配置时区解释。

- `eligible`：准入通过。已经入队的文章不会因为排队跨日而失去资格。
- `needs_date`：缺日期或仅有年/月，保存但不自动评分。
- `invalid_date`：非法或未来日期，保存待核验，不自动评分。
- `outside_window`：明确早于窗口，保留为历史待处理。
- `unreviewed`：旧库尚未准入。普通队列读取临时判断，只有合格文章进入队列；不会自动把旧历史/未知日期文章批量迁移。

准入与 `score_status` 独立。无日期不会用入库时间/当天补齐；延期不写失败、不打零分。发表日期与日期来源、准入时间、首次排队时间分别保存。Crossref 优先完整在线发表日期，再回退出版日期；RSS 不使用 updated 冒充发表日期；arXiv 使用首次提交日期。

采集窗口、失败恢复窗口和日报候选池是不同概念。预算延期文章不受旧的七天重试年龄限制；评分已成功的文章不再自动评分。

## 预算

```yaml
processing:
  max_score_articles_per_run: 100
  trial_max_score_articles: 30
```

配置为正整数，可主动调整。一个名额指一次文章评分尝试，不等于一次 HTTP 请求。模型内部有界重试仍可能增加实际 HTTP 请求；`llm_requests` 统计所有实际模型尝试，包括失败及重试。SDK 隐式重试关闭。

预算在摘要补查、翻译、WOS 增强及评分线程启动前选定。新增、已准入积压和失败重试按文章 ID 去重，并按期刊轮转、期刊内首次排队时间 FIFO。失败重试最多占本轮预算的四分之一（小预算至少一个名额）；剩余名额不被无尽重试填满。深度解读也有独立的同额上限，因此本轮评分100篇并不表示全任务只有100次模型请求。

`score_deferred` 表示预算及重试份额延期，保留 `eligible`、排队时间和延期原因。下一轮继续处理。任务完成仅代表本轮执行结束，不表示全部积压已消化。

## 预览与试运行

```bash
# 真实采集预检：不调用模型，不写文章/游标，不生成日报、不推送
python src/main.py --config /path/to/config.yaml --collect-preview --date 2026-09-13

# 试运行：按试运行预算评分及保存结果，不生成正式日报或推送
python src/main.py --config /path/to/config.yaml --trial --date 2026-09-13
```

Web 任务页有“采集预览”和“小批量试运行”按钮。HTTP `/api/run` 使用 `run_mode=preview|trial|normal`，`mode=light|deep` 继续决定分析深度；MCP `run_pipeline` 透传同一参数。预览仍会访问来源网络，也可能等待来源超时；它不是模型评分。Web 可保存任务执行记录，文章与来源游标不写入。

裸 `--dry-run` 不再意外运行正式任务：它仅与 `--import-sources` 配合使用。采集预检使用 `--collect-preview`。

## 来源完整性与游标

每源结果记录窗口、原始返回量、实际保留量、成功/完整/截断、下一页位置和错误。文章及准入记录保存成功后，只有来源成功且完整、没有截断才推进成功游标；评分失败不会撤回已经完成的采集。

当前 P0 不自动遍历所有分页。达到来源上限、下一页存在或无法证明完整时，记录 `source_incomplete`，保留旧成功游标和 `source_sync` 恢复元数据；后续重复取同窗口可通过 DOI 去重收敛。不能把“本页成功”理解成整个窗口采集完成。停机较久且始终截断时，需要后续显式分页/回填任务处理，不能靠每天自动跳到现在。

持久化失败时保守地不推进本批来源游标，避免漏采。来源关联保存在 `article_sources`，已存在的 DOI 可增加来源记录，不重复评分。任务锁覆盖 CLI、Web 和定时任务；Windows 使用文件锁而不是空实现。

## arXiv 请求策略

arXiv Terms of Use 要求“每 3 秒不超过 1 次请求，且同时只使用 1 个连接”，且超限后果是限流/封禁而非提供可用的 Retry-After。因此：

- **优先使用分类 RSS**：`https://rss.arxiv.org/rss/<category>` 是静态公告文件，不受查询 API 的 3 秒限制，实测在查询 API 返回 429 时仍可正常返回；同一分类只发 **1 次**请求，订阅用到的所有分类去重后逐个获取。
- **本地过滤关键词**：带关键词的订阅（如 `cat:X AND (all:"..." OR all:"...")`）不再各自发请求，而是在该分类 RSS 结果上本地匹配；语义为并集——整分类订阅保留全部条目，关键词订阅额外保留命中项。
- **只收新公告**：`arxiv:announce_type` 为 `replace` 的条目跳过（它们是旧论文的修订版，公告日期会让它们误判为"新文献"）。
- **查询 API 仅作回退**：RSS 不可用时才使用查询 API，此时仍执行合并查询 + 3 秒主动节流 + 冷却快速失败。
- **主动节流**：每次请求前补齐到 `fetcher.arxiv_min_interval_seconds`（默认 3 秒），对应 `arxiv.py` 客户端 `delay_seconds=3.0` 的做法；命中 429 时仍走共享冷却（60 秒上限）并在冷却期内快速失败。
- **单连接**：复用同一个 `Session`，符合 ToU 的 single connection 要求。
- 每个被合并的订阅仍各自写入一条 source_result，因此游标与健康状态照常推进；文章只归属首个订阅，来源信息通过 `_sources` 记录全部订阅 id。

## 统计口径

- `fetched_raw`：本次来源原始返回量（不是源历史总数）。
- `outside_window`、`needs_date`、`invalid_date`：准入决策数量，发生在批次去重前。
- `cross_source_duplicates`：批次合并重复数量。
- `already_in_db`：数据库已有/屏蔽判断数量。
- `new_eligible`、`new_quarantined`：成功入库后按准入状态拆分。
- `score_queue_total`：全库合并后的可评分积压，包括本轮新文章。
- `score_attempted`：本轮实际开始评分的文章数。
- `score_deferred`：没有分配本轮评分名额的队列记录数。
- `score_succeeded`、`score_failed`：本轮评分结果。
- `llm_requests`：评分/分析所有实际请求，包含失败和内部重试。
- `source_failed`、`source_incomplete`：来源请求失败和未完成同步数量。

不同阶段数字不可直接混减。无日期文章的存在不等于抓取失败；来源同步不完整也不同于模型失败。

## 旧积压处置：默认只读

预览不实例化 `Database`，使用 SQLite `mode=ro`，不自动创建表或触发迁移。输出 ID、决策及收藏/笔记/标签/聊天/划线/专题/日报关联标志，不输出私人正文。

```bash
PYTHONPATH=src python -m utils.backlog --db /path/to/articles.db --date 2026-09-13 --days 3 --timezone Asia/Shanghai
```

只有检查清单并明确决定后，才执行：

```bash
PYTHONPATH=src python -m utils.backlog --db /path/to/articles.db --date 2026-09-13 --days 3 --apply
PYTHONPATH=src python -m utils.backlog --db /path/to/articles.db --restore BATCH_ID
```

执行前应停止针对该库的任务。`--apply` 和 `--restore` 都先通过 SQLite backup API 备份。apply 事务记录迁移批次和逐条旧/新状态，不删除文章或关系；已成功评分及已有准入记录保留。restore 仅恢复仍匹配该批新状态、且尚未成功处理的记录，避免覆盖后续操作，可重复执行。

程序启动只做结构升级，不执行上述真实存量批量分类。本次开发没有对用户真实库执行 apply/restore。

## 验证

固定日期、临时库和模拟接口覆盖：2000篇两轮100/100预算；跨日延期恢复；试运行30篇且无日报；预览零模型和零业务写入；日期时区边界；失败重试份额；来源截断及入库失败不推进；旧库预览、备份、状态恢复及用户关系保留。参见 `tests/test_processing_p0.py`、`test_p0_pipeline_budget.py`、`test_backlog_p0.py`、`test_source_sync_p0.py`、`test_p0_modes.py`、`test_analyzer_requests.py`。
