"""Opt-in external read-only MCP checks.

Requires MCP_E2E_ALLOW_EXTERNAL=1 and an explicit DPD_E2E_API test URL.
For an automatically isolated workbench use mcp_smoke_local.py.
"""
import asyncio
import json
import os
from pathlib import Path
import sys

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

EXPECTED_TOOLS = {
    "search_papers", "get_paper", "today_top_n", "preview_daily_digest", "get_digest",
    "get_digest_history", "publish_daily_digest", "trends", "set_reading_status",
    "star_paper", "add_note", "add_tags", "list_topics", "get_topic", "create_topic",
    "add_papers_to_topic", "push_to_zotero", "watch_paper", "watch_author", "run_pipeline",
    "task_status", "list_reports", "compare_papers", "related_work_draft", "chat_with_paper",
}


def parse(result):
    if getattr(result, "structuredContent", None) is not None:
        return bool(result.isError), result.structuredContent
    for block in result.content:
        text = getattr(block, "text", None)
        if text:
            try:
                return bool(result.isError), json.loads(text)
            except json.JSONDecodeError:
                return bool(result.isError), text
    return bool(result.isError), None


async def main():
    address = os.environ.get("DPD_E2E_API", "").strip()
    if os.environ.get("MCP_E2E_ALLOW_EXTERNAL") != "1" or not address:
        print("拒绝运行：设置 MCP_E2E_ALLOW_EXTERNAL=1 和 DPD_E2E_API 测试地址。")
        return 2
    env = dict(os.environ, DPD_API=address)
    env["PYTHONPATH"] = os.pathsep.join(str(Path(p).resolve()) for p in sys.path if p)
    params = StdioServerParameters(command=sys.executable,
                                  args=[str(REPO / "src/mcp_server.py")], env=env)
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            assert {t.name for t in (await session.list_tools()).tools} == EXPECTED_TOOLS

            async def call(name, args):
                error, data = parse(await session.call_tool(name, args))
                assert not error and isinstance(data, dict), (name, data)
                return data

            papers = await call("search_papers", {"query": "machine learning"})
            if papers.get("items"):
                aid = papers["items"][0]["id"]
                detail = await call("get_paper", {"paper_id": aid})
                assert "fulltext_text" not in detail
            await call("preview_daily_digest", {})
            history = await call("get_digest_history", {"limit": 5})
            if history.get("items"):
                vid = history["items"][0]["version_id"]
                assert (await call("get_digest", {"version_id": vid}))["version_id"] == vid
            await call("task_status", {})
            await call("list_reports", {})
    print("READ-ONLY E2E OK")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
