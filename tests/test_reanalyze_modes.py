import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from core.db import Database
import web_server


class ReanalyzeModeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Database(str(Path(self.tmp.name) / "reanalyze.db"))
        self.addCleanup(self.db.close)
        self.article_id = self.db.save_articles_batch([{
            "title": "Paper", "journal": "Journal", "abstract": "Original abstract.",
            "pub_date": "2026-09-20", "processing_status": "eligible",
        }])[0]
        self.db.update_article_fields(
            self.article_id, evidence_level="FULLTEXT", fulltext_text="Stored full text",
        )
        db_path = str(Path(self.tmp.name) / "reanalyze.db")
        def connect_db():
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            return conn
        self.ctx = SimpleNamespace(
            db=self.db,
            connect_db=connect_db,
            config={"llm": {"provider": "fake", "fake": {
                "api_key": "k", "base_url": "https://example.test", "model": "m"}}},
        )

    def test_explicit_abstract_mode_ignores_stored_fulltext(self):
        seen = []
        class Analyzer:
            model = "m"
            def __init__(self, config):
                pass
            def analyze_article(self, article):
                seen.append(article)
                return {"success": True, "analysis": "一段完整译文。" * 20,
                        "evidence_level": "ABSTRACT_ONLY", "error": ""}
        with patch.object(web_server, "LLMAnalyzer", Analyzer):
            payload, code = web_server._reanalyze_article_unlocked(
                self.ctx, self.article_id, {"mode": "abstract_translation"})
        self.assertEqual(code, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(seen[0]["evidence_level"], "ABSTRACT_ONLY")
        row = self.db.get_articles_by_ids([self.article_id])[0]
        self.assertEqual(row["analysis_evidence_level"], "ABSTRACT_ONLY")
        self.assertEqual(row["evidence_level"], "FULLTEXT")
        self.assertEqual(row["analysis_prompt_version"], "v4")

    def test_explicit_fulltext_mode_uses_fetcher_and_closes_it(self):
        from fetchers.models import BestFormat, FetchResult, FetchStatus
        closed = []
        seen = []
        class Analyzer:
            model = "m"
            def __init__(self, config):
                pass
            def analyze_article(self, article):
                seen.append(article)
                return {"success": True, "analysis": "Deep analysis",
                        "evidence_level": "FULLTEXT", "error": ""}
        class Fetcher:
            def __init__(self, config):
                pass
            def fetch_fulltext_with_status(self, article):
                return FetchResult(text="New full text " * 100,
                                   best_available_format=BestFormat.HTML_FULLTEXT,
                                   fetch_status=FetchStatus.SUCCESS)
            def close(self):
                closed.append(True)
        with patch.object(web_server, "LLMAnalyzer", Analyzer), \
             patch("core.fetcher.JournalFetcher", Fetcher):
            payload, code = web_server._reanalyze_article_unlocked(
                self.ctx, self.article_id,
                {"mode": "fulltext_analysis", "fetch_fulltext": True})
        self.assertEqual(code, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(seen[0]["evidence_level"], "FULLTEXT")
        self.assertEqual(closed, [True])
        row = self.db.get_articles_by_ids([self.article_id])[0]
        self.assertEqual(row["analysis_evidence_level"], "FULLTEXT")

    def test_invalid_mode_is_rejected(self):
        payload, code = web_server._reanalyze_article_unlocked(
            self.ctx, self.article_id, {"mode": "unknown"})
        self.assertEqual(code, 400)
        self.assertFalse(payload["ok"])


if __name__ == "__main__":
    unittest.main()
