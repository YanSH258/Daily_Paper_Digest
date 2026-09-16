"""插件清单一致性回归：ZCode 与 Codex 两套清单必须描述同一个插件。

覆盖：两套清单的名称/版本/MCP 命令一致、Skill 文件存在且被两套清单指向、
市场清单指向真实存在的插件目录、清单里的工具数与 MCP 服务器实际暴露的数量一致。
只读文件，不启动任何进程、不联网。
"""
import json
import re
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PLUGIN = REPO / "plugin"
ZCODE_MANIFEST = PLUGIN / ".zcode-plugin/plugin.json"
CODEX_MANIFEST = PLUGIN / ".codex-plugin/plugin.json"
CODEX_MCP = PLUGIN / ".mcp.json"
ZCODE_MARKET = PLUGIN / "marketplace.json"
ROOT_ZCODE_MARKET = REPO / "marketplace.json"
CODEX_MARKET = REPO / ".agents/plugins/marketplace.json"
SKILL = PLUGIN / "skills/daily-papers/SKILL.md"
MCP_SERVER = REPO / "src/mcp_server.py"


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


class PluginManifestTests(unittest.TestCase):
    def test_all_manifest_files_exist(self):
        for path in (ZCODE_MANIFEST, CODEX_MANIFEST, CODEX_MCP, ZCODE_MARKET,
                     ROOT_ZCODE_MARKET, CODEX_MARKET, SKILL):
            self.assertTrue(path.exists(), f"缺少插件文件: {path}")

    def test_skill_frontmatter_is_valid(self):
        text = SKILL.read_text(encoding="utf-8")
        m = re.match(r"^---\n(.*?)\n---\n", text, re.S)
        self.assertIsNotNone(m, "SKILL.md 缺少 YAML frontmatter")
        self.assertIn("name: daily-papers", m.group(1))
        self.assertRegex(m.group(1), r"description:\s*\S+")

    def test_name_and_version_agree_across_clients(self):
        zcode = _load(ZCODE_MANIFEST)
        codex = _load(CODEX_MANIFEST)
        self.assertEqual(zcode["name"], "daily-papers")
        self.assertEqual(codex["name"], zcode["name"])
        self.assertEqual(codex["version"], zcode["version"])

    def test_descriptions_match(self):
        zcode = _load(ZCODE_MANIFEST)
        codex = _load(CODEX_MANIFEST)
        self.assertEqual(zcode["description"], codex["description"])

    def test_mcp_server_declaration_agrees(self):
        zcode_mcp = _load(ZCODE_MANIFEST)["mcpServers"]["daily-papers"]
        codex_pointer = _load(CODEX_MANIFEST)["mcpServers"]
        self.assertEqual(codex_pointer, "./.mcp.json", "Codex 清单必须外链 .mcp.json")
        codex_mcp = _load(CODEX_MCP)["mcpServers"]["daily-papers"]
        self.assertEqual(codex_mcp["command"], zcode_mcp["command"])
        self.assertEqual(codex_mcp.get("args", []), zcode_mcp.get("args", []))
        self.assertEqual(codex_mcp["env"], zcode_mcp["env"])
        self.assertEqual(codex_mcp["env"]["DPD_API"], "http://127.0.0.1:8080")
        # Token 绝不写进仓库
        self.assertNotIn("DPD_TOKEN", codex_mcp["env"])
        self.assertNotIn("DPD_TOKEN", zcode_mcp["env"])

    def test_codex_manifest_uses_path_form_for_skills(self):
        codex = _load(CODEX_MANIFEST)
        self.assertEqual(codex["skills"], "./skills/", "Codex 的 skills 必须是路径字符串")
        skills_dir = PLUGIN / codex["skills"].lstrip("./")
        self.assertTrue((skills_dir / "daily-papers/SKILL.md").exists())

    def test_zcode_manifest_lists_skills_dir(self):
        zcode = _load(ZCODE_MANIFEST)
        self.assertIn("skills", zcode["skills"])
        self.assertTrue((PLUGIN / "skills/daily-papers/SKILL.md").exists())

    def test_marketplaces_point_at_existing_plugin_dir(self):
        zcode_market = _load(ZCODE_MARKET)
        self.assertEqual(zcode_market["plugins"][0]["source"], ".")
        root_market = _load(ROOT_ZCODE_MARKET)
        target = REPO / root_market["plugins"][0]["source"]
        self.assertTrue((target / ".zcode-plugin/plugin.json").exists(),
                        f"ZCode 根市场指向的目录没有插件清单: {target}")

        codex_market = _load(CODEX_MARKET)
        entry = codex_market["plugins"][0]
        self.assertEqual(entry["name"], "daily-papers")
        codex_target = (REPO / entry["source"]["path"]).resolve()
        self.assertTrue((codex_target / ".codex-plugin/plugin.json").exists(),
                        f"Codex 市场指向的目录没有插件清单: {codex_target}")

    def test_manifest_tool_count_matches_server(self):
        """清单文案里的工具数必须等于 MCP 服务器实际暴露的工具数。"""
        declared = len(re.findall(r"(\d+)\s*个语义化工具",
                                  _load(ZCODE_MANIFEST)["description"]))
        self.assertEqual(declared, 1, "清单描述里应写明工具数量")
        claimed = int(re.search(r"(\d+)\s*个语义化工具",
                                _load(ZCODE_MANIFEST)["description"]).group(1))
        actual = len(re.findall(r"@(?:\w+)\.tool",
                                MCP_SERVER.read_text(encoding="utf-8")))
        self.assertEqual(claimed, actual,
                         f"清单写的是 {claimed} 个工具，mcp_server.py 实际注册 {actual} 个")

    def test_skill_documents_every_tool(self):
        """SKILL.md 的工具地图应覆盖服务器暴露的工具名（避免清单与指引脱节）。"""
        server = MCP_SERVER.read_text(encoding="utf-8")
        names = set(re.findall(r"@mcp\.tool\(\)\s*\n\s*(?:async )?def (\w+)", server))
        self.assertTrue(names, "未能从 mcp_server.py 提取到工具名")
        skill = SKILL.read_text(encoding="utf-8")
        missing = sorted(n for n in names if f"`{n}`" not in skill)
        self.assertEqual(missing, [], f"SKILL.md 未提及这些工具: {missing}")


if __name__ == "__main__":
    unittest.main()
