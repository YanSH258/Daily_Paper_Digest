"""OpenAlex from_publication_date 规范化：水位线带时间不得 400。"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from integrations.openalex import _normalize_from_date  # noqa: E402


class NormalizeFromDateTests(unittest.TestCase):
    def test_strips_time_component(self):
        self.assertEqual(_normalize_from_date("2026-09-13T13:02:38"), "2026-09-13")
        self.assertEqual(_normalize_from_date("2026-09-13 13:02:38"), "2026-09-13")
        self.assertEqual(_normalize_from_date("2026-09-06"), "2026-09-06")

    def test_empty_or_invalid(self):
        self.assertEqual(_normalize_from_date(""), "")
        self.assertEqual(_normalize_from_date(None or ""), "")
        self.assertEqual(_normalize_from_date("not-a-date"), "")


if __name__ == "__main__":
    unittest.main()
