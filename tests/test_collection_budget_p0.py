"""Step 1 budget, tracking parallelism and preview cache (no network, temp dirs)."""
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import main
from core.db import Database
from core.fetcher import JournalFetcher

DAY = "2026-09-15"


class CollectionBudgetTests(unittest.TestCase):
    def test_budget_skips_remaining_sources(self):
        f = JournalFetcher({"fetcher": {"request_timeout": 5},
                            "performance": {"rss_concurrency": 1}, "journals": [
            {"id": 1, "name": "slow", "rss": "http://a", "source_type": "rss"},
            {"id": 2, "name": "later", "rss": "http://b", "source_type": "rss"}]})
        def slow(journal):
            time.sleep(0.4)
            return []
        with patch.object(JournalFetcher, "_fetch_journal", side_effect=slow):
            deadline = time.monotonic() + 0.15
            f.fetch_all(deadline=deadline)
        errors = [r["error"] for r in f.source_results]
        self.assertTrue(any(e and "collection budget exceeded" in e for e in errors), errors)

    def test_request_timeout_clamped_to_remaining_budget(self):
        f = JournalFetcher({"fetcher": {"request_timeout": 30}})
        self.assertEqual(f._request_timeout(), 30)
        f.fetch_all(deadline=time.monotonic() + 2)   # sets the deadline
        self.assertLessEqual(f._request_timeout(), 5)


class TrackingParallelTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config = {
            "database": {"path": str(Path(self.tmp.name) / "t.db")},
            "output": {"output_dir": str(Path(self.tmp.name) / "out")},
            "fetcher": {"date_filter_days": 3, "use_fulltext": False, "collection_budget_seconds": 30},
            "scheduler": {"timezone": "Asia/Shanghai"},
            "tracking": {"enabled": True},
            "relevance_threshold": 5,
        }

    def test_tracking_runs_alongside_collection(self):
        order = []
        def slow_tracking(config, db):
            time.sleep(0.5)
            order.append("tracking")
            return [], {}
        def slow_fetch(self, health_callback=None, deadline=None):
            self.source_results = []
            time.sleep(0.5)
            order.append("fetch")
            return []
        patches = [
            patch("main.collect_tracking_articles", side_effect=slow_tracking),
            patch.object(JournalFetcher, "fetch_all", slow_fetch),
            patch("main.LLMAnalyzer", _StubAnalyzer),
            patch("main.Notifier", _StubNotifier),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        start = time.monotonic()
        main.run_once(self.config, DAY, trial=True)
        elapsed = time.monotonic() - start
        self.assertLess(elapsed, 0.95, f"tracking must overlap collection, took {elapsed:.2f}s")
        self.assertEqual(set(order), {"tracking", "fetch"})

    def test_tracking_budget_skip_keeps_cursors(self):
        released = threading.Event()
        def stuck_tracking(config, db):
            released.wait(timeout=5)
            return [], {"seed_windows": {1: "2026-09-01"}}
        self.config["fetcher"]["collection_budget_seconds"] = 1
        self.config["fetcher"]["preview_cache_dir"] = str(Path(self.tmp.name) / "cache")
        patches = [
            patch("main.collect_tracking_articles", side_effect=stuck_tracking),
            patch.object(JournalFetcher, "fetch_all", lambda self, health_callback=None, deadline=None: []),
            patch("main.LLMAnalyzer", _StubAnalyzer),
            patch("main.Notifier", _StubNotifier),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        try:
            result = main.run_once(self.config, DAY, trial=True)
        finally:
            released.set()
        self.assertEqual(result.get("tracking_skipped"), "collection budget exceeded")


class PreviewCacheTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config = {
            "database": {"path": str(Path(self.tmp.name) / "t.db")},
            "output": {"output_dir": str(Path(self.tmp.name) / "out")},
            "fetcher": {"date_filter_days": 3,
                        "preview_cache_dir": str(Path(self.tmp.name) / "cache")},
            "scheduler": {"timezone": "Asia/Shanghai"},
            "tracking": {"enabled": False},
            "relevance_threshold": 5,
        }
        Database(self.config["database"]["path"]).close()
        self.calls = 0
        owner = self
        class Fetcher:
            def __init__(self, config):
                self.source_results = []
            def fetch_all(self, health_callback=None, deadline=None):
                owner.calls += 1
                self.source_results = [{"source_id": 1, "success": True, "complete": True,
                                        "truncated": False, "raw_count": 0, "returned_count": 0,
                                        "error": None}]
                return []
            def close(self):
                pass
        p = patch("main.JournalFetcher", Fetcher)
        p.start()
        self.addCleanup(p.stop)
        # preview_cache_dir keeps every write inside the temp directory.

    def test_preview_cache_reuse_and_refresh(self):
        first = main.run_once(self.config, DAY, preview=True)
        self.assertEqual(self.calls, 1)
        self.assertNotIn("cached", first)
        second = main.run_once(self.config, DAY, preview=True)
        self.assertEqual(self.calls, 1, "second preview must reuse today's cache")
        self.assertTrue(second.get("cached"))
        third = main.run_once(self.config, DAY, preview=True, refresh=True)
        self.assertEqual(self.calls, 2, "refresh must re-collect")
        self.assertNotIn("cached", third)


class _StubAnalyzer:
    model = "stub"
    def __init__(self, config=None):
        self.usage = {"requests": 0, "calls": 0}
    def set_feedback_examples(self, *args):
        pass
    def filter_relevance(self, article):
        return {"score": 1, "reason": "", "model": "stub", "basis": "abstract"}
    def analyze_article(self, article):
        return {"success": True, "analysis": "", "evidence_level": "ABSTRACT_ONLY", "error": ""}


class _StubNotifier:
    def __init__(self, config):
        self.output_dir = Path(config["output"]["output_dir"])
    def render_digest_version(self, payload):
        return []
    def send_digest_files(self, **kwargs):
        return None


if __name__ == "__main__":
    unittest.main()
