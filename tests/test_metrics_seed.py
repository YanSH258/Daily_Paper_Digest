"""Journal seeds must preserve user edits unless explicitly reset."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core.db import Database
from utils.journal_metrics import SEED_METRICS


class MetricsSeedTests(unittest.TestCase):
    OLD_TIMESTAMP = "2001-02-03 04:05:06"
    FIELDS = ("name", "full_name", "if_value", "cas_zone", "issn", "updated_at")

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="dpd-metrics-seed-")
        self.addCleanup(tmp.cleanup)
        self.db_path = str(Path(tmp.name) / "metrics.db")
        self.db = self.open_db()
        self.seed = SEED_METRICS[0]

    def open_db(self):
        db = Database(self.db_path)
        self.addCleanup(db.close)
        return db

    def reopen_db(self):
        self.db.close()
        self.db = self.open_db()

    def metric(self, name):
        row = self.db.get_connection().execute(
            "SELECT name, full_name, if_value, cas_zone, issn, updated_at "
            "FROM journal_metrics WHERE name = ?", (name,)
        ).fetchone()
        self.assertIsNotNone(row)
        return dict(zip(self.FIELDS, row))

    def set_old_timestamp(self, name):
        conn = self.db.get_connection()
        conn.execute(
            "UPDATE journal_metrics SET updated_at = ? WHERE name = ?",
            (self.OLD_TIMESTAMP, name),
        )
        conn.commit()

    def customize_metric(self):
        self.assertTrue(self.db.upsert_journal_metric(
            self.seed["name"], "Manually curated journal", 99.25, 4, "9999-9999"
        ))
        self.set_old_timestamp(self.seed["name"])
        return self.metric(self.seed["name"])

    def test_custom_metrics_survive_reopening(self):
        # Explicitly cleared values must also remain cleared on startup.
        for values in (("Manually curated journal", 99.25, 4, "9999-9999"),
                       ("", None, None, "")):
            with self.subTest(values=values):
                self.assertTrue(self.db.upsert_journal_metric(self.seed["name"], *values))
                self.set_old_timestamp(self.seed["name"])
                before = self.metric(self.seed["name"])
                self.reopen_db()
                self.assertEqual(self.metric(self.seed["name"]), before)

    def test_repeated_initialization_preserves_timestamp(self):
        # Keep seed values unchanged so this catches timestamp-only writes.
        self.set_old_timestamp(self.seed["name"])
        before = self.metric(self.seed["name"])
        for attempt in range(2):
            with self.subTest(attempt=attempt):
                self.reopen_db()
                self.assertEqual(self.metric(self.seed["name"]), before)

    def test_new_seed_is_added_without_changing_existing_metrics(self):
        before = self.customize_metric()
        new_seed = {
            "name": "New Test Journal",
            "full_name": "Newly Seeded Test Journal",
            "if_value": 5.25,
            "cas_zone": 2,
            "issn": "1234-5678",
        }
        with patch("utils.journal_metrics.SEED_METRICS", [*SEED_METRICS, new_seed]):
            self.reopen_db()
        added = self.metric(new_seed["name"])
        self.assertEqual({key: added[key] for key in new_seed}, new_seed)
        self.assertTrue(added["updated_at"])
        self.assertEqual(self.metric(self.seed["name"]), before)
        self.assertEqual(len(self.db.list_journal_metrics()), len(SEED_METRICS) + 1)

    def test_explicit_reset_restores_seed_metrics(self):
        before = self.customize_metric()
        self.assertEqual(self.db.reseed_journal_metrics(), len(SEED_METRICS))
        restored = self.metric(self.seed["name"])
        self.assertEqual(
            {key: restored[key] for key in self.FIELDS if key != "updated_at"},
            {key: self.seed.get(key) for key in self.FIELDS if key != "updated_at"},
        )
        self.assertTrue(restored["updated_at"])
        self.assertNotEqual(restored["updated_at"], before["updated_at"])
        self.reopen_db()
        self.assertEqual(self.metric(self.seed["name"]), restored)


if __name__ == "__main__":
    unittest.main()
