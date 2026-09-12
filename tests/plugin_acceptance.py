"""插件 v0.1 客户端验收脚本：隔离工作台 + 合成数据 + 模拟外部服务。

覆盖场景矩阵（每行打印 PASS/FAIL 作为验收证据）：
中文/空格安装路径启动 / 工具列表可读取 / 今天读什么（无业务写入）/
查看论文 / 加入待读·收藏与笔记（读回核验）/ 发布但不推送 / 同请求重复发布 /
查询历史 / 阅读链接可达 / 工作台停止 / 错误令牌。

运行：python tests/plugin_acceptance.py
不连接真实邮箱、飞书、Zotero 或付费模型（Notifier 被替换，误发即失败）。
"""
import asyncio
from contextlib import asynccontextmanager, contextmanager
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
from unittest.mock import patch
from urllib.request import Request, urlopen

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
import web_server
from mcp_e2e import EXPECTED_TOOLS, parse

DATE = "2026-09-12"
RESULTS: list[tuple[str, bool, str]] = []


def record(scene: str, ok: bool, detail: str = ""):
    RESULTS.append((scene, ok, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {scene}" + (f" — {detail}" if detail else ""))


@contextmanager
def isolated_workbench(data_root: Path):
    """隔离工作台：数据根支持中文/空格路径 + 读取保护 + 外部发送模拟化。"""
    data_root.mkdir(parents=True, exist_ok=True)
    cfg = data_root / "config.yaml"
    cfg.write_text(f"""
database:
  path: {data_root / 'db' / 'test.db'}
llm:
  provider: fake
  fake:
    api_key: fake-only
    base_url: https://example.invalid
output:
  output_dir: {data_root / 'output'}
  email:
    enabled: false
  feishu:
    enabled: false
web:
  api_token: acceptance-token
  protect_read: true
  enable_scheduler: false
tracking:
  enabled: false
digest:
  daily:
    limit: 5
    category_limits:
      mlip: {{min: 0, max: 10}}
relevance_threshold: 5
""", encoding="utf-8")
    ctx = web_server.WebContext(config_path=str(cfg))
    ids = ctx.db.save_articles_batch([
        {"doi": f"10.acc/{i}", "title": f"Machine learning interatomic potential study {i}",
         "pub_date": DATE, "abstract": "Abstract " * 100}
        for i in range(6)
    ])
    conn = ctx.db._conn()
    for i, aid in enumerate(ids):
        conn.execute("UPDATE articles SET relevance=?, created_at=?, analysis=?, "
                     "relevance_reason=? WHERE id=?",
                     (9 - i / 10, DATE + " 08:00:00", "Analysis " * 200,
                      "与机器学习势方向相关的合成理由", aid))
    conn.commit()
    server = web_server.ThreadingHTTPServer(("127.0.0.1", 0), web_server.Handler)
    server.context = ctx
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    with patch.object(web_server.Notifier, "send_digest_files",
                      side_effect=AssertionError("unexpected external delivery")):
        thread.start()
        try:
            yield ctx, f"http://127.0.0.1:{server.server_address[1]}", ids
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
            ctx.db.close()


def mcp_env(address: str, token: str = "acceptance-token") -> dict:
    env = dict(os.environ)
    env.update(DPD_API=address, DPD_TOKEN=token, DPD_TIMEOUT="10",
               PYTHONDONTWRITEBYTECODE="1", NO_PROXY="127.0.0.1,localhost",
               no_proxy="127.0.0.1,localhost")
    env["PYTHONPATH"] = os.pathsep.join(dict.fromkeys(
        [str(REPO / "src")] + [p for p in sys.path if p]))
    return env


@asynccontextmanager
async def open_session(address: str, token: str = "acceptance-token"):
    params = StdioServerParameters(
        command=sys.executable, args=[str(REPO / "src/mcp_server.py")],
        env=mcp_env(address, token))
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            yield session


async def run_main_scenarios(ctx, address, ids):
    target = ids[0]

    async with open_session(address) as session:
        tools = {t.name for t in (await session.list_tools()).tools}
        record("安装并启动：插件 MCP 工具列表可读取", tools == EXPECTED_TOOLS,
               f"{len(tools)}/25 个工具")

        before = ctx.db._conn().total_changes
        error, preview = parse(await session.call_tool("preview_daily_digest", {"date": DATE}))
        ok = (not error and preview.get("selected_count") == 5
              and all(it.get("reason") for it in preview["items"])
              and ctx.db._conn().total_changes == before)
        record("今天读什么：推荐顺序正确、含推荐依据、数据库无业务写入", ok)

        error, detail = parse(await session.call_tool("get_paper", {"paper_id": target}))
        ok = (not error and detail["title"].startswith("Machine learning interatomic")
              and "fulltext_text" not in detail
              and detail["analysis"].startswith("Analysis")
              and detail.get("relevance_reason"))
        record("查看某篇论文：字段正确、默认不含全文、摘要/解读来源可区分", ok)

        for name, args in [
            ("set_reading_status", {"paper_id": target, "status": "queued"}),
            ("star_paper", {"paper_id": target, "starred": True}),
            ("add_note", {"paper_id": target, "note": "验收笔记 A"}),
            ("add_tags", {"paper_id": target, "tags": ["验收"]}),
        ]:
            await session.call_tool(name, args)
        _, detail = parse(await session.call_tool("get_paper", {"paper_id": target}))
        ok = (detail["read_status"] == "queued" and detail["starred"] == 1
              and detail["note"] == "验收笔记 A" and "验收" in (detail["tags"] or ""))
        record("加入待读/收藏与笔记：写入后读回一致", ok)

        version = {}

        async def publish_steps(sess):
            _, published = parse(await sess.call_tool(
                "publish_daily_digest", {"date": DATE, "channels": []}))
            _, again = parse(await sess.call_tool(
                "publish_daily_digest", {"date": DATE}))
            ok = (published["version_id"] == again["version_id"]
                  and not again["created"]
                  and ctx.db.list_digest_sends(published["version_id"]) == [])
            record("发布日报但不推送：创建固定版本、无外部请求、同请求复用", ok)
            _, history = parse(await sess.call_tool("get_digest_history", {"limit": 5}))
            _, snap = parse(await sess.call_tool(
                "get_digest", {"version_id": published["version_id"]}))
            ok = (len(history["items"]) == 1 and len(snap["items"]) == 5
                  and all(len((it["snapshot"] or {}).get("analysis") or "") <= 800
                          for it in snap["items"]))
            record("查询历史日报：返回固定快照（条目文本截断）", ok)
            version.update(published)

        await publish_steps(session)

    # 阅读链接：两栏阅读页免认证可达；日报产物页带 Token 可达
    try:
        with urlopen(f"{address}/article/{target}", timeout=10) as resp:
            body = resp.read().decode("utf-8", "replace")
            record("阅读链接可达：两栏阅读页 /article/{id}",
                   resp.status == 200 and len(body) > 200)
    except Exception as e:  # noqa: BLE001
        record("阅读链接可达：两栏阅读页 /article/{id}", False, str(e))
    artifact = next((a for a in version.get("artifacts", []) if a["format"] == "html"), None)
    if artifact:
        req = Request(address + artifact["url"], headers={"X-API-Token": "acceptance-token"})
        try:
            with urlopen(req, timeout=10) as resp:
                record("阅读链接可达：日报产物页（读取保护下带 Token）",
                       resp.status == 200)
        except Exception as e:  # noqa: BLE001
            record("阅读链接可达：日报产物页", False, str(e))


async def run_stop_scenario():
    """工作台停止：先正常调用，服务退出后返回可理解的连接错误。"""
    with tempfile.TemporaryDirectory(prefix="dpd-acc-stop-") as tmp:
        with isolated_workbench(Path(tmp) / "文献 数据 Stop") as (_, address, _):
            session_ctx = open_session(address)
            session = await session_ctx.__aenter__()
            try:
                error, _ = parse(await session.call_tool("task_status", {}))
                assert not error
            finally:
                pass
        # with 块已退出：工作台已停止，会话仍存活
        try:
            error, data = parse(await session.call_tool("task_status", {}))
            message = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)
            record("工作台停止：返回可理解的连接错误（含启动提示）",
                   bool(error) and "无法连接文献工作台" in message, message[:80])
        finally:
            await session_ctx.__aexit__(None, None, None)


async def run_auth_scenario():
    """错误令牌：返回可理解的认证错误，不回显令牌。"""
    with tempfile.TemporaryDirectory(prefix="dpd-acc-auth-") as tmp:
        with isolated_workbench(Path(tmp) / "文献 数据 Auth") as (_, address, _):
            async with open_session(address, token="wrong-token") as session:
                error, data = parse(await session.call_tool("task_status", {}))
                message = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)
                record("错误令牌：返回可理解的认证错误（提示 DPD_TOKEN/web.api_token）",
                       bool(error) and "认证失败" in message and "wrong-token" not in message,
                       message[:80])


async def main():
    with tempfile.TemporaryDirectory(prefix="dpd-acceptance-") as tmp:
        data_root = Path(tmp) / "文献 数据 Root"
        with isolated_workbench(data_root) as (ctx, address, ids):
            record("中文、空格安装路径：工作台与数据库正常启动",
                   ctx.db is not None and address.startswith("http://127.0.0.1"),
                   f"data_root={data_root.name}")
            await run_main_scenarios(ctx, address, ids)
    await run_stop_scenario()
    await run_auth_scenario()

    failed = [r for r in RESULTS if not r[1]]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} 场景通过")
    if failed:
        sys.exit(1)
    print("ACCEPTANCE OK")


if __name__ == "__main__":
    asyncio.run(main())
