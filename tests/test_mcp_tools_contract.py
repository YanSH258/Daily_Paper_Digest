"""MCP 工具契约回归：工具清单与读写语义。

只读工具不得带写开关；正式发布只允许通过显式的写工具。不联网、不起服务。
需要 MCP SDK（`pip install -e ".[mcp]"`）；未安装时跳过工具清单部分，
源码级契约仍然会检查。
"""
import asyncio
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import mcp_server  # noqa: E402

EXPECTED_TOOLS = 25
MCP_SOURCE = Path(mcp_server.__file__).read_text(encoding="utf-8")


def _mcp_sdk_available() -> bool:
    try:
        import mcp.server.fastmcp  # noqa: F401
        return True
    except Exception:  # noqa: BLE001 - 只影响测试是否能校验实时 schema
        return False


def _tools() -> dict:
    server = mcp_server._build_server()
    listed = asyncio.run(server.list_tools())
    return {t.name: t for t in listed}


def _signature_params(tool_name: str) -> set[str]:
    """从源码取某个工具的形参名（无需 MCP SDK）。"""
    m = re.search(rf"def {tool_name}\((.*?)\) -> dict:", MCP_SOURCE, re.S)
    assert m, f"未找到工具 {tool_name} 的定义"
    return {p.split(":")[0].strip() for p in m.group(1).split(",") if p.strip()}


class ToolSourceContractTests(unittest.TestCase):
    """不依赖 MCP SDK 的源码级契约。"""

    def test_today_top_n_has_no_commit_switch(self):
        self.assertEqual(_signature_params("today_top_n"), {"date"})

    def test_no_tool_writes_legacy_digest_history(self):
        """legacy 落盘路径（POST /api/digest）不得再被任何 MCP 工具调用。"""
        self.assertNotIn('_call("POST", "/api/digest"', MCP_SOURCE,
                         "MCP 层不应再写 legacy 选择历史；发布走 publish_daily_digest")

    def test_tool_definitions_carry_docstrings(self):
        for name in re.findall(r"@mcp\.tool\(\)\s*\n\s*def (\w+)\(", MCP_SOURCE):
            with self.subTest(tool=name):
                m = re.search(rf"def {name}\(.*?\) -> dict:\s*\n\s*\"\"\"(.+?)\"\"\"",
                              MCP_SOURCE, re.S)
                self.assertIsNotNone(m, f"{name} 缺少 docstring")
                self.assertTrue(m.group(1).strip(), f"{name} 的 docstring 为空")


@unittest.skipUnless(_mcp_sdk_available(), "需要 MCP SDK：pip install -e '.[mcp]'")
class ToolSchemaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tools = _tools()

    def test_tool_count(self):
        self.assertEqual(len(self.tools), EXPECTED_TOOLS,
                         f"工具数量变了：{sorted(self.tools)}")

    def test_today_top_n_is_read_only(self):
        """旧版 Top-N 只读：不得再暴露 commit 之类的落盘开关。"""
        props = (self.tools["today_top_n"].inputSchema or {}).get("properties") or {}
        self.assertNotIn("commit", props,
                         "today_top_n 不应再有 commit 参数：发布走 publish_daily_digest")
        self.assertEqual(set(props), {"date"})

    def test_preview_and_publish_are_separate_tools(self):
        """读预览与写发布必须是两个工具，agent 才能从清单上看清语义。"""
        self.assertIn("preview_daily_digest", self.tools)
        self.assertIn("publish_daily_digest", self.tools)
        publish_props = (self.tools["publish_daily_digest"].inputSchema or {}).get("properties") or {}
        self.assertIn("channels", publish_props, "发布工具应显式接收投递渠道")

    def test_read_tools_have_no_write_switches(self):
        """只读工具不接受 commit/dry_run 这类"假装只读"的开关。"""
        for name in ("preview_daily_digest", "search_papers", "get_paper", "today_top_n"):
            with self.subTest(tool=name):
                props = (self.tools[name].inputSchema or {}).get("properties") or {}
                self.assertNotIn("commit", props)
                self.assertNotIn("dry_run", props)

    def test_write_tools_are_declared_as_writes(self):
        """写操作在 docstring 里必须写明，便于 agent 判断副作用。"""
        write_tools = ("set_reading_status", "star_paper", "add_note", "add_tags",
                       "create_topic", "add_papers_to_topic", "push_to_zotero",
                       "watch_paper", "watch_author", "run_pipeline",
                       "publish_daily_digest")
        for name in write_tools:
            with self.subTest(tool=name):
                description = (self.tools[name].description or "")
                self.assertIn("写操作", description,
                              f"{name} 的说明应标明副作用: {description[:60]}")

    def test_read_tools_do_not_claim_writes(self):
        """只读工具不得声称自己是写操作。"""
        for name in ("search_papers", "get_paper", "today_top_n", "get_digest",
                     "get_digest_history", "list_topics", "get_topic", "trends",
                     "task_status", "list_reports"):
            with self.subTest(tool=name):
                description = (self.tools[name].description or "")
                self.assertNotIn("【写操作】", description, f"{name} 是只读工具")


if __name__ == "__main__":
    unittest.main()
