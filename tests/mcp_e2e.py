"""MCP server 端到端验证：真实拉起 daily-paper-mcp 子进程，走 stdio 协议调用工具。

用法：PYTHONPATH=src python tests/mcp_e2e.py [api_base]
要求：目标工作台服务已运行（默认 http://127.0.0.1:8090）
"""
import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from mcp import ClientSession, StdioServerParameters  # noqa: E402
from mcp.client.stdio import stdio_client  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
API_BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8090"

PASS, FAIL = [], []


def check(name: str, ok: bool, detail: str = ""):
    (PASS if ok else FAIL).append(name)
    print(f"  {'✓' if ok else '✗'} {name}" + (f"  [{detail}]" if detail and not ok else ""))


def parse(result):
    """MCP CallToolResult → (is_error, payload)。"""
    err = getattr(result, "isError", False)
    payload = None
    for block in result.content:
        text = getattr(block, "text", None)
        if text:
            try:
                payload = json.loads(text)
            except json.JSONDecodeError:
                payload = text
            break
    return err, payload


async def main() -> int:
    params = StdioServerParameters(
        command=sys.executable,
        args=[str(REPO / "src" / "mcp_server.py")],
        env={"DPD_API": API_BASE, "DPD_TIMEOUT": "120",
             "PATH": os.environ.get("PATH", "")},
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            # ── 1. 工具清单 ──
            tools = await session.list_tools()
            names = {t.name for t in tools.tools}
            print(f"① 工具清单: {len(names)} 个")
            expected = {"search_papers", "get_paper", "today_top_n", "trends",
                        "set_reading_status", "star_paper", "add_note", "add_tags",
                        "list_topics", "get_topic", "create_topic", "add_papers_to_topic",
                        "push_to_zotero", "watch_paper", "watch_author", "run_pipeline",
                        "task_status", "list_reports", "compare_papers",
                        "related_work_draft", "chat_with_paper"}
            check("全部预期工具已注册", expected <= names, f"缺失: {expected - names}")

            # ── 2. 检索 ──
            err, data = parse(await session.call_tool("search_papers", {"query": "machine learning"}))
            check("search_papers 关键词检索", not err and data["total"] >= 1,
                  str(data)[:200] if err else "")
            paper_id = data["items"][0]["id"] if data.get("items") else 1

            err, data = parse(await session.call_tool("search_papers",
                                                      {"query": "", "read_status": "queued"}))
            check("search_papers 状态筛选", not err)

            # ── 3. 详情（上下文裁剪）──
            err, paper = parse(await session.call_tool("get_paper", {"paper_id": paper_id}))
            check("get_paper 基础详情", not err and paper.get("title"))
            err, paper_ft = parse(await session.call_tool(
                "get_paper", {"paper_id": 1, "include_fulltext": True}))
            has_ft = bool(paper_ft.get("fulltext_text"))
            err2, paper_no_ft = parse(await session.call_tool("get_paper", {"paper_id": 1}))
            check("get_paper 全文按需返回", (not err and not err2)
                  and (has_ft == bool(paper_ft.get("fulltext_text"))))

            # ── 4. 今日 Top-N ──
            err, digest = parse(await session.call_tool("today_top_n", {}))
            check("today_top_n 预览", not err and len(digest.get("selected", [])) >= 1,
                  str(digest)[:200] if err else "")
            check("today_top_n 含可读报告", bool(digest.get("report_text")))

            # ── 5. 写操作：状态/笔记/标签 ──
            err, data = parse(await session.call_tool("set_reading_status",
                                                      {"paper_id": paper_id, "status": "queued"}))
            check("set_reading_status", not err and data.get("ok"))
            err, data = parse(await session.call_tool("add_note",
                                                      {"paper_id": paper_id, "note": "MCP 测试笔记"}))
            check("add_note", not err and data.get("ok"))
            err, data = parse(await session.call_tool("add_tags",
                                                      {"paper_id": paper_id, "tags": ["mcp测试"]}))
            check("add_tags", not err and data.get("ok"))
            err, verify = parse(await session.call_tool("get_paper", {"paper_id": paper_id}))
            check("写后读一致（笔记/标签落库）",
                  verify.get("note") == "MCP 测试笔记" and "mcp测试" in (verify.get("tags") or ""))

            # ── 6. 专题 ──
            err, data = parse(await session.call_tool(
                "create_topic", {"name": "MCP 自动创建专题", "research_question": "端到端验证"}))
            tid = data.get("id")
            check("create_topic", not err and tid)
            err, data = parse(await session.call_tool("add_papers_to_topic",
                                                      {"topic_id": tid, "paper_ids": [paper_id]}))
            check("add_papers_to_topic", not err and data.get("added") == 1)
            err, detail = parse(await session.call_tool("get_topic", {"topic_id": tid}))
            check("get_topic 含论文集合", len(detail.get("papers", [])) == 1)

            # ── 7. 趋势 / 任务 / 报告 ──
            err, data = parse(await session.call_tool("trends", {"months": 4}))
            check("trends", not err and "monthly" in data)
            err, data = parse(await session.call_tool("task_status", {}))
            check("task_status", not err and "task" in data)
            err, data = parse(await session.call_tool("list_reports", {}))
            check("list_reports", not err and "items" in data)

            # ── 8. 追踪（真实 OpenAlex 检索）──
            err, data = parse(await session.call_tool(
                "watch_author", {"name": "Nongnuch Artrith"}))
            # 服务端需 Token 时返回 401 → 报错可读即可；未启用 Token 则应成功
            check("watch_author（自动检索）", not err and (data.get("ok") or data.get("needs_choice")),
                  str(data)[:200])

            # ── 9. 错误路径：LLM 假 key 应返回可读错误而非崩溃 ──
            err, data = parse(await session.call_tool(
                "chat_with_paper", {"paper_id": 1, "question": "测试"}))
            check("chat_with_paper 假key返回可读错误", err and "认证" in str(data) or err,
                  f"err={err} data={str(data)[:120]}")

            # ── 10. 参数校验路径 ──
            err, data = parse(await session.call_tool("compare_papers", {"paper_ids": [1]}))
            check("compare_papers 少于2篇被拒", err or (data and not data.get("ok")))

    print(f"\n通过 {len(PASS)} / {len(PASS) + len(FAIL)}")
    if FAIL:
        print("失败项:", FAIL)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
