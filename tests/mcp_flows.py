"""模拟 agent 真实工作流：晨间简报 / 文献整理。检查每次调用的上下文体积。"""
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


def parse(result):
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


def size_of(payload) -> int:
    return len(json.dumps(payload, ensure_ascii=False)) if not isinstance(payload, str) else len(payload)


async def main() -> int:
    params = StdioServerParameters(
        command=sys.executable,
        args=[str(REPO / "src" / "mcp_server.py")],
        env={"DPD_API": API_BASE, "PATH": os.environ.get("PATH", "")},
    )
    issues = []
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            print("──── 晨间简报流程 ────")
            err, digest = parse(await session.call_tool("today_top_n", {}))
            n = size_of(digest)
            print(f"1. today_top_n → {n} 字符")
            if n > 20000:
                issues.append(f"today_top_n 载荷过大: {n}")
            top = (digest.get("selected") or [{}])[0]
            print(f"   汇报示例: #{top.get('rank')} {str(top.get('title'))[:50]}")
            print(f"   理由: {str(top.get('relevance_reason'))[:60]}")

            err, queued = parse(await session.call_tool("search_papers", {"read_status": "queued"}))
            n = size_of(queued)
            print(f"2. search_papers(queued) → {n} 字符, {queued.get('total')} 篇待读")
            if n > 30000:
                issues.append(f"search 载荷过大: {n}")
            for it in (queued.get("items") or [])[:3]:
                if len(it.get("abstract") or "") > 300:
                    issues.append("search 结果 abstract 未裁剪")
                    break

            err, paper = parse(await session.call_tool(
                "get_paper", {"paper_id": (queued.get("items") or [{}])[0].get("id", 1)}))
            n = size_of(paper)
            print(f"3. get_paper(不含全文) → {n} 字符")
            if n > 30000:
                issues.append(f"get_paper 不含全文仍过大: {n}")

            print("──── 整理流程 ────")
            err, highs = parse(await session.call_tool("search_papers", {"min_score": 8, "limit": 10}))
            print(f"1. 高分文献 → {size_of(highs)} 字符, {highs.get('total')} 篇")
            err, data = parse(await session.call_tool("star_paper",
                                                      {"paper_id": (highs.get("items") or [{}])[0].get("id", 1),
                                                       "starred": True}))
            print(f"2. star_paper → ok={data.get('ok')}" if not err else f"2. star_paper → {data}")

            print("──── 上下文审计 ────")
            if issues:
                for i in issues:
                    print("  ✗", i)
            else:
                print("  ✓ 所有载荷在合理范围内")

    return 1 if issues else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
