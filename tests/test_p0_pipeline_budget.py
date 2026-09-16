"""P0 end-to-end evidence: fixed dates, temporary databases, no network/model."""
import copy
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import main
from core.db import Database

DAY = "2026-09-13"


class P0PipelineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config = {
            "database": {"path": str(Path(self.tmp.name) / "test.db")},
            "output": {"output_dir": str(Path(self.tmp.name) / "out"),
                       "email": {"enabled": True}},
            "fetcher": {"date_filter_days": 3, "use_fulltext": False},
            "scheduler": {"timezone": "Asia/Shanghai"},
            "tracking": {"enabled": False},
            "relevance_threshold": 5,
        }
        self.articles = []
        self.sources = []
        self.seen = []
        owner = self

        class Fetcher:
            def __init__(self, config):
                self.source_results = copy.deepcopy(owner.sources)
            def fetch_all(self, health_callback=None, deadline=None):
                return copy.deepcopy(owner.articles)
            def close(self):
                pass

        class Analyzer:
            model = "fake"
            def __init__(self, config):
                self.usage = {"requests": 0, "calls": 0}
            def set_feedback_examples(self, *args):
                pass
            def filter_relevance(self, article):
                owner.seen.append(article["id"])
                self.usage["requests"] += 1
                return {"score": 1, "reason": "irrelevant", "model": "fake", "basis": "abstract"}
            def analyze_article(self, article):
                raise AssertionError("low scores must not trigger analysis")

        for target, value in (("main.JournalFetcher", Fetcher), ("main.LLMAnalyzer", Analyzer)):
            p = patch(target, value)
            p.start()
            self.addCleanup(p.stop)
        for target in ("fetchers.oa_fetcher.get_dedup_abstract", "utils.translate.translate_title"):
            p = patch(target, return_value="")
            p.start()
            self.addCleanup(p.stop)
        p = patch("core.notifier.Notifier.send_digest_files", side_effect=AssertionError("no sending"))
        p.start()
        self.addCleanup(p.stop)

    def article(self, n, pub_date=DAY):
        return {"doi": f"10.p0/{n}", "title": f"paper {n}", "journal": f"journal {n % 10}",
                "pub_date": pub_date, "abstract": "A complete original abstract. " * 20}

    def test_2000_two_rounds_and_expired_admitted_backlog(self):
        self.config["output"]["email"]["enabled"] = False
        self.articles = [self.article(i) for i in range(2000)]
        first = main.run_once(self.config, DAY)
        first_ids = set(self.seen)
        self.assertEqual((first["score_queue_total"], first["score_attempted"], first["score_deferred"]), (2000, 100, 1900))
        self.articles = []
        second = main.run_once(self.config, "2026-09-20")
        self.assertEqual((second["score_queue_total"], second["score_attempted"], second["score_deferred"]), (1900, 100, 1800))
        self.assertEqual(len(set(self.seen)), 200)
        self.assertFalse(first_ids.intersection(self.seen[100:]))
        with sqlite3.connect(self.config["database"]["path"]) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM articles WHERE score_status='ok'").fetchone()[0], 200)
            self.assertEqual(conn.execute("SELECT count(*) FROM articles WHERE COALESCE(score_status,'')='' ").fetchone()[0], 1800)

    def test_trial_quarantine_and_no_digest(self):
        self.articles = [self.article(i) for i in range(100)] + [
            self.article(101, ""), self.article(102, "2026-09-10"),
            self.article(103, "2026-09-14"), self.article(104, "2026-09")]
        result = main.run_once(self.config, DAY, trial=True)
        self.assertEqual(result["score_attempted"], 30)
        self.assertEqual(result["new_quarantined"], 4)
        self.assertEqual(result["score_deferred"], 70)
        with sqlite3.connect(self.config["database"]["path"]) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM articles").fetchone()[0], 104)
            self.assertEqual(conn.execute("SELECT count(*) FROM digest_versions").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT count(*) FROM articles WHERE processing_status!='eligible' AND score_status='ok'").fetchone()[0], 0)

    def test_preview_has_no_database_model_or_notifier_writes(self):
        db = Database(self.config["database"]["path"])
        db.close()
        path = Path(self.config["database"]["path"])
        before = path.read_bytes()
        self.articles = [self.article(i) for i in range(150)]
        with patch("main.Database", side_effect=AssertionError("must be read-only")), \
             patch("main.LLMAnalyzer", side_effect=AssertionError("no model")), \
             patch("main.Notifier", side_effect=AssertionError("no notifier")):
            result = main.run_once(self.config, DAY, preview=True)
        self.assertEqual(result["score_planned"], 100)
        self.assertEqual(result["score_attempted"], 0)
        self.assertEqual(path.read_bytes(), before)

    def test_source_incomplete_and_save_failure_do_not_advance(self):
        db = Database(self.config["database"]["path"])
        with db.get_connection() as conn:
            conn.execute("INSERT INTO journals(id,name,rss,enabled,last_run) VALUES(999,'source','https://example.test',1,'2026-09-10')")
            conn.commit()
        db.close()
        self.sources = [{"source_id": 999, "success": True, "complete": False, "truncated": True,
                         "window_start": "2026-09-11", "window_end": DAY, "next_cursor": "page2", "raw_count": 1}]
        self.articles = [self.article(1)]
        main.run_once(self.config, DAY, trial=True)
        with sqlite3.connect(self.config["database"]["path"]) as conn:
            self.assertEqual(conn.execute("SELECT last_run FROM journals WHERE id=999").fetchone()[0], "2026-09-10")
        self.sources[0].update(complete=True, truncated=False)
        self.articles = [{**self.article(2), "title": "Distinct solvation energy landscape"}]
        with patch.object(Database, "save_articles_batch", return_value=[None]):
            result = main.run_once(self.config, DAY, trial=True)
        self.assertGreater(result["db_errors"], 0)
        with sqlite3.connect(self.config["database"]["path"]) as conn:
            self.assertEqual(conn.execute("SELECT last_run FROM journals WHERE id=999").fetchone()[0], "2026-09-10")


if __name__ == "__main__":
    unittest.main()
