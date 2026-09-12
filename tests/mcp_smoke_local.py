"""Isolated MCP protocol and workflow checks; no existing workbench is contacted.

Run with the project's Python environment: python tests/mcp_smoke_local.py
"""
import asyncio
from contextlib import contextmanager
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
import web_server
from mcp_e2e import EXPECTED_TOOLS, parse

DATE = "2026-09-10"


@contextmanager
def local_workbench():
    with tempfile.TemporaryDirectory(prefix="dpd-mcp-") as tmp:
        root = Path(tmp)
        cfg = root / "config.yaml"
        cfg.write_text(f"""
database:
  path: {root / 'test.db'}
llm:
  provider: fake
  fake:
    api_key: fake-only
    base_url: https://example.invalid
output:
  output_dir: {root / 'output'}
  email:
    enabled: false
  feishu:
    enabled: false
web:
  api_token: isolated-token
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
            {"doi": f"10.test/{i}", "title": f"Machine learning interatomic potential {i}",
             "pub_date": DATE, "abstract": "Abstract " * 100}
            for i in range(8)
        ])
        conn = ctx.db._conn()
        for i, aid in enumerate(ids):
            conn.execute("UPDATE articles SET relevance=?, created_at=?, analysis=? WHERE id=?",
                         (9 - i / 10, DATE + " 08:00:00", "Analysis " * 200, aid))
        conn.commit()
        server = web_server.ThreadingHTTPServer(("127.0.0.1", 0), web_server.Handler)
        server.context = ctx
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        # Any accidental delivery is a test failure, rather than a real network call.
        with patch.object(web_server.Notifier, "send_digest_files", side_effect=AssertionError("unexpected delivery")):
            thread.start()
            try:
                yield ctx, f"http://127.0.0.1:{server.server_address[1]}"
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)
                ctx.db.close()


async def run_checks(ctx, address):
    env = dict(os.environ)
    env.update(DPD_API=address, DPD_TOKEN="isolated-token", DPD_TIMEOUT="10",
               PYTHONDONTWRITEBYTECODE="1", NO_PROXY="127.0.0.1,localhost",
               no_proxy="127.0.0.1,localhost")
    env["PYTHONPATH"] = os.pathsep.join(dict.fromkeys(str(Path(p).resolve()) for p in sys.path if p))
    params = StdioServerParameters(command=sys.executable,
                                  args=[str(REPO / "src/mcp_server.py")], env=env)
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            names = {t.name for t in (await session.list_tools()).tools}
            assert names == EXPECTED_TOOLS, (names - EXPECTED_TOOLS, EXPECTED_TOOLS - names)

            async def call(name, args):
                error, data = parse(await session.call_tool(name, args))
                assert not error and isinstance(data, dict), (name, data)
                return data

            history = await call("get_digest_history", {"limit": 5})
            assert history["items"] == []
            before = ctx.db._conn().total_changes
            preview = await call("preview_daily_digest", {"date": DATE})
            assert preview["selected_count"] == 5
            assert ctx.db._conn().total_changes == before
            assert not ctx.db.list_digest_versions()
            published = await call("publish_daily_digest", {"date": DATE, "channels": []})
            again = await call("publish_daily_digest", {"date": DATE})
            assert published["version_id"] == again["version_id"] and not again["created"]
            assert ctx.db.list_digest_sends(published["version_id"]) == []
            detail = await call("get_digest", {"version_id": published["version_id"]})
            assert len(detail["items"]) == 5
            for item in detail["items"]:
                assert len(item["snapshot"].get("abstract") or "") <= 300
                assert len(item["snapshot"].get("analysis") or "") <= 800
            expected = [(it["rank"], it["article_id"]) for it in ctx.db.get_digest_items(published["version_id"])]
            assert [(it["rank"], it["article_id"]) for it in detail["items"]] == expected
            history = await call("get_digest_history", {"limit": 5})
            assert len(history["items"]) == 1
            error, data = parse(await session.call_tool("get_digest", {"version_id": 999999}))
            assert error, data
            legacy = await call("today_top_n", {"date": DATE, "commit": True})
            assert "deprecated" in legacy
            assert len(ctx.db.list_digest_versions()) == 1
            print(f"MCP protocol: {len(names)} tools; preview/publish/history/detail/idempotency/errors OK")


def main():
    with local_workbench() as (ctx, address):
        asyncio.run(run_checks(ctx, address))
    print("SMOKE OK")


if __name__ == "__main__":
    main()
