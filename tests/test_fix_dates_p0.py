"""--fix-dates: DOI-based date verification for quarantined articles."""
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from utils import backlog

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))


class FixDatesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "t.db"
        with sqlite3.connect(self.path) as conn:
            conn.executescript("""
                CREATE TABLE articles(id INTEGER PRIMARY KEY, doi TEXT, title TEXT,
                    pub_date TEXT, journal TEXT, processing_status TEXT DEFAULT 'needs_date',
                    processing_reason TEXT, admitted_at TEXT, queued_at TEXT, date_source TEXT,
                    score_status TEXT DEFAULT '', processed INTEGER DEFAULT 0,
                    starred INTEGER DEFAULT 0);
            """)
            conn.executemany("INSERT INTO articles(id, doi, title) VALUES(?,?,?)", [
                (1, "10.t/in-window", "In window paper"),
                (2, "10.t/old", "Old paper"),
                (3, "10.t/future", "Future paper"),
                (4, "10.t/unknown", "Unresolvable paper"),
                (5, "10.t/crossref-only", "Crossref paper"),
                (6, None, "No DOI paper"),
                (7, "10.t/scored", "Already scored"),
            ])
            conn.execute("UPDATE articles SET score_status='ok', processed=1 WHERE id=7")

    def run_fix(self, openalex_side, crossref_side=None, limit=100):
        with patch("integrations.openalex.get_work_by_doi", side_effect=openalex_side) as oa_mock, \
             patch("utils.backlog._crossref_date", side_effect=crossref_side or (lambda doi, m: (None, None))) as cr_mock:
            stats = backlog.fix_dates(str(self.path), "2026-09-13", days=3, limit=limit, email="a@b.c")
        return stats, oa_mock, cr_mock

    def test_resolution_and_readmission(self):
        openalex_map = {"10.t/in-window": {"pub_date": "2026-09-12"},
                        "10.t/old": {"pub_date": "2020-01-01"},
                        "10.t/future": {"pub_date": "2027-01-01"},
                        "10.t/unknown": None,
                        "10.t/crossref-only": None,
                        "10.t/scored": {"pub_date": "2026-09-12"}}
        def lookup(doi):
            return openalex_map.get(doi)
        stats, oa, cr = self.run_fix(lookup,
                                     crossref_side=lambda doi, m: ("2026-09-11", "crossref_published-online")
                                     if doi == "10.t/crossref-only" else (None, None))
        self.assertEqual(stats["checked"], 5)          # no-DOI and scored rows are not queried
        self.assertEqual(oa.call_count, 5)
        with sqlite3.connect(self.path) as conn:
            status = dict(conn.execute("select id, processing_status from articles").fetchall())
            pub = dict(conn.execute("select id, pub_date from articles").fetchall())
        self.assertEqual(status[1], "eligible")
        self.assertEqual(pub[1], "2026-09-12")
        self.assertEqual(status[2], "outside_window")
        self.assertEqual(status[3], "invalid_date")    # verified-future dates stay quarantined
        self.assertEqual(status[4], "needs_date")      # unresolved stays quarantined
        self.assertEqual(status[5], "eligible")        # crossref fallback resolved it
        self.assertEqual(pub[5], "2026-09-11")
        self.assertTrue(cr.called)
        self.assertEqual(stats["unresolved"], 1)

    def test_limit_and_scored_rows_are_never_touched(self):
        stats, _, _ = self.run_fix(lambda doi: {"pub_date": "2026-09-12"}, limit=2)
        self.assertEqual(stats["checked"], 2)
        with sqlite3.connect(self.path) as conn:
            row = conn.execute("select score_status, pub_date from articles where id=7").fetchone()
        self.assertEqual(row[0], "ok")


if __name__ == "__main__":
    unittest.main()
