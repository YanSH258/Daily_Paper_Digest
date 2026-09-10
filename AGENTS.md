# Daily Paper Digest — Agent 工作约定

## 项目目标与入口

本项目是面向化学/材料研究的个人文献工作台：RSS 与作者/引文追踪 → 去重 → 相关性评分 → 全文获取 → AI 解读 → 日报/周报与推送，同时提供阅读清单、专题、笔记、问答和 Zotero 集成。

开始工作时先阅读本文件、README.md 和相关代码，检查 `git status --short`。历史审查见 `docs/CODE_REVIEW_2026-09-10.md`；审查中的问题是当时快照，实施前必须重新确认。用户明确限定“审查/规划”时只分析或编写指定文档，不自动执行产品改造。

## 源码地图

- `src/main.py`：配置加载、环境变量、RunLock、分阶段流水线、调度、周报及备份入口；`--digest-dry-run` / `--digest` 为 Phase0 Top-N 选择。
- `src/digest/`：Phase0 每日 Top-N（规则分类、freshness、软配额选择器、dry-run 解释）。
- `src/core/db.py`：SQLite、迁移、文章处理状态、阅读数据、专题、digest_entries 和任务记录。
- `src/core/analyzer.py`：评分、分析、证据上下文、流式问答和模型用量。
- `src/core/fetcher.py`、`src/fetchers/`：RSS、HTML/PDF/OA/浏览器回退；共享契约以 `fetchers/models.py` 为准。
- `src/core/tracking.py`、`src/integrations/`：作者/引文发现、OpenAlex、Zotero、WoS。
- `src/core/notifier.py`、`src/utils/`：报告、推送、引用导出、周报、相似度和期刊指标。
- `src/web_server.py`：标准库 ThreadingHTTPServer、API、TaskRunner、设置热更新和静态资源。
- `src/mcp_server.py`：MCP 服务器（`daily-paper-mcp`），把 HTTP API 包装为 21 个语义化工具供 AI agent 调用；薄层转换，业务逻辑不得下沉到此处。
- `src/static/`：原生 HTML/CSS/JavaScript，无前端构建步骤；页面使用共享 API 封装。
- `tests/`：unittest 测试；`setup.py` 负责 CLI 和 webassets 打包。
- `build/` 是生成副本，不在其中修复业务逻辑，也不通过手动同步它来替代构建验证。

## 数据与配置约束

- 不提交实际 `config/config.yaml`、API Key、邮件密码、用户数据库、日志、全文、报告或本地依赖。示例配置仅使用占位值。
- 保留现有未提交修改；只暂存本任务明确涉及的文件，禁止为方便提交而使用 `git add .`、覆盖式 checkout 或强制推送。
- 迁移保留文章 ID、收藏、笔记、标签、聊天、专题关系和外部同步标识。使用 SQLite backup API 备份，迁移可重复执行，并在临时旧库样例上验证。
- 摘要与全文分别保存；证据等级由 FetchResult 判定并持久化，不能按文本长度猜测全文，也不能将 AI 解读当成原文。
- 去重与处理状态分离。失败不能伪装成低相关或成功；重试保留成功阶段。日报同日更新和推送重试必须有明确幂等语义。
- 追踪游标仅在成功完成相应采集及持久化后推进；失败应可重试，同一文章可关联多个来源。
- CLI 与 Web 使用同一配置加载和环境变量优先级。设置先校验、再原子写入，再同步运行时对象；运行中的任务使用配置快照。
- Token 留空、不修改、替换、清除必须语义明确。读取保护开启时检查共享 GET 封装、报告访问和新浏览器认证入口。
- 用户已经明确授权的操作无需重复确认；普通测试使用模拟服务。真实付费模型、整库重分析、邮件/飞书发送、Zotero 写入只在任务明确包含这些操作时执行。

## 实现习惯

- 延续 Python + SQLite + 原生 JS；模块拆分随实际变更推进，不无故整体更换框架。
- 复用共享数据模型、数据库方法、配置和 API 封装，避免新增另一套状态或认证实现。
- 外部请求设置超时，区分认证、限流、暂时失败和解析错误；日志不暴露凭据。数据库连接、浏览器、流和锁在异常路径也要释放。
- 展示论文或模型文本时转义 HTML、限制链接协议。UI 显示加载、空数据、失败和部分成功状态；保存失败不得显示成功。
- 科研结论、期刊指标、推荐理由须保留来源与时间；原文事实、模型推断和个人笔记明确区分。

## 验证方法

使用项目可用的 Python 环境，不依赖特定机器的绝对路径。正常安装方式为 `python3 -m pip install -r requirement.txt`；没有安装依赖时先报告环境情况，不把缺依赖归为业务失败。

从仓库根目录运行：

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src NO_PROXY=127.0.0.1,localhost no_proxy=127.0.0.1,localhost python3 -m unittest discover -s tests -v
for f in src/static/js/*.js; do node --check "$f" || exit; done
git diff --check
```

本地已有 `.deps` 且兼容当前解释器时，可临时使用 `PYTHONPATH=src:.deps`；这不替代干净安装验证。为避免测试默认输出落到真实数据目录，可从临时目录启动测试，并将源码和 tests 参数改为绝对路径。

- 测试使用临时数据库/输出目录、假凭据及模拟 LLM、抓取、推送和第三方服务；HTTP 测试只绑定 localhost 临时端口。
- 行为修复添加覆盖实际触发条件的回归测试，不仅测试内部辅助函数。Mock 使用 patch/context manager 或 addCleanup 恢复。
- 前端行为改动除语法检查外，应验证完整用户路径；语法通过不等于浏览器交互正确。
- 涉及安装时构建并在干净环境验证 wheel、CLI 和静态资源；涉及版本声明时验证最低支持 Python。
- 文档变更检查命令、链接、路径和差异即可；如果进行全库审查，记录实际测试结果及未验证范围。

## 提交与交接

完成后说明修改内容、验证结果、已知问题和未执行的外部操作。用户要求 commit/push 时，检查暂存差异及敏感信息，提交明确范围，使用正常 push，并核对远端提交 SHA；若推送失败，保留本地提交并报告具体原因，不重置或强推。与本任务无关的工作区修改保持原状。
