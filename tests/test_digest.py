"""Phase 0：Daily Digest 选择器单元测试（纯函数，无网络/LLM）。"""
import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from digest.categories import classify_digest_category  # noqa: E402
from digest.freshness import freshness_score  # noqa: E402
from digest.scorer import score_article  # noqa: E402
from digest.selector import select_daily_top  # noqa: E402


NOW = datetime(2026, 9, 10, 12, 0, 0)


def _art(aid, title, relevance=8.0, journal="J. Chem. Phys. (JCP)",
         pub_date="2026-09-09", abstract=""):
    return {
        "id": aid, "title": title, "abstract": abstract, "journal": journal,
        "pub_date": pub_date, "relevance": relevance, "topic": "",
    }


class TestClassify(unittest.TestCase):
    def test_mlip(self):
        a = _art(1, "MACE: A foundation machine learning interatomic potential")
        self.assertEqual(classify_digest_category(a), "mlip")

    def test_dft(self):
        a = _art(2, "Orbital-free density functional theory with neural functionals")
        self.assertEqual(classify_digest_category(a), "dft")

    def test_top_chemistry_journal(self):
        a = _art(3, "Selective C-H activation on porous frameworks", journal="Nature Chemistry")
        self.assertEqual(classify_digest_category(a), "top_chemistry")

    def test_other(self):
        a = _art(4, "Some unrelated economics modeling", journal="Econ J")
        self.assertEqual(classify_digest_category(a), "other")

    def test_regression_fixtures(self):
        import json
        path = Path(__file__).resolve().parent / "fixtures" / "digest_category_cases.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        for case in data["cases"]:
            a = {
                "title": case["title"],
                "abstract": case.get("abstract") or "",
                "journal": case.get("journal") or "",
            }
            got = classify_digest_category(a)
            self.assertEqual(
                got, case["expect"],
                msg=f"fixture {case['name']}: got {got}, expect {case['expect']}",
            )


class TestFreshness(unittest.TestCase):
    def test_today_is_high(self):
        a = _art(1, "x", pub_date="2026-09-10")
        self.assertGreaterEqual(freshness_score(a, now=NOW), 9.0)

    def test_old_is_low(self):
        a = _art(1, "x", pub_date="2026-08-01")
        self.assertLessEqual(freshness_score(a, now=NOW), 1.0)

    def test_missing_neutral(self):
        a = {"id": 1, "title": "x"}
        self.assertEqual(freshness_score(a, now=NOW), 5.0)


class TestScore(unittest.TestCase):
    def test_weights_sum_range(self):
        a = _art(1, "Universal machine learning interatomic potential DPA4",
                 relevance=9.0, journal="Nature", pub_date="2026-09-10")
        s = score_article(a, now=NOW, metrics_by_name={"Nature": {"cas_zone": 1, "if_value": 50}})
        self.assertGreaterEqual(s["final"], 0)
        self.assertLessEqual(s["final"], 10)
        self.assertEqual(s["category"], "mlip")


class TestSelector(unittest.TestCase):
    def _scored(self):
        items = []
        for i in range(20):
            title = "Deep Potential MACE MLIP " + str(i) if i < 8 else f"Random paper {i}"
            a = _art(i + 1, title, relevance=9.0 - i * 0.1, pub_date="2026-09-10")
            s = score_article(a, now=NOW)
            item = dict(a)
            item["scores"] = s
            items.append(item)
        return items

    def test_limit_and_exclusion(self):
        scored = self._scored()
        res = select_daily_top(scored, limit=10, excluded_ids={1, 2})
        ids = {r["id"] for r in res.selected}
        self.assertNotIn(1, ids)
        self.assertNotIn(2, ids)
        self.assertTrue(any("repeat" in x["reason"] for x in res.rejected))
        # max 封顶：mlip≤4 + other≤2，不强行凑满 10
        self.assertLessEqual(len(res.selected), 10)
        self.assertGreaterEqual(len(res.selected), 3)

    def test_soft_quota_prefers_structure(self):
        scored = self._scored()
        res = select_daily_top(scored, limit=10)
        cats = [r["scores"]["category"] for r in res.selected]
        self.assertEqual(cats.count("mlip"), 4)  # max=4
        self.assertLessEqual(cats.count("other"), 2)

    def test_other_capped_at_two(self):
        items = []
        for i in range(8):
            a = _art(i + 1, f"unrelated generic topic {i}", relevance=8 - i * 0.1,
                     journal="Econ J", pub_date="2026-09-10")
            it = dict(a)
            it["scores"] = score_article(a, now=NOW)
            items.append(it)
        for i in range(4):
            a = _art(100 + i, "machine learning interatomic potential MACE",
                     relevance=9 - i * 0.1, pub_date="2026-09-10")
            it = dict(a)
            it["scores"] = score_article(a, now=NOW)
            items.append(it)
        a = _art(200, "crystal structure prediction genetic algorithm",
                 relevance=8.5, pub_date="2026-09-10")
        it = dict(a)
        it["scores"] = score_article(a, now=NOW)
        items.append(it)
        a = _art(201, "density functional theory pseudopotential",
                 relevance=8.4, pub_date="2026-09-10")
        it = dict(a)
        it["scores"] = score_article(a, now=NOW)
        items.append(it)
        res = select_daily_top(items, limit=10)
        cats = [r["scores"]["category"] for r in res.selected]
        self.assertLessEqual(cats.count("other"), 2)
        self.assertGreaterEqual(cats.count("mlip"), 2)
        self.assertGreaterEqual(cats.count("ai_materials"), 1)
        self.assertGreaterEqual(cats.count("dft"), 1)

    def test_min_coverage_for_dft(self):
        # 无 DFT 时也能选满；有 DFT 时至少 1 篇进
        items = []
        for i in range(9):
            a = _art(i + 1, f"machine learning interatomic potential {i}",
                     relevance=9, pub_date="2026-09-10")
            it = dict(a)
            it["scores"] = score_article(a, now=NOW)
            items.append(it)
        a = _art(50, "pseudopotentials for density functional theory meta-GGA",
                 relevance=7.0, pub_date="2026-09-10")
        it = dict(a)
        it["scores"] = score_article(a, now=NOW)
        items.append(it)
        res = select_daily_top(items, limit=10)
        cats = [r["scores"]["category"] for r in res.selected]
        self.assertGreaterEqual(cats.count("dft"), 1)

    def test_other_max_blocks_weak_fill(self):
        # 1 篇 MLIP + 9 篇 other：other.max=2，只能选出 3 篇（不硬凑弱文）
        items = []
        a = _art(1, "machine learning interatomic potential", relevance=10, pub_date="2026-09-10")
        item = dict(a)
        item["scores"] = score_article(a, now=NOW)
        items.append(item)
        for i in range(9):
            b = _art(100 + i, f"unrelated topic paper {i}", relevance=8, pub_date="2026-09-09")
            it = dict(b)
            it["scores"] = score_article(b, now=NOW)
            items.append(it)
        res = select_daily_top(items, limit=10)
        cats = [r["scores"]["category"] for r in res.selected]
        self.assertEqual(cats.count("mlip"), 1)
        self.assertLessEqual(cats.count("other"), 2)
        self.assertLessEqual(len(res.selected), 3)
        self.assertEqual(sum(1 for r in res.selected if r["id"] == 1), 1)


if __name__ == "__main__":
    unittest.main()
