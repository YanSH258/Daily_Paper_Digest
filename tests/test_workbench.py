"""研究工作台新功能测试：GB/T 引用、追踪数据、Zotero 解析、周报统计。"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from core.db import Database  # noqa: E402
from utils.citation import format_article_citation  # noqa: E402
from integrations.zotero_client import _parse_creators  # noqa: E402
from utils.similarity import build_profile, compute_prior  # noqa: E402


class TestGBT7714(unittest.TestCase):
    def test_gbt_format(self):
        a = {"title": "Machine Learning Potentials", "authors": "Han, Lee",
             "journal": "Nature", "pub_date": "2026-09-08", "doi": "10.1038/x",
             "url": "https://doi.org/10.1038/x"}
        s = format_article_citation(a, "gbt")
        self.assertIn("[J]", s)          # 文献类型标识
        self.assertIn("Nature", s)
        self.assertIn("10.1038/x", s)


class TestTrackingDB(unittest.TestCase):
    def test_watch_seed_and_edges(self):
        db = Database(":memory:")
        ids = db.save_articles_batch([
            {"doi": "10.1/seed", "title": "Seed Paper", "journal": "J", "abstract": "x"},
            {"doi": "10.1/citing", "title": "Citing Paper", "journal": "J", "abstract": "y"},
        ])
        db.set_watch_seed(ids[0], True)
        self.assertTrue(db.is_seed_watched(ids[0]))
        seeds = db.get_watched_seeds()
        self.assertEqual(len(seeds), 1)
        db.mark_seed_checked(ids[0])
        self.assertIsNotNone(db.get_watched_seeds()[0]["last_checked_at"])

        db.add_citation_edge(ids[0], ids[1])
        db.add_citation_edge(ids[0], ids[1])  # 幂等
        edges = db.get_citation_edges_since(days=7)
        self.assertEqual(len(edges), 1)
        self.assertEqual(edges[0]["seed_title"], "Seed Paper")

        db.set_watch_seed(ids[0], False)
        self.assertFalse(db.is_seed_watched(ids[0]))

    def test_watch_authors(self):
        db = Database(":memory:")
        aid = db.add_watch_author("Nongnuch Artrith", "A508etect")
        self.assertTrue(aid)
        self.assertEqual(len(db.list_watch_authors()), 1)
        db.mark_author_run(aid)
        self.assertIsNotNone(db.list_watch_authors()[0]["last_run"])
        self.assertTrue(db.delete_watch_author(aid))
        self.assertEqual(db.list_watch_authors(), [])

    def test_topics(self):
        db = Database(":memory:")
        tid = db.create_topic("MLIP 泛化", "如何跨化学体系迁移", "优先看方法部分")
        self.assertTrue(tid)
        ids = db.save_articles_batch([
            {"doi": "10.1/x", "title": "X", "journal": "J", "abstract": "a"},
            {"doi": "10.1/y", "title": "Y", "journal": "J", "abstract": "b"},
        ])
        self.assertEqual(db.add_topic_papers(tid, ids), 2)
        topic = db.get_topic(tid)
        self.assertEqual(topic["paper_count"] if "paper_count" in topic else len(topic["papers"]), 2)
        self.assertEqual(len(db.list_topics()), 1)
        db.remove_topic_paper(tid, ids[0])
        self.assertEqual(len(db.get_topic(tid)["papers"]), 1)
        self.assertTrue(db.delete_topic(tid))
        self.assertEqual(db.list_topics(), [])

    def test_highlights(self):
        db = Database(":memory:")
        (aid,) = db.save_articles_batch([{"doi": "10.1/h", "title": "H", "abstract": "x"}])
        hid = db.add_highlight(aid, "关键句", "待验证")
        self.assertTrue(hid)
        self.assertEqual(db.get_highlights(aid)[0]["note"], "待验证")
        self.assertTrue(db.delete_highlight(hid))
        self.assertEqual(db.get_highlights(aid), [])


class TestZoteroParsing(unittest.TestCase):
    def test_creators_last_first(self):
        creators = _parse_creators({"authors": "Jumper, John, Evans, Richard"})
        self.assertEqual(creators[0], {"creatorType": "author",
                                       "lastName": "Jumper", "firstName": "John"})
        self.assertEqual(creators[1]["lastName"], "Evans")

    def test_creators_plain(self):
        creators = _parse_creators({"authors": ["John Jumper", "Richard Evans"]})
        self.assertEqual(creators[0]["lastName"], "Jumper")
        self.assertEqual(creators[0]["firstName"], "John")

    def test_creators_semicolon(self):
        creators = _parse_creators({"authors": "Han, Y.; Smith, J.; Lee, K."})
        self.assertEqual(len(creators), 3)
        self.assertEqual(creators[1]["lastName"], "Smith")


class TestSimilarityPrior(unittest.TestCase):
    def test_prior_range_and_empty(self):
        texts = [f"machine learning interatomic potential for {m} systems training dataset"
                 for m in ("oxide", "alloy", "mof", "water", "silicon", "copper")]
        profile = build_profile(texts)
        self.assertIsNotNone(profile)
        prior = compute_prior(profile, "a machine learning interatomic potential for oxide glass")
        self.assertIsNotNone(prior)
        self.assertTrue(0 <= prior <= 10)
        # 完全无关的文本应该低分
        prior_irrel = compute_prior(profile, "clinical trial of drug dosage in patients")
        self.assertLess(prior_irrel or 0, prior)

    def test_profile_needs_samples(self):
        self.assertIsNone(build_profile(["too short"]))


if __name__ == "__main__":
    unittest.main()
