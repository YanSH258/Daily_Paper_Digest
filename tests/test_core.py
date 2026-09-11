import unittest
import sqlite3
import tempfile
import unittest.mock
from pathlib import Path
from types import SimpleNamespace

from core.db import Database, _compute_title_hash
from core.notifier import classify_article
from core.analyzer import LLMAnalyzer, LLMError, LLMResponseParseError
from utils.paths import PROJECT_ROOT, resolve_against_root


class TestResolveAgainstRoot(unittest.TestCase):
    """相对输出路径统一锚定项目根。

    回归背景：notifier/reanalyze 用 parent.parent 把相对路径锚到了 src/
    （少锚一级），日报被写到 src/data/output，而网页预览读 data/output，
    导致日报预览 404。
    """

    def test_relative_path_anchored_to_project_root(self):
        resolved = resolve_against_root("data/output")
        self.assertEqual(resolved, PROJECT_ROOT / "data/output")
        # 关键回归断言：解析结果不得落在 src/ 下
        self.assertNotIn("src", resolved.parts)

    def test_absolute_path_unchanged(self):
        tmp = Path(tempfile.mkdtemp())
        self.assertEqual(resolve_against_root(tmp / "out"), tmp / "out")

    def test_notifier_uses_shared_resolver(self):
        # Notifier 对相对 output_dir 的解析与共享锚定一致
        from core.notifier import Notifier
        n = Notifier({"output": {"output_dir": "data/output"}})
        self.assertEqual(n.output_dir, PROJECT_ROOT / "data/output")


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

        # 测试批量保存（返回与输入对齐的 id 列表）
        saved_ids = db.save_articles_batch([article1])
        self.assertEqual(len(saved_ids), 1)
        self.assertIsInstance(saved_ids[0], int)
        article_id = saved_ids[0]

        # 检查 topic 是否保存成功
        conn = db.get_connection()
        row = conn.execute("SELECT topic FROM articles WHERE doi = ?", (article1["doi"],)).fetchone()
        self.assertEqual(row[0], "机器学习势函数 / MLIP")

        # 新文章初始为未完成状态（processed=0），阶段状态默认空
        row = conn.execute("SELECT processed, score_status FROM articles WHERE doi = ?",
                           (article1["doi"],)).fetchone()
        self.assertEqual(row[0], 0)
        self.assertEqual(row[1], "")

        # 阶段状态更新（白名单字段）
        self.assertTrue(db.update_article_fields(
            article_id, score_status="ok", relevance=9.0,
            relevance_reason="命中", processed=1))
        row = conn.execute("SELECT score_status, relevance_reason, updated_at FROM articles WHERE id = ?",
                           (article_id,)).fetchone()
        self.assertEqual(row[0], "ok")
        self.assertEqual(row[1], "命中")
        self.assertIsNotNone(row[2])

        # 白名单之外的列不允许通过该接口修改
        self.assertFalse(db.update_article_fields(article_id, doi="hack"))

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


class TestCallLLMErrorHandling(unittest.TestCase):
    """_call_llm 对确定性 4xx 错误（如模型名写错）不重试、立即失败。

    回归背景：2026-09-11 模型名写错导致 59 篇评分对 HTTP 400 反复重试
    3 次，空耗约 5.5 分钟。
    """

    def _make_analyzer_with_status_error(self, status_code):
        dummy_config = {
            "llm": {
                "provider": "deepseek",
                "deepseek": {"api_key": "fake", "base_url": "https://fake.api.com", "model": "bad-model"},
            }
        }
        analyzer = LLMAnalyzer(dummy_config)

        import httpx2
        import openai

        def raise_status_error(*args, **kwargs):
            req = httpx2.Request("POST", "https://fake.api.com/chat/completions")
            resp = httpx2.Response(
                status_code, request=req,
                json={"error": {"message": f"The supported API model names are deepseek-flash, "
                                           f"but you passed bad-model.", "type": "invalid_request_error"}},
            )
            raise openai.APIStatusError(f"Error code: {status_code}", response=resp, body=None)

        analyzer.client = SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=raise_status_error))
        )
        return analyzer

    def test_400_no_retry(self):
        # 400（无效模型名）：立即失败，不 sleep、不重复请求
        analyzer = self._make_analyzer_with_status_error(400)
        with unittest.mock.patch("core.analyzer.time.sleep") as mock_sleep:
            with self.assertRaises(LLMError) as cm:
                analyzer._call_llm("hello")
            mock_sleep.assert_not_called()
        self.assertIn("客户端错误", str(cm.exception))
        self.assertIn("HTTP 400", str(cm.exception))

    def test_500_still_retries(self):
        # 5xx（服务端错误）：保留重试
        analyzer = self._make_analyzer_with_status_error(500)
        with unittest.mock.patch("core.analyzer.time.sleep") as mock_sleep:
            with self.assertRaises(LLMError):
                analyzer._call_llm("hello")
            self.assertTrue(mock_sleep.called)

    def test_unicode_encode_error_no_retry(self):
        # 占位符 API Key（中文）导致请求头编码失败：确定性错误，立即失败不重试
        dummy_config = {
            "llm": {
                "provider": "deepseek",
                "deepseek": {"api_key": "fake", "base_url": "https://fake.api.com", "model": "m1"},
            }
        }
        analyzer = LLMAnalyzer(dummy_config)

        def raise_unicode(*args, **kwargs):
            raise UnicodeEncodeError("ascii", "Bearer 填入你的_QWEN_API_KEY", 7, 11,
                                     "ordinal not in range(128)")

        analyzer.client = SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=raise_unicode))
        )
        with unittest.mock.patch("core.analyzer.time.sleep") as mock_sleep:
            with self.assertRaises(LLMError) as cm:
                analyzer._call_llm("hello")
            mock_sleep.assert_not_called()
        self.assertIn("占位符", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
