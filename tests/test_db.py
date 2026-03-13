"""
tests/test_db.py - Database 去重逻辑的单元测试

运行：
    python -m pytest tests/test_db.py -v
"""
import sys
import unittest
from pathlib import Path

# 将 src/ 目录加入模块搜索路径
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from core.db import Database, _compute_title_hash


# ─────────────────────────────────────────────────────────────
# 工具函数
# ─────────────────────────────────────────────────────────────

def _make_article(**kwargs) -> dict:
    """构造测试用文章字典"""
    base = {
        "doi":      "",
        "title":    "Test Article Title",
        "journal":  "Test Journal",
        "authors":  ["Author A"],
        "pub_date": "2024-01-01",
        "url":      "",
        "abstract": "Test abstract.",
        "relevance": 7.0,
    }
    base.update(kwargs)
    return base


# ─────────────────────────────────────────────────────────────
# 1. 辅助函数测试
# ─────────────────────────────────────────────────────────────

class TestComputeTitleHash(unittest.TestCase):
    def test_same_title_same_hash(self):
        h1 = _compute_title_hash("Quantum Computing in Chemistry")
        h2 = _compute_title_hash("Quantum Computing in Chemistry")
        self.assertEqual(h1, h2)

    def test_case_insensitive(self):
        h1 = _compute_title_hash("Quantum Computing")
        h2 = _compute_title_hash("quantum computing")
        self.assertEqual(h1, h2)

    def test_whitespace_normalized(self):
        h1 = _compute_title_hash("  Quantum   Computing  ")
        h2 = _compute_title_hash("Quantum Computing")
        self.assertEqual(h1, h2)

    def test_different_titles_different_hash(self):
        h1 = _compute_title_hash("Quantum Computing")
        h2 = _compute_title_hash("Classical Computing")
        self.assertNotEqual(h1, h2)

    def test_empty_string(self):
        h = _compute_title_hash("")
        self.assertIsInstance(h, str)
        self.assertEqual(len(h), 32)  # MD5 hex digest is 32 chars


# ─────────────────────────────────────────────────────────────
# 2. 数据库初始化测试
# ─────────────────────────────────────────────────────────────

class TestDatabaseInit(unittest.TestCase):
    def setUp(self):
        self.db = Database(":memory:")

    def test_tables_created(self):
        with self.db._conn() as conn:
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
        self.assertIn("articles", tables)
        self.assertIn("daily_reports", tables)

    def test_title_hash_column_exists(self):
        with self.db._conn() as conn:
            cols = {
                row[1]
                for row in conn.execute(
                    "PRAGMA table_info(articles)"
                ).fetchall()
            }
        self.assertIn("title_hash", cols)

    def test_reinit_idempotent(self):
        """反复初始化不应抛出异常"""
        db2 = Database(":memory:")
        self.assertIsNotNone(db2)


# ─────────────────────────────────────────────────────────────
# 3. is_processed 多维度测试
# ─────────────────────────────────────────────────────────────

class TestIsProcessed(unittest.TestCase):
    def setUp(self):
        self.db = Database(":memory:")
        self.db.save_article(_make_article(
            doi="10.1021/test001",
            url="https://pubs.acs.org/doi/10.1021/test001",
            title="Catalytic Activity of Metal Nanoparticles",
        ))

    def test_doi_match(self):
        self.assertTrue(self.db.is_processed(doi="10.1021/test001"))

    def test_url_match(self):
        self.assertTrue(
            self.db.is_processed(url="https://pubs.acs.org/doi/10.1021/test001")
        )

    def test_title_hash_match(self):
        self.assertTrue(
            self.db.is_processed(title="Catalytic Activity of Metal Nanoparticles")
        )

    def test_title_case_insensitive(self):
        self.assertTrue(
            self.db.is_processed(title="catalytic activity of metal nanoparticles")
        )

    def test_unknown_doi_returns_false(self):
        self.assertFalse(self.db.is_processed(doi="10.9999/unknown"))

    def test_empty_args_returns_false(self):
        self.assertFalse(self.db.is_processed())

    def test_backward_compat_positional_doi(self):
        """旧版仅传 doi 字符串的调用方式应继续有效"""
        self.assertTrue(self.db.is_processed("10.1021/test001"))
        self.assertFalse(self.db.is_processed(""))


# ─────────────────────────────────────────────────────────────
# 4. check_duplicate 多维度测试
# ─────────────────────────────────────────────────────────────

