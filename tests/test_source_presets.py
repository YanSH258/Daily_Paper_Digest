"""来源预设导入回归：临时数据库，不触发采集/评分/推送。

覆盖：全量导入、重复导入不重复、部分失败逐条报告、停用来源不进抓取、
仓库内置预设文件保持可解析。
"""
import json
import tempfile
import unittest
from pathlib import Path

from core.db import Database
from utils import presets
from main import load_journals_config

REPO = Path(__file__).resolve().parents[1]
PRESET = REPO / "config/source_presets.json"


class SourcePresetTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db = Database(str(Path(self._tmp.name) / "test.db"))
        self.addCleanup(self.db.close)

    def test_repo_preset_is_valid_and_verifiable(self):
        raw = json.loads(PRESET.read_text(encoding="utf-8"))
        self.assertTrue(raw["sources"])
        for src in raw["sources"]:
            self.assertTrue(src.get("name"))
            self.assertIn(src.get("source_type"), presets.VALID_SOURCE_TYPES)
            self.assertIsNotNone(presets.canonical_url(src))
            self.assertTrue(src.get("verified"), "每条来源必须带核验记录")

    def test_import_then_repeat_no_duplicates(self):
        first = presets.import_preset(PRESET, self.db)
        self.assertEqual(len(first["added"]), len(json.loads(PRESET.read_text())["sources"]))
        self.assertEqual(first["skipped"], [])

        second = presets.import_preset(PRESET, self.db)
        self.assertEqual(second["added"], [])
        self.assertEqual(len(second["skipped"]), len(first["added"]))
        self.assertTrue(all(s["reason"] == "已存在" for s in second["skipped"]))

        rows = self.db.list_journals()
        self.assertEqual(len(rows), len(first["added"]))

    def test_arxiv_openalex_store_query_and_canonical_url(self):
        presets.import_preset(PRESET, self.db)
        by_type = {j["name"]: j for j in self.db.list_journals()}
        arxiv = [j for j in by_type.values() if j["source_type"] == "arxiv"]
        openalex = [j for j in by_type.values() if j["source_type"] == "openalex"]
        self.assertTrue(arxiv and openalex)
        for j in arxiv + openalex:
            self.assertTrue(j["query"], j["name"])
            self.assertTrue(j["rss"].startswith("https://"), j["rss"])
        # arXiv 检索式进入规范 URL
        mlip = [j for j in arxiv if "machine learning potential" in j["query"]]
        self.assertTrue(mlip)
        self.assertIn("export.arxiv.org/api/query", mlip[0]["rss"])

    def test_partial_failure_reported_per_item(self):
        bad = Path(self._tmp.name) / "bad_preset.json"
        bad.write_text(json.dumps({
            "name": "broken", "sources": [
                {"name": "正常来源", "source_type": "rss", "url": "https://example.org/feed.rss"},
                {"name": "缺检索式的arXiv", "source_type": "arxiv", "query": ""},
                {"name": "未知类型", "source_type": "webpage", "url": "https://example.org"},
                {"source_type": "rss", "url": "https://example.org/named.rss"},
            ]
        }, ensure_ascii=False), encoding="utf-8")
        result = presets.import_preset(bad, self.db)
        self.assertEqual(len(result["added"]), 1)
        reasons = {s["name"]: s["reason"] for s in result["skipped"]}
        self.assertEqual(reasons["缺检索式的arXiv"], "缺少有效地址或检索式")
        self.assertIn("不支持的来源类型", reasons["未知类型"])
        self.assertIn("缺少来源名称", reasons["(第 4 条)"])

    def test_null_entry_does_not_abort_batch(self):
        """坏条目（null/非对象/字段类型错误）逐条报告，后续合法条目继续导入。"""
        mixed = Path(self._tmp.name) / "mixed_preset.json"
        mixed.write_text(json.dumps({
            "name": "mixed", "sources": [
                {"name": "来源A", "source_type": "rss", "url": "https://example.org/a.rss"},
                None,
                {"name": "来源B", "source_type": "rss", "url": "https://example.org/b.rss"},
                {"name": 123, "source_type": "rss", "url": "https://example.org/c.rss"},
                {"name": "URL类型错误", "source_type": "rss", "url": {"bad": "value"}},
            ]
        }, ensure_ascii=False), encoding="utf-8")

        # 预览同样逐条容错，不抛异常
        items = presets.preview_preset(mixed)
        self.assertEqual(len(items), 5)
        self.assertEqual([it["ok"] for it in items],
                         [True, False, True, False, False])

        result = presets.import_preset(mixed, self.db)
        self.assertEqual([a["name"] for a in result["added"]], ["来源A", "来源B"])
        reasons = {s["name"]: s["reason"] for s in result["skipped"]}
        self.assertIn("不是对象", reasons["(第 2 条)"])
        self.assertIn("缺少来源名称", reasons["(第 4 条)"])
        self.assertEqual(reasons["URL类型错误"], "缺少有效地址或检索式")
        # 两条合法来源都已入库，坏条目没有部分写入
        self.assertEqual(len(self.db.list_journals()), 2)

    def test_disabled_source_excluded_from_fetch(self):
        presets.import_preset(PRESET, self.db)
        first = self.db.list_journals()[0]
        self.db.set_journal_enabled(first["id"], False)
        journals = load_journals_config({}, self.db)
        self.assertNotIn(first["id"], [j["id"] for j in journals])
        self.assertGreater(len(journals), 0)
        # 非禁用来源仍带 source_type/query 供 fetcher 使用
        self.assertIn("source_type", journals[0])


    def test_cli_dry_run_shows_validation_reasons(self):
        """CLI 预览输出必须标明每条状态；坏条目显示具体原因而非 None。"""
        import os
        import subprocess
        import sys
        mixed = Path(self._tmp.name) / "cli_preset.json"
        mixed.write_text(json.dumps({
            "name": "cli", "sources": [
                {"name": "正常来源", "source_type": "rss", "url": "https://example.org/feed.rss",
                 "verified": "2026-09-12 HTTP 200"},
                {"name": "坏来源", "source_type": "arxiv", "query": ""},
            ]
        }, ensure_ascii=False), encoding="utf-8")
        repo = Path(__file__).resolve().parents[1]
        env = dict(os.environ)
        py_paths = [str(repo / "src")]
        deps = repo / ".deps"
        if deps.is_dir():
            py_paths.append(str(deps))
        env["PYTHONPATH"] = os.pathsep.join(py_paths)
        env["DPD_DATA_ROOT"] = self._tmp.name  # 日志/数据不落到真实目录
        result = subprocess.run(
            [sys.executable, "-m", "main", "--import-sources", str(mixed), "--dry-run"],
            capture_output=True, text=True, env=env,
            cwd=self._tmp.name, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        out = result.stdout
        self.assertIn("1 条无效", out)
        self.assertIn("正常来源 [可导入]", out)
        self.assertIn("坏来源 [无效] 缺少有效地址或检索式", out)
        self.assertNotIn("None", out)


if __name__ == "__main__":
    unittest.main()
