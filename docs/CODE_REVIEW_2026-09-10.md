# 代码审查与修改方向（2026-09-10）

## 范围与结论

基于 main 分支 b73bc00 和审查开始时的工作区，阅读主流水线、Web/API、追踪、Zotero、前端认证、打包与测试。已有未提交变化涉及 README、配置模板、前端，以及删除 CLAUDE.md；本次仅新增/编写 AGENTS.md 和本报告，不提交这些已有变化，也未实施下列业务修复。

上轮规划中的配置环境变量、设置热更新、阶段状态、摘要/全文分离、稳定订阅 ID、阅读工作台和打包资源已有实现，不能继续将旧问题列表整体当作当前缺陷。以下按当前代码重新确认。

## 应优先修复的问题

### P1：读取保护开启后，前端 GET 不携带 Token

证据：`src/static/js/api.js:36-37` 的 get 直接调用 json(url)，没有 headers；`src/web_server.py:1529-1533` 在 protect_read 开启时认证 GET API。

触发：即使浏览器已保存正确 Token，开启读取保护后，文献、设置等 GET 请求仍返回 401。

方向：所有受保护请求复用认证封装；测试必须覆盖保存 Token 后的真实前端请求，而不只是手工带 Header 的后端请求。验收：无 Token 为 401，正确 Token 可读取列表、详情和设置。

### P1：当前未提交的前端删除了输入已有 Token 的入口

证据：工作区 `src/static/index.html` 和 `src/static/js/app.js` 删除 tokenInput/tokenSave/initTokenBox；settings 页仅能通过受保护的保存接口修改服务端 Token。

触发：服务端已配置 Token，新浏览器或清空 localStorage 后无法通过 UI 设置客户端凭据；读取保护开启时连设置页数据也不能加载。

方向：提供不依赖已认证 API 的“连接/输入已有 Token”入口，401 时可进入；与服务端 Token 管理分开。验收：全新浏览器可连接已有 Token 的服务。此问题来自工作区变化，未包含在本次文档提交中。

### P1：同日第二批新增/失败重试仍会替换已有日报

证据：`src/main.py:627-647` 只在没有新增且没有重试时跳过生成；其余情况向 notifier 传入本批 relevant_articles/new_articles，报告按日期文件名写入。

触发：上午有 A，下午新增 B 或重试 C，第二次日报不再包含上午已经成功处理的 A。

方向：建立报告与文章的关联，按当天完整成员集合重新生成，或保留批次报告版本；推送状态独立记录。验收：两批新增、只重试、无新增三种情况均保持日报完整，且可追溯版本。当前 FakeNotifier 只写固定 REPORT，不能验证内容完整性。

### P1：引文采集失败也会推进游标

证据：`src/core/tracking.py:39-46` 使用 last_checked_at 作为查询起点，却在 finally 中调用 mark_seed_checked。

触发：请求超时后更新时间仍前移，下次查询可能跳过失败期间的论文；即使采集成功，当前标记也早于后续文章入库。

方向：区分 last_attempt_at 与 last_success_at，采集并持久化完成才提交成功游标，保留重叠窗口去重。验收：模拟请求失败或入库失败，下一次仍覆盖上次窗口。

## 下一批改进

1. **报告读取的认证边界**：protect_read 目前只检查 `/api/`，`/paper-index` 和 `/reports/*` 仍可直读（web_server.py:1563 起）。若用于保护研究内容，应覆盖生成文件；同步处理 iframe/下载的认证方式，不能只机械添加 Header 检查。
2. **追踪多对多关系**：tracking.py 用全局 seen_dois 跳过已见文章，citing_seed 又只保存一个 seed_id。相同文章引用两个关注种子时会丢关系。文章去重与边去重应独立，补充双种子样例。
3. **失败统计完整性**：main.py 的索引失败只记日志；任务 partial 判断没有包含 push_results 中的失败。应返回各阶段结果，并测试“模型成功、邮件失败”不会被报告为完全成功。
4. **Zotero 幂等核验**：zotero_client.py::find_item_by_doi 读取小写 doi，而创建模板使用大写 DOI。补充真实结构的模拟响应并统一边界字段；查重失败不要直接当作未找到后创建。未执行真实 Zotero 请求。
5. **资源与测试隔离**：run_once 的 finally 仅释放锁，检查 Database、Fetcher、模型客户端的关闭；test_pipeline.py 直接覆盖 main 模块类且未恢复，改为可清理的 patch；现有测试亦出现未关闭配置文件的 ResourceWarning。
6. **交付卫生**：仓库追踪了 35 个 build/ 副本，建议单独提交移除并忽略构建目录；README 工作区仍引用已删除的 CLAUDE.md，且仍说明侧栏输入 Token，需要随对应前端变更一起修正。不要把生成副本当作维护入口。

## 实际验证

- 使用 Python 3.12 与仓库已有 `.deps`，从 `/tmp` 运行 unittest，源码/tests 使用绝对路径，禁写 pyc；没有读取真实 config.yaml 或操作生产数据库。
- 首次受沙箱 socket 限制；允许 localhost 监听后又遇环境代理 502；为 localhost 设置 NO_PROXY/no_proxy 后，**30 个测试全部通过**。
- `node --check` 检查 src/static/js 下全部 JavaScript，通过。
- 测试通过不覆盖上述遗漏的用户路径；本次未进行真实浏览器交互、干净 wheel 安装、真实 LLM、RSS、Zotero、邮件或飞书验证。
- 建议顺序：认证入口与 GET → 日报完整性 → 追踪游标及关联 → 任务结果与外部集成 → 模块拆分。每项用独立提交和针对性回归测试交付。
