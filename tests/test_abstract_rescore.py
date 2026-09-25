import copy
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import main
from core.analyzer import LLMRuntimeDependencyError
from core.db import Database
from processing import ABSTRACT_RESCORE_ERROR, is_score_retry

DAY = "2026-09-16"
ABSTRACT = ("An original abstract about first-principles simulations of catalytic surfaces. " * 12).strip()


class AbstractRescoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.config = {
            "database": {"path": str(root / "test.db")},
            "output": {"output_dir": str(root / "output"), "email": {"enabled": False}},
            "fetcher": {"date_filter_days": 3, "use_fulltext": False,
                        "preview_cache_dir": str(root / "cache")},
            "scheduler": {"timezone": "Asia/Shanghai"},
            "tracking": {"enabled": False}, "analyzer": {"analyze_abstract_only": False},
            "relevance_threshold": 6, "performance": {"llm_concurrency": 1},
        }
        self.db = Database(self.config["database"]["path"])
        self.addCleanup(self.db.close)
        self.discoveries, self.sources, self.scored = [], [], []
        self.analysis_calls = []
        self.score, self.score_failure, self.analysis_failure = 7, False, False
        self.score_exception = None
        owner = self

        class Fetcher:
            def __init__(self, config):
                self.source_results = copy.deepcopy(owner.sources)
            def fetch_all(self, **kwargs):
                return copy.deepcopy(owner.discoveries)
            def close(self):
                pass

        class Analyzer:
            model = "test-model"
            def __init__(self, config):
                self.usage = {"calls": 0, "requests": 0}
            def set_feedback_examples(self, *args):
                pass
            def filter_relevance(self, article):
                owner.scored.append(article["id"])
                self.usage["requests"] += 1
                if owner.score_exception:
                    raise owner.score_exception
                if owner.score_failure:
                    raise RuntimeError("temporary scoring failure")
                return {"score": owner.score, "reason": "New abstract evidence", "model": self.model,
                        "basis": "abstract", "prompt_version": "test-score-v2"}
            def analyze_article(self, article):
                owner.analysis_calls.append(article["id"])
                return {"success": not owner.analysis_failure, "analysis": "New analysis",
                        "error": "temporary analysis failure" if owner.analysis_failure else "",
                        "evidence_level": "ABSTRACT"}

        for name, value in (("main.JournalFetcher", Fetcher), ("main.LLMAnalyzer", Analyzer)):
            mock = patch(name, value)
            mock.start()
            self.addCleanup(mock.stop)
        for name in ("fetchers.oa_fetcher.get_dedup_abstract", "utils.translate.translate_title"):
            mock = patch(name, return_value="")
            mock.start()
            self.addCleanup(mock.stop)
        mock = patch.dict(os.environ, {"WOS_API_KEY": ""})
        mock.start()
        self.addCleanup(mock.stop)

    def seed(self, n, **overrides):
        article = {"doi": f"10.1234/sample-{n}", "title": f"Original paper {n}", "journal": f"Journal {n % 3}",
                   "url": f"https://example.org/papers/{n}", "pub_date": DAY,
                   "processing_status": "eligible", "abstract": overrides.pop("abstract", "")}
        for name in ("doi", "title", "journal", "url", "pub_date", "processing_status"):
            if name in overrides:
                article[name] = overrides.pop(name)
        aid = self.db.save_articles_batch([article])[0]
        self.assertIsNotNone(aid)
        fields = {"relevance": 2, "score_status": "ok", "score_model": "old-model", "score_basis": "title",
                  "score_prompt_version": "old-version", "relevance_reason": "Old title evidence", "processed": 1}
        fields.update(overrides)
        self.db.update_article_fields(aid, **fields)
        return aid

    def row(self, aid):
        return self.db.get_articles_by_ids([aid])[0]

    def run_pipeline(self):
        return main.run_once(self.config, DAY, trial=True)

    def test_later_persisted_abstract_rescores_once_and_records_version(self):
        aid = self.seed(1)
        self.assertEqual(self.db.get_processing_queue(self.config, DAY)["selected"], [])
        self.db.update_article_fields(aid, abstract=ABSTRACT)
        result = self.run_pipeline()
        self.assertEqual((result["score_attempted"], result["rescored_ok"]), (1, 1))
        row = self.row(aid)
        self.assertEqual((row["relevance"], row["score_basis"], row["score_prompt_version"]),
                         (7, "abstract", "test-score-v2"))
        self.assertEqual(row["score_model"], "test-model")
        self.assertIsNone(row["score_error"])
        self.assertEqual(self.run_pipeline()["score_attempted"], 0)

    def test_failure_preserves_prior_score_and_successful_analysis_then_recovers(self):
        aid = self.seed(1, abstract=ABSTRACT, analysis="Keep original analysis", analysis_status="ok")
        before = self.row(aid)
        self.score_failure = True
        result = self.run_pipeline()
        self.assertEqual((result["scored_failed"], result["rescored_failed"]), (1, 1))
        after = self.row(aid)
        for key in ("relevance", "relevance_reason", "score_model", "score_basis", "score_prompt_version",
                    "score_status", "processed", "analysis", "analysis_status"):
            self.assertEqual(after[key], before[key], key)
        self.assertTrue(after["score_error"].startswith(ABSTRACT_RESCORE_ERROR))
        self.score_failure = False
        self.assertEqual(self.run_pipeline()["rescored_ok"], 1)
        self.assertIsNone(self.row(aid)["score_error"])
        self.assertEqual(self.row(aid)["analysis"], "Keep original analysis")
        self.assertEqual(self.analysis_calls, [])

    def test_analysis_failure_after_low_to_high_rescore_is_recoverable(self):
        self.config["analyzer"]["analyze_abstract_only"] = True
        aid = self.seed(1, abstract=ABSTRACT)
        self.analysis_failure = True
        self.run_pipeline()
        self.assertEqual((self.row(aid)["processed"], self.row(aid)["analysis_status"]), (0, "failed"))
        self.analysis_failure = False
        result = self.run_pipeline()
        self.assertEqual(result["score_attempted"], 0)
        self.assertEqual(self.row(aid)["analysis_status"], "ok")
        self.assertEqual(self.row(aid)["processed"], 1)

    def test_failed_rescore_does_not_block_existing_analysis_retry(self):
        self.config["analyzer"]["analyze_abstract_only"] = True
        aid = self.seed(1, abstract=ABSTRACT, relevance=8, processed=0, analysis_status="failed")
        self.score_failure = True
        self.run_pipeline()
        self.assertEqual(self.analysis_calls, [aid])
        self.assertEqual(self.row(aid)["analysis_status"], "ok")
        self.assertEqual(self.row(aid)["relevance"], 8)
        self.assertTrue(self.row(aid)["score_error"].startswith(ABSTRACT_RESCORE_ERROR))

    def test_previous_rescore_failure_remains_eligible_for_analysis_retry(self):
        aid = self.seed(
            1, abstract=ABSTRACT, relevance=8, processed=0, analysis_status="failed",
            score_error=ABSTRACT_RESCORE_ERROR + "temporary",
        )
        queue = self.db.get_analysis_queue(self.config, DAY, trial=True)
        self.assertEqual([row["id"] for row in queue["selected"]], [aid])

    def test_lower_new_score_is_saved_without_erasing_analysis(self):
        aid = self.seed(1, abstract=ABSTRACT, relevance=8, analysis_status="ok", analysis="Kept")
        self.score = 3
        self.run_pipeline()
        self.assertEqual((self.row(aid)["relevance"], self.row(aid)["processed"]), (3, 1))
        self.assertEqual(self.row(aid)["analysis"], "Kept")
        self.assertEqual(self.analysis_calls, [])

    def test_unrelated_success_manual_and_out_of_window_rows_not_reopened(self):
        eligible = self.seed(1, abstract=ABSTRACT)
        self.seed(2)
        self.seed(3, abstract=ABSTRACT, score_basis="abstract")
        self.seed(4, abstract=ABSTRACT, pub_date="2020-01-01")
        for n, marker in enumerate(("needs_date", "invalid_date", "outside_window"), 5):
            self.seed(n, abstract=ABSTRACT, processing_status=marker)
        for n, marker in enumerate(("score_model", "score_basis", "discovered_via"), 8):
            self.seed(n, abstract=ABSTRACT, **{marker: "manual"})
        queue = self.db.get_processing_queue(self.config, DAY)
        self.assertEqual([r["id"] for r in queue["selected"]], [eligible])

    def test_legacy_empty_processing_state_matches_read_only_preview(self):
        ids = [self.seed(1, abstract=ABSTRACT, processing_status=None),
               self.seed(2, abstract=ABSTRACT, processing_status="")]
        self.seed(3, abstract=ABSTRACT, processing_status=None, pub_date="2020-01-01")
        before = list(self.db.get_connection().iterdump())
        preview = main.run_once(self.config, DAY, preview=True, refresh=True)
        self.assertEqual(list(self.db.get_connection().iterdump()), before)
        queue = self.db.get_processing_queue(self.config, DAY)
        self.assertEqual(preview["rescore_selected"], 2)
        self.assertEqual({row["id"] for row in queue["selected"]}, set(ids))

    def test_new_and_rescore_candidates_share_run_and_retry_budgets(self):
        for n in range(45):
            self.seed(n, abstract=ABSTRACT, score_error=ABSTRACT_RESCORE_ERROR + "temporary")
        for n in range(45, 90):
            self.seed(n, abstract=ABSTRACT, score_status="failed", relevance=None, processed=0)
        for n in range(90, 160):
            self.seed(n, abstract=ABSTRACT)
        for n in range(160, 230):
            self.seed(n, abstract=ABSTRACT, score_status="", relevance=None, processed=0)
        for trial, budget in ((False, 100), (True, 30)):
            with self.subTest(trial=trial):
                queue = self.db.get_processing_queue(self.config, DAY, trial=trial)
                selected = queue["selected"]
                self.assertEqual(len(selected), budget)
                self.assertEqual(len({row["id"] for row in selected}), budget)
                self.assertEqual(sum(is_score_retry(row) for row in selected), max(1, budget // 4))
                self.assertGreater(queue["rescore_selected"], 0)
        self.assertEqual(self.run_pipeline()["score_attempted"], 30)

    def test_deterministic_runtime_error_aborts_remaining_scores(self):
        ids = [
            self.seed(n, abstract=ABSTRACT, score_status="", relevance=None,
                      score_model=None, score_basis=None, processed=0)
            for n in range(1, 4)
        ]
        self.score_exception = LLMRuntimeDependencyError("brotli incompatible")

        result = self.run_pipeline()

        self.assertEqual(result["score_attempted"], 1)
        self.assertEqual(result["score_aborted"], 2)
        self.assertEqual(result["scored_failed"], 1)
        self.assertEqual(result["score_errors"]["LLMRuntimeDependencyError"]["count"], 1)
        self.assertEqual(len(self.scored), 1)
        attempted_id = self.scored[0]
        rows = {row["id"]: row for row in self.db.get_articles_by_ids(ids)}
        self.assertEqual(rows[attempted_id]["score_status"], "failed")
        for aid in ids:
            if aid != attempted_id:
                self.assertEqual(rows[aid]["score_status"], "")
                self.assertEqual(rows[aid]["processing_status"], "eligible")

    def test_budget_deferred_rescore_does_not_use_stale_score_for_analysis(self):
        self.config["processing"] = {"trial_max_score_articles": 1}
        self.config["analyzer"]["analyze_abstract_only"] = True
        scored_id = self.seed(1, abstract=ABSTRACT, score_status="", relevance=None,
                              score_model=None, score_basis=None, processed=0)
        deferred_id = self.seed(2, abstract=ABSTRACT, relevance=8, processed=0,
                                analysis_status="failed")

        result = self.run_pipeline()

        self.assertEqual(result["score_attempted"], 1)
        self.assertEqual(result["score_deferred"], 1)
        self.assertEqual(result["rescore_selected"], 0)
        self.assertIn(scored_id, self.analysis_calls)
        self.assertNotIn(deferred_id, self.analysis_calls)
        deferred = self.row(deferred_id)
        self.assertEqual(deferred["score_basis"], "title")
        self.assertEqual(deferred["analysis_status"], "failed")

    def test_duplicate_with_later_abstract_keeps_identity_and_user_relations(self):
        aid = self.seed(1)
        topic = self.db.create_topic("Saved topic")
        self.db.add_topic_papers(topic, [aid])
        self.db.add_chat_message(aid, "user", "Saved question")
        with self.db.get_connection() as conn:
            conn.execute("UPDATE articles SET note='Keep note', tags='tag', starred=1, zotero_key='fake-key' WHERE id=?", (aid,))
        original = self.row(aid)
        self.discoveries = [{**original, "id": None, "abstract": ""},
                            {**original, "id": None, "abstract": ABSTRACT}]
        result = self.run_pipeline()
        self.assertEqual((result["new_articles"], result["abstract_completed"], result["rescored_ok"]), (0, 1, 1))
        row = self.row(aid)
        for key in ("id", "doi", "title", "note", "tags", "starred", "zotero_key", "discovered_via"):
            self.assertEqual(row[key], original[key], key)
        self.assertEqual(row["abstract"], ABSTRACT)
        conn = self.db.get_connection()
        self.assertEqual(conn.execute("SELECT count(*) FROM topic_papers WHERE article_id=?", (aid,)).fetchone()[0], 1)
        self.assertEqual(conn.execute("SELECT count(*) FROM chat_messages WHERE article_id=?", (aid,)).fetchone()[0], 1)

    def test_identity_mismatch_does_not_replace_metadata(self):
        aid = self.seed(1)
        original = self.row(aid)
        self.assertEqual(self.db.complete_discovered_metadata({"doi": "10.1234/other", "title": original["title"],
                                                               "url": "https://example.org/other", "abstract": ABSTRACT}), {})
        self.assertEqual(self.db.complete_discovered_metadata({"doi": "10.1234/other", "url": original["url"],
                                                               "abstract": ABSTRACT}), {})
        self.assertFalse(self.row(aid)["abstract"])
        self.assertEqual(self.db.complete_discovered_metadata({"url": original["url"], "abstract": ABSTRACT}),
                         {"abstract": ABSTRACT})
        self.assertEqual(self.db.complete_discovered_metadata({"doi": original["doi"], "abstract": "different"}), {})
        self.assertEqual(self.row(aid)["abstract"], ABSTRACT)

    def test_preview_counts_later_abstract_without_writing_or_model_calls(self):
        aid = self.seed(1)
        self.discoveries = [{**self.row(aid), "abstract": ABSTRACT}]
        before = list(self.db.get_connection().iterdump())
        result = main.run_once(self.config, DAY, preview=True, refresh=True)
        self.assertEqual((result["score_planned"], result["rescore_selected"]), (1, 1))
        self.assertEqual(self.scored, [])
        self.assertEqual(list(self.db.get_connection().iterdump()), before)
        self.assertFalse(self.row(aid)["abstract"])

    def test_repeated_source_can_fill_unknown_date_without_bulk_repair(self):
        aid = self.seed(1, pub_date="", abstract="", processing_status="needs_date",
                        score_status="", score_model=None, score_basis=None, relevance=None, processed=0)
        self.discoveries = [{**self.row(aid), "pub_date": DAY, "date_source": "rss_dc_date", "abstract": ABSTRACT}]
        result = self.run_pipeline()
        row = self.row(aid)
        self.assertEqual((result["new_articles"], result["dates_completed"], result["score_attempted"]), (0, 1, 1))
        self.assertEqual((row["pub_date"], row["date_source"]), (DAY, "rss_dc_date"))
        self.assertEqual(row["processing_status"], "eligible")
        self.assertEqual(self.db.get_connection().execute("SELECT count(*) FROM articles").fetchone()[0], 1)

    def test_metadata_does_not_replace_known_date_or_reopen_manual_or_old_rows(self):
        for n, fields in enumerate((
                {"pub_date": "2020-01-01", "processing_status": "outside_window"},
                {"pub_date": DAY, "processing_status": "invalid_date"},
                {"pub_date": "", "processing_status": "needs_date", "discovered_via": "manual"},
                {"pub_date": "", "processing_status": "needs_date"}), 1):
            aid = self.seed(n, **fields)
            before = self.row(aid)
            decision = {"decision": "eligible", "publication_date": DAY, "date_source": "rss_dc_date"}
            if n == 4:
                decision.update(decision="outside_window", publication_date="2020-01-01")
            changed = self.db.complete_discovered_metadata({"doi": before["doi"], "pub_date": DAY,
                                                            "_admission": decision})
            self.assertEqual(changed, {})
            self.assertEqual(self.row(aid), before)

    def test_post_score_abstract_only_triggers_next_round_not_extra_call(self):
        from fetchers.models import FetchResult
        self.config["fetcher"]["use_fulltext"] = True
        aid = self.seed(1, score_status="", score_basis=None, score_model=None, relevance=None, processed=0)
        calls = []
        def score(analyzer, article):
            calls.append(article["id"])
            return {"score": 7, "reason": "Limited evidence", "model": "test-model",
                    "basis": "abstract" if article.get("abstract") else "title", "prompt_version": "test-score-v2"}
        with patch.object(main.LLMAnalyzer, "filter_relevance", score), \
             patch.object(main.JournalFetcher, "fetch_fulltext_batch", create=True,
                          return_value=[FetchResult(text=ABSTRACT)]):
            first = self.run_pipeline()
            self.assertEqual(first["score_attempted"], 1)
            self.assertEqual(self.row(aid)["score_basis"], "title")
            second = self.run_pipeline()
        self.assertEqual(second["rescored_ok"], 1)
        self.assertEqual(calls, [aid, aid])
        self.assertEqual(self.row(aid)["score_basis"], "abstract")

    def test_score_persistence_error_does_not_mark_processed(self):
        aid = self.seed(1, abstract=ABSTRACT, processed=0)
        original = self.row(aid)
        real_update = Database.update_article_fields
        def update(db, article_id, **fields):
            if article_id == aid and fields.get("score_model") == "test-model":
                return False
            return real_update(db, article_id, **fields)
        with patch.object(Database, "update_article_fields", update):
            result = self.run_pipeline()
        self.assertGreater(result["db_errors"], 0)
        for key in ("relevance", "score_basis", "score_model", "score_prompt_version", "processed"):
            self.assertEqual(self.row(aid)[key], original[key], key)
        self.assertEqual(result["scored_ok"], 0)

    def test_metadata_persistence_failure_prevents_source_cursor_advancement(self):
        aid = self.seed(1)
        jid = self.db.add_journal("Test source", "https://example.org/feed")
        with self.db.get_connection() as conn:
            conn.execute("UPDATE journals SET last_run='2026-09-13' WHERE id=?", (jid,))
        self.sources = [{"source_id": jid, "success": True, "complete": True, "truncated": False,
                         "window_end": DAY, "raw_count": 1}]
        self.discoveries = [{**self.row(aid), "abstract": ABSTRACT}]
        self.config["tracking"]["enabled"] = True
        with patch.object(Database, "complete_discovered_metadata", side_effect=sqlite3.OperationalError("write failed")), \
             patch("main.collect_tracking_articles", return_value=([], {"sources": ["test-author"]})), \
             patch("main.record_tracking_edges", return_value=0), \
             patch("main.mark_tracking_cursor") as cursor:
            result = self.run_pipeline()
        cursor.assert_not_called()
        self.assertGreater(result["db_errors"], 0)
        self.assertEqual(result["score_attempted"], 0)
        last_run = self.db.get_connection().execute("SELECT last_run FROM journals WHERE id=?", (jid,)).fetchone()[0]
        self.assertEqual(last_run, "2026-09-13")


if __name__ == "__main__":
    unittest.main()
