import unittest
import sqlite3
from core.db import Database, _compute_title_hash
from core.notifier import classify_article
from core.analyzer import LLMAnalyzer, LLMResponseParseError


class TestDailyPaperDigestCore(unittest.TestCase):
    def test_title_hash(self):
        h1 = _compute_title_hash("Deep Learning for Molecules")
        h2 = _compute_title_hash("deep   learning  for molecules ")
        self.assertEqual(h1, h2)
        self.assertIsNone(_compute_title_hash(""))

    def test_database_dedup_and_batch_save(self):
        db = Database(":memory:")

        article1 = {
            "doi": "10.1021/acs.jcim.12345",
            "title": "Machine Learning Potentials for Water",
            "journal": "J. Chem. Inf. Model.",
            "authors": ["Author A", "Author B"],
            "pub_date": "2026-09-01",
            "url": "https://pubs.acs.org/doi/10.1021/acs.jcim.12345",
            "abstract": "We develop a machine learning potential...",
            "relevance": 9.0,
            "topic": "机器学习势函数 / MLIP",
        }

        # 测试批量保存
        saved = db.save_articles_batch([article1])
        self.assertEqual(saved, 1)

        # 检查 topic 是否保存成功
        conn = db.get_connection()
        row = conn.execute("SELECT topic FROM articles WHERE doi = ?", (article1["doi"],)).fetchone()
        self.assertEqual(row[0], "机器学习势函数 / MLIP")

        # 测试 DOI 精确查重
        is_dup, reason = db.check_duplicate({"doi": "10.1021/acs.jcim.12345", "title": "Other Title"})
        self.assertTrue(is_dup)
        self.assertIn("DOI重复", reason)

        # 测试 URL 查重
        is_dup, reason = db.check_duplicate({"url": "https://pubs.acs.org/doi/10.1021/acs.jcim.12345"})
        self.assertTrue(is_dup)
        self.assertIn("URL重复", reason)

        # 测试标题相似度快速修剪
        is_dup, reason = db.check_duplicate({"title": "Machine Learning Potential for Waters"})
        self.assertTrue(is_dup)
        self.assertTrue("相似" in reason or "精确" in reason)

    def test_classify_article(self):
        art_ml = {"title": "Active Learning for Machine Learning Potential", "abstract": "Training with deepmd"}
        self.assertEqual(classify_article(art_ml), "机器学习势函数 / MLIP")

        art_dft = {"title": "First-principles study of catalysts", "abstract": "Calculated by VASP using DFT"}
        self.assertIn(classify_article(art_dft), ("DFT / 第一性原理", "催化 / 反应机理"))

        art_other = {"title": "Economic review of materials", "abstract": "Market value of lithium"}
        self.assertEqual(classify_article(art_other), "其他")

    def test_analyzer_json_parsing(self):
        dummy_config = {
            "llm": {
                "provider": "deepseek",
                "deepseek": {"api_key": "fake", "base_url": "https://fake.api.com"},
            }
        }
        analyzer = LLMAnalyzer(dummy_config)

        # 1. 正常 json
        res1 = analyzer._parse_json('{"score": 8, "reason": "Good match"}')
        self.assertEqual(res1["score"], 8)

        # 2. Markdown 代码块包裹
        res2 = analyzer._parse_json('```json\n{"score": 9, "reason": "Awesome"}\n```')
        self.assertEqual(res2["score"], 9)

        # 3. 混杂自然语言的前后文
        res3 = analyzer._parse_json('Here is my analysis: {"score": 5, "reason": "Moderate"} Thank you.')
        self.assertEqual(res3["score"], 5)

        # 4. 异常解析抛出 LLMResponseParseError
        with self.assertRaises(LLMResponseParseError):
            analyzer._parse_json("Not a valid json at all")


if __name__ == "__main__":
    unittest.main()