class TestCheckDuplicate(unittest.TestCase):
    def setUp(self):
        self.db = Database(":memory:")
        self.db.save_article(_make_article(
            doi="10.1021/acid001",
            url="https://example.com/acid001",
            title="Acid-Base Reactions in Aqueous Solutions",
        ))

    # --- DOI 精确匹配 ---
    def test_doi_duplicate(self):
        is_dup, reason = self.db.check_duplicate(
            _make_article(doi="10.1021/acid001", url="https://other.com/x", title="Other")
        )
        self.assertTrue(is_dup)
        self.assertIn("DOI", reason)

    def test_different_doi_not_duplicate(self):
        is_dup, _ = self.db.check_duplicate(
            _make_article(doi="10.1021/new999", url="https://example.com/new999",
                          title="Completely Different Paper")
        )
        self.assertFalse(is_dup)

    # --- URL 精确匹配 ---
    def test_url_duplicate(self):
        is_dup, reason = self.db.check_duplicate(
            _make_article(doi="", url="https://example.com/acid001", title="Other")
        )
        self.assertTrue(is_dup)
        self.assertIn("URL", reason)

    def test_different_url_not_duplicate(self):
        is_dup, _ = self.db.check_duplicate(
            _make_article(doi="", url="https://example.com/new999",
                          title="Brand New Study")
        )
        self.assertFalse(is_dup)

    # --- 标题哈希 ---
    def test_exact_title_duplicate(self):
        is_dup, reason = self.db.check_duplicate(
            _make_article(doi="", url="", title="Acid-Base Reactions in Aqueous Solutions")
        )
        self.assertTrue(is_dup)
        self.assertIn("标题", reason)

    def test_title_case_insensitive_duplicate(self):
        is_dup, _ = self.db.check_duplicate(
            _make_article(doi="", url="", title="acid-base reactions in aqueous solutions")
        )
        self.assertTrue(is_dup)

    # --- 标题相似度 ---
    def test_similar_title_duplicate(self):
        # 稍微修改标题，相似度 > 0.80
        is_dup, reason = self.db.check_duplicate(
            _make_article(
                doi="", url="",
                title="Acid-Base Reactions in Aqueous Solution"  # 末尾少了 's'
            )
        )
        self.assertTrue(is_dup)
        self.assertIn("相似", reason)

    def test_very_different_title_not_duplicate(self):
        is_dup, _ = self.db.check_duplicate(
            _make_article(
                doi="", url="",
                title="Machine Learning Applications in Drug Discovery"
            )
        )
        self.assertFalse(is_dup)

    # --- 空字段 ---
    def test_all_empty_fields_not_duplicate(self):
        is_dup, _ = self.db.check_duplicate(_make_article(doi="", url="", title=""))
        self.assertFalse(is_dup)

    def test_no_doi_url_new_title(self):
        is_dup, _ = self.db.check_duplicate(
            _make_article(doi="", url="", title="Completely Unique Research on Polymers")
        )
        self.assertFalse(is_dup)


# ─────────────────────────────────────────────────────────────
# 5. find_similar_articles 测试
# ─────────────────────────────────────────────────────────────

class TestFindSimilarArticles(unittest.TestCase):
    def setUp(self):
        self.db = Database(":memory:")
        self.db.save_article(_make_article(
            doi="10.1021/nano001",
            title="Synthesis of Gold Nanoparticles via Chemical Reduction",
        ))
        self.db.save_article(_make_article(
            doi="10.1021/poly001",
            title="Polymer Chain Dynamics in Dense Solutions",
        ))

    def test_finds_similar(self):
        results = self.db.find_similar_articles(
            "Synthesis of Gold Nanoparticles by Chemical Reduction"
        )
        self.assertTrue(len(results) >= 1)
        self.assertIn("Gold Nanoparticles", results[0]["title"])

    def test_exact_match_returns_high_similarity(self):
        results = self.db.find_similar_articles(
            "Synthesis of Gold Nanoparticles via Chemical Reduction"
        )
        self.assertTrue(len(results) >= 1)
        self.assertEqual(results[0]["similarity"], 1.0)

    def test_unrelated_title_returns_empty(self):
        results = self.db.find_similar_articles(
            "Quantum Entanglement in Photonic Systems"
        )
        self.assertEqual(results, [])

    def test_sorted_by_similarity_desc(self):
        # directly populate DB to avoid being blocked by check_duplicate
        with self.db._conn() as conn:
            from core.db import _compute_title_hash
            conn.execute(
                """INSERT INTO articles (doi, title, title_hash) VALUES (?, ?, ?)""",
                (
                    "10.1021/nano002",
                    "Synthesis of Gold Nanoparticles via Chemical Reduction Method",
                    _compute_title_hash(
                        "Synthesis of Gold Nanoparticles via Chemical Reduction Method"
                    ),
                ),
            )
        results = self.db.find_similar_articles(
            "Synthesis of Gold Nanoparticles via Chemical Reduction"
        )
        self.assertTrue(len(results) >= 2)
        for i in range(len(results) - 1):
            self.assertGreaterEqual(
                results[i]["similarity"], results[i + 1]["similarity"]
            )

    def test_empty_title_returns_empty(self):
        results = self.db.find_similar_articles("")
        self.assertEqual(results, [])

    def test_custom_threshold(self):
        # 用极高阈值，应该只匹配完全相同的标题
        results = self.db.find_similar_articles(
            "Synthesis of Gold Nanoparticles via Chemical Reduction",
            threshold=0.99,
        )
        # 完全相同标题应该被匹配
        self.assertTrue(len(results) >= 1)
        self.assertEqual(results[0]["similarity"], 1.0)


