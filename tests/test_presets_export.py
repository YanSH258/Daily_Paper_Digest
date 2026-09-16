"""来源预设导出的回归测试：临时数据库，不触发采集/评分/推送，不联网。

覆盖：只导出启用来源、导出→导入往返一致、旧文件说明文字保留、
新条目核验字段不虚构、导出内容不含任何密钥/邮箱、无法表达的记录被报告。
"""
import json
import re
import tempfile
import unittest
from pathlib import Path

from core.db import Database
from utils import presets

SECRET_KEYS = {"api_key", "apikey", "password", "token", "secret", "webhook", "webhook_url"}
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


def _walk_keys(node, found: set[str]) -> None:
    if isinstance(node, dict):
        for k, v in node.items():
            found.add(str(k).lower())
            _walk_keys(v, found)
    elif isinstance(node, list):
        for v in node:
            _walk_keys(v, found)


class PresetExportTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.db = Database(str(self.tmp / "test.db"))
        self.addCleanup(self.db.close)
        # 与 import_preset 一致：非 rss 来源的 rss 列存规范化抓取地址（同时是去重键）
        self.db.add_journal("J. Comput. Chem.", "https://example.org/jcc.xml",
                            publisher="Wiley", max_articles=10)
        self._add_source("arXiv cond-mat", "arxiv", "cat:cond-mat.mtrl-sci")
        self._add_source("JACS", "openalex", "filter:locations.source.issn:0002-7863",
                         publisher="OpenAlex", max_articles=30)
        self._add_source("J. Chem. Phys. (JCP)", "crossref", "0021-9606", publisher="AIP")
        self.db.add_journal("停用来源", "https://example.org/legacy.xml", enabled=False)

    def _add_source(self, name, source_type, query, publisher="DEFAULT", max_articles=100):
        entry = {"name": name, "source_type": source_type, "query": query}
        return self.db.add_journal(name, presets.canonical_url(entry), publisher=publisher,
                                   max_articles=max_articles, source_type=source_type,
                                   query=query)

    # ── 基本行为 ────────────────────────────────────────────────
    def test_only_enabled_sources_exported(self):
        preset, skipped = presets.build_preset(self.db, exported_at="2026-09-16")
        names = [s["name"] for s in preset["sources"]]
        self.assertNotIn("停用来源", names)
        self.assertEqual(len(preset["sources"]), 4)
        self.assertEqual(skipped, [])
        self.assertIn("1 条", " ".join(preset["notes"]))  # 说明里交代未导出的停用条数

    def test_include_disabled_flag(self):
        preset, _ = presets.build_preset(self.db, include_disabled=True, exported_at="2026-09-16")
        self.assertEqual(len(preset["sources"]), 5)

    def test_every_entry_has_required_fields(self):
        preset, _ = presets.build_preset(self.db, exported_at="2026-09-16")
        for src in preset["sources"]:
            self.assertTrue(src["name"])
            self.assertIn(src["source_type"], presets.VALID_SOURCE_TYPES)
            self.assertTrue(src["coverage"])
            self.assertTrue(src["limitations"])
            self.assertTrue(src["verified"])
            self.assertIsNotNone(presets.canonical_url(src), src["name"])
            self.assertTrue(src.get("url") or src.get("query"), src["name"])

    def test_crossref_roundtrip(self):
        preset, _ = presets.build_preset(self.db, exported_at="2026-09-16")
        cr = [s for s in preset["sources"] if s["source_type"] == "crossref"]
        self.assertEqual(len(cr), 1)
        self.assertEqual(cr[0]["query"], "0021-9606")
        self.assertEqual(presets.canonical_url(cr[0]),
                         "https://api.crossref.org/journals/0021-9606/works")

    def test_query_recovered_from_url_when_missing(self):
        """历史记录可能只写地址没写检索式，导出仍需还原出可导入的 query。"""
        self.db.add_journal("旧格式 OpenAlex", "https://api.openalex.org/works?filter:locations.source.issn:1234-5678",
                            publisher="OpenAlex", source_type="openalex", query="")
        preset, _ = presets.build_preset(self.db, exported_at="2026-09-16")
        row = [s for s in preset["sources"] if s["name"] == "旧格式 OpenAlex"]
        self.assertEqual(row[0]["query"], "filter:locations.source.issn:1234-5678")

    # ── 往返一致 ────────────────────────────────────────────────
    def test_export_then_import_recreates_same_sources(self):
        target = self.tmp / "preset.json"
        result = presets.export_preset(self.db, target, exported_at="2026-09-16")
        self.assertEqual(result["count"], 4)

        entries = presets.preview_preset(target)
        self.assertTrue(all(e["ok"] for e in entries), [e for e in entries if not e["ok"]])
        self.assertEqual(len(entries), 4)

        fresh = Database(str(self.tmp / "fresh.db"))
        self.addCleanup(fresh.close)
        imported = presets.import_preset(target, fresh)
        self.assertEqual(len(imported["added"]), 4)
        self.assertEqual(imported["skipped"], [])

        before = {presets.canonical_url(s) for s in presets.build_preset(self.db, exported_at="x")[0]["sources"]}
        after = {j["rss"] for j in fresh.list_journals()}
        self.assertEqual(before, after)

    def test_export_is_idempotent(self):
        target = self.tmp / "preset.json"
        presets.export_preset(self.db, target, exported_at="2026-09-16")
        first = target.read_text(encoding="utf-8")
        presets.export_preset(self.db, target, exported_at="2026-09-16")
        self.assertEqual(first, target.read_text(encoding="utf-8"))

    # ── 说明文字：保留旧的，不虚构新的 ───────────────────────────
    def test_existing_text_preserved_by_canonical_url(self):
        old = {
            "name": "computational_materials",
            "sources": [{
                "name": "J. Comput. Chem.",
                "source_type": "rss",
                "url": "https://example.org/jcc.xml",
                "coverage": "人工写过的覆盖说明",
                "limitations": "人工写过的限制说明",
                "verified": "2026-09-13 批量核验接口可达",
            }],
        }
        preset, _ = presets.build_preset(self.db, previous=old, exported_at="2026-09-16")
        row = [s for s in preset["sources"] if s["name"] == "J. Comput. Chem."][0]
        self.assertEqual(row["coverage"], "人工写过的覆盖说明")
        self.assertEqual(row["limitations"], "人工写过的限制说明")
        self.assertEqual(row["verified"], "2026-09-13 批量核验接口可达")
        self.assertEqual(preset["name"], "computational_materials")

    def test_new_entry_does_not_invent_verification(self):
        preset, _ = presets.build_preset(self.db, exported_at="2026-09-16")
        row = [s for s in preset["sources"] if s["name"] == "JACS"][0]
        self.assertIn("未经单独核验", row["verified"])
        self.assertIn("2026-09-16", row["verified"])
        # 类型说明来自接口事实，不写成"已验证可用"
        self.assertIn("额度", row["limitations"])
        self.assertNotIn("核验通过", row["coverage"] + row["limitations"])

    def test_arxiv_note_describes_rss_first(self):
        preset, _ = presets.build_preset(self.db, exported_at="2026-09-16")
        row = [s for s in preset["sources"] if s["source_type"] == "arxiv"][0]
        self.assertIn("rss.arxiv.org", row["coverage"])
        self.assertIn("3 秒", row["limitations"])

    # ── 安全：导出文件不得带密钥或邮箱 ───────────────────────────
    def test_export_contains_no_secret_keys_or_emails(self):
        target = self.tmp / "preset.json"
        presets.export_preset(self.db, target, exported_at="2026-09-16")
        text = target.read_text(encoding="utf-8")
        raw = json.loads(text)
        keys: set[str] = set()
        _walk_keys(raw, keys)
        self.assertEqual(keys & SECRET_KEYS, set())
        self.assertIsNone(EMAIL_RE.search(text), "导出文件不应含邮箱")
        self.assertEqual(raw["version"], "2.0")
        self.assertEqual(raw["exported_at"], "2026-09-16")

    # ── 无法表达的记录逐条报告，不静默丢弃 ───────────────────────
    def test_unrepresentable_row_is_reported(self):
        self.db.add_journal("坏地址来源", "not-a-url", source_type="rss")
        preset, skipped = presets.build_preset(self.db, exported_at="2026-09-16")
        self.assertEqual([s["name"] for s in skipped], ["坏地址来源"])
        self.assertIn("没有可导入的公开地址", skipped[0]["reason"])
        self.assertEqual(len(preset["sources"]), 4)

    def test_export_does_not_touch_journals(self):
        before = self.db.list_journals()
        presets.export_preset(self.db, self.tmp / "preset.json", exported_at="2026-09-16")
        self.assertEqual([j["id"] for j in before], [j["id"] for j in self.db.list_journals()])


class RepoPresetStillValidTests(unittest.TestCase):
    """仓库内置预设文件必须始终可被导出格式往返（防格式漂移）。"""

    def test_exported_shape_matches_repo_file_contract(self):
        repo_file = Path(__file__).resolve().parents[1] / "config/source_presets.json"
        raw = json.loads(repo_file.read_text(encoding="utf-8"))
        unknown = [s.get("source_type") for s in raw["sources"]
                   if s.get("source_type") not in presets.VALID_SOURCE_TYPES]
        self.assertEqual(unknown, [], f"预设文件含不支持的来源类型: {set(unknown)}")


if __name__ == "__main__":
    unittest.main()
