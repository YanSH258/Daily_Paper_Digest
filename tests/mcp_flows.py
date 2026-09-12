"""Opt-in, strictly read-only MCP workflow/context audit.

Set MCP_FLOW_ALLOW_EXTERNAL=1 and DPD_E2E_API explicitly. For isolated protocol
coverage run mcp_smoke_local.py instead. No fallback to arbitrary article IDs.
"""
import asyncio
import os
from pathlib import Path
import sys
import json

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp_e2e import EXPECTED_TOOLS, parse


async def main():
    address = os.environ.get("DPD_E2E_API", "").strip()
    if os.environ.get("MCP_FLOW_ALLOW_EXTERNAL") != "1" or not address:
        print("拒绝运行：设置 MCP_FLOW_ALLOW_EXTERNAL=1 和 DPD_E2E_API 测试地址。")
        return 2
    env = dict(os.environ, DPD_API=address)
    env["PYTHONPATH"] = os.pathsep.join(str(Path(p).resolve()) for p in sys.path if p)
    params = StdioServerParameters(command=sys.executable,
                                  args=[str(REPO / "src/mcp_server.py")], env=env)
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            assert {t.name for t in (await session.list_tools()).tools} == EXPECTED_TOOLS

            async def call(name, args, maximum=30000):
                error, data = parse(await session.call_tool(name, args))
                assert not error and isinstance(data, dict), (name, data)
                size = len(json.dumps(data, ensure_ascii=False))
                assert size <= maximum, (name, size)
                print(f"{name}: {size} 字符")
                return data

            await call("preview_daily_digest", {})
            history = await call("get_digest_history", {"limit": 5})
            if history.get("items"):
                await call("get_digest", {"version_id": history["items"][0]["version_id"]}, 50000)
            queued = await call("search_papers", {"read_status": "queued"})
            if queued.get("items"):
                await call("get_paper", {"paper_id": queued["items"][0]["id"]})
            await call("search_papers", {"min_score": 8, "limit": 10})
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