# ─────────────────────────────────────────────────────────────
# 6. save_article 去重集成测试
# ─────────────────────────────────────────────────────────────

class TestSaveArticle(unittest.TestCase):
    def setUp(self):
        self.db = Database(":memory:")

    def test_new_article_returns_true(self):
        result = self.db.save_article(_make_article(
            doi="10.1021/new001", title="Novel Catalyst Design"
        ))
        self.assertTrue(result)

    def test_duplicate_doi_returns_false(self):
        art = _make_article(doi="10.1021/dup001", title="Duplicate DOI Article")
        self.db.save_article(art)
        result = self.db.save_article(art)
        self.assertFalse(result)

    def test_duplicate_url_returns_false(self):
        art = _make_article(doi="", url="https://example.com/paper/1",
                            title="Article With URL")
        self.db.save_article(art)
        art2 = _make_article(doi="", url="https://example.com/paper/1",
                             title="Same URL Different Title")
        result = self.db.save_article(art2)
        self.assertFalse(result)

    def test_duplicate_title_no_doi_returns_false(self):
        art = _make_article(doi="", url="", title="Unique Title For This Test")
        self.db.save_article(art)
        result = self.db.save_article(art)
        self.assertFalse(result)

    def test_title_hash_saved(self):
        title = "Photocatalytic Water Splitting"
        self.db.save_article(_make_article(title=title))
        with self.db._conn() as conn:
            row = conn.execute(
                "SELECT title_hash FROM articles WHERE title = ?", (title,)
            ).fetchone()
        self.assertIsNotNone(row)
        self.assertIsNotNone(row[0])

    def test_empty_doi_stored_as_null(self):
        """空 DOI 应以 NULL 存入数据库，避免 UNIQUE 约束冲突"""
        self.db.save_article(_make_article(
            doi="", title="Electrochemical Impedance Spectroscopy of Zinc Anodes"
        ))
        self.db.save_article(_make_article(
            doi="", title="Molecular Dynamics Simulation of Lipid Bilayers"
        ))
        with self.db._conn() as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM articles WHERE doi IS NULL"
            ).fetchone()[0]
        self.assertEqual(count, 2)

    def test_multiple_articles_no_doi(self):
        """多篇无 DOI 文章（标题各异）均可保存"""
        distinct_titles = [
            "Electrochemical Reduction of Carbon Dioxide on Copper Electrodes",
            "Single-Molecule Fluorescence Imaging of Protein Folding Dynamics",
            "Heterogeneous Nucleation Kinetics in Supercooled Liquid Metals",
            "Plasmonic Enhancement of Photovoltaic Efficiency in Perovskite Cells",
            "Supramolecular Self-Assembly of Amphiphilic Block Copolymers in Water",
        ]
        for title in distinct_titles:
            ok = self.db.save_article(_make_article(doi="", title=title))
            self.assertTrue(ok, f"Failed to save: {title}")


# ─────────────────────────────────────────────────────────────
# 7. 统计方法测试
# ─────────────────────────────────────────────────────────────

class TestDedupStats(unittest.TestCase):
    def setUp(self):
        self.db = Database(":memory:")

    def test_get_dedup_stats_empty(self):
        stats = self.db.get_dedup_stats()
        self.assertEqual(stats["total_articles"], 0)
        self.assertEqual(stats["with_doi"], 0)
        self.assertEqual(stats["with_url"], 0)
        self.assertEqual(stats["duplicate_titles"], 0)

    def test_get_dedup_stats_counts(self):
        self.db.save_article(_make_article(
            doi="10.1021/s001", url="https://a.com/1",
            title="Electrochemical Nitrogen Fixation on Iron Catalysts",
        ))
        self.db.save_article(_make_article(
            doi="", url="https://a.com/2",
            title="Quantum Dot Solar Cells with High Efficiency",
        ))
        self.db.save_article(_make_article(
            doi="", url="",
            title="Ultrafast Spectroscopy of Molecular Vibrations",
        ))
        stats = self.db.get_dedup_stats()
        self.assertEqual(stats["total_articles"], 3)
        self.assertEqual(stats["with_doi"], 1)
        self.assertEqual(stats["with_url"], 2)
        self.assertEqual(stats["duplicate_titles"], 0)

    def test_get_duplicate_count_zero_for_clean_db(self):
        self.db.save_article(_make_article(doi="10.1/a", title="Article A"))
        self.db.save_article(_make_article(doi="10.1/b", title="Article B"))
        self.assertEqual(self.db.get_duplicate_count(), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
