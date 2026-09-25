import sqlite3
from contextlib import closing
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from core.db import Database
import web_server


class ManualScoreMetadataTests(unittest.TestCase):
    def test_count_score_failed_counts_only_retryable_backlog(self):
        with Database(":memory:") as db:
            first, outside, already = db.save_articles_batch([
                {"title": "First failed", "journal": "Journal"},
                {"title": "Outside window failed", "journal": "Journal"},
                {"title": "Processed failed", "journal": "Journal"},
            ])
            db.update_article_fields(first, score_status="failed",
                                     processing_status="eligible", processed=0)
            db.update_article_fields(outside, score_status="failed",
                                     processing_status="outside_window", processed=0)
            db.update_article_fields(already, score_status="failed",
                                     processing_status="eligible", processed=1)
            self.assertEqual(db.count_score_failed(), 1)
            db.update_article_fields(first, score_status="ok", score_error=None)
            self.assertEqual(db.count_score_failed(), 0)

    def test_manual_api_records_source_without_model_call(self):
        with Database(":memory:") as db:
            ctx = SimpleNamespace(db=db)
            for index, score in enumerate((None, 6.0)):
                payload = {"title": f"Manually chosen article {index}", "abstract": "Original abstract."}
                if score is not None:
                    payload["relevance"] = score
                body, code = web_server._manual_add_article(ctx, payload)
                self.assertEqual(code, 200)
                row = db.get_articles_by_ids([body["id"]])[0]
                self.assertEqual(row["relevance"], score if score is not None else 8.0)
                self.assertEqual((row["score_status"], row["score_model"], row["score_basis"]),
                                 ("ok", "manual", "manual"))
                self.assertEqual(row["discovered_via"], "manual")
                self.assertIn("非模型评分", row["relevance_reason"])
                self.assertIsNone(row["score_prompt_version"])
                self.assertEqual(row["starred"], 1)
                self.assertEqual(db.get_processing_queue({}, "2026-09-16")["selected"], [])

    def test_score_statistics_separate_model_manual_and_unknown_origins(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "scores.db")
            with Database(path) as db:
                model_high, model_low, unknown, unscored = db.save_articles_batch([
                    {"title": "Model high", "journal": "Journal"},
                    {"title": "Model low", "journal": "Journal"},
                    {"title": "Legacy unknown", "journal": "Journal", "relevance": 7},
                    {"title": "Unscored", "journal": "Journal"},
                ])
                manual = db.save_manual_article(
                    {"title": "Manual", "journal": "Journal", "relevance": 8}
                )
                db.update_article_fields(
                    model_high, relevance=8, score_status="ok",
                    score_model="test-model", score_basis="abstract",
                )
                db.update_article_fields(
                    model_low, relevance=4, score_status="ok",
                    score_model="test-model", score_basis="title",
                )
                self.assertIsNotNone(manual)
                self.assertIsNotNone(unknown)
                self.assertIsNotNone(unscored)

                def connect():
                    conn = sqlite3.connect(path)
                    conn.row_factory = sqlite3.Row
                    return conn

                ctx = SimpleNamespace(
                    db=db, config={"relevance_threshold": 6}, connect_db=connect,
                )
                summary = web_server._db_summary(ctx)
                trends = web_server._trends_view(ctx, {"months": ["6"]})

            self.assertEqual(summary["model_scored"], 2)
            self.assertEqual(summary["manual_scored"], 1)
            self.assertEqual(summary["unknown_scored"], 1)
            self.assertEqual(summary["avg_relevance"], 6.0)
            self.assertEqual(summary["avg_all_scored_relevance"], 6.75)
            month = trends["monthly"][0]
            self.assertEqual(
                (month["total"], month["model_scored"], month["model_relevant"],
                 month["manual_scored"], month["manual_relevant"],
                 month["unknown_scored"], month["unknown_relevant"]),
                (5, 2, 1, 1, 1, 1, 1),
            )
            self.assertEqual(month["relevant"], 3)
            journal = trends["journals"][0]
            self.assertEqual((journal["model_scored"], journal["manual_scored"],
                              journal["unknown_scored"]), (2, 1, 1))

    def test_score_version_migration_keeps_user_data_and_unknown_origins(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "old.db"
            db = Database(str(path))
            aid = db.save_articles_batch([{"doi": "10.1234/old", "title": "Existing article"}])[0]
            db.update_article_fields(aid, relevance=8.0, score_status="ok", processed=1,
                                     zotero_key="test-zotero-key", analysis="Existing analysis")
            topic = db.create_topic("Research topic")
            db.add_topic_papers(topic, [aid])
            db.add_chat_message(aid, "user", "Existing question")
            conn = db.get_connection()
            conn.execute("UPDATE articles SET starred=1, note='Existing note', tags='dft' WHERE id=?", (aid,))
            conn.execute("ALTER TABLE articles DROP COLUMN score_prompt_version")
            conn.commit()
            expected = conn.execute("SELECT * FROM articles").fetchone()
            old_cols = [r[1] for r in conn.execute("PRAGMA table_info(articles)")]
            relations = {table: conn.execute(f"SELECT * FROM {table}").fetchall()
                         for table in ("topic_papers", "chat_messages")}
            db.close()

            with Database(str(path)) as migrated:
                conn = migrated.get_connection()
                actual = conn.execute(f"SELECT {', '.join(old_cols)} FROM articles").fetchone()
                self.assertEqual(actual, expected)
                row = migrated.get_articles_by_ids([aid])[0]
                self.assertIsNone(row["score_prompt_version"])
                self.assertIsNone(row["score_model"])
                for table, rows in relations.items():
                    self.assertEqual(conn.execute(f"SELECT * FROM {table}").fetchall(), rows)
            backups = list(Path(tmp).glob("*backup*"))
            self.assertEqual(len(backups), 1)
            with closing(sqlite3.connect(backups[0])) as backup:
                self.assertEqual(backup.execute("SELECT * FROM articles").fetchone(), expected)
                self.assertNotIn("score_prompt_version", [r[1] for r in backup.execute("PRAGMA table_info(articles)")])
            with Database(str(path)):
                pass
            self.assertEqual(list(Path(tmp).glob("*backup*")), backups)


if __name__ == "__main__":
    unittest.main()
