"""Phase 0：Daily Digest 选择器单元测试（纯函数，无网络/LLM）。"""
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from digest.builder import build_daily_digest  # noqa: E402
from digest.categories import classify_digest_category  # noqa: E402
from digest.config import collect_daily_config, validate_digest_config  # noqa: E402
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


class TestFreshness30DayLinear(unittest.TestCase):
    """新鲜度 = 10 × (1 − 天龄/30)：30 天归零，与防重窗口一致（规范 §3.4）。"""

    NOW_MIDNIGHT = datetime(2026, 9, 10)  # 零点对齐，天龄为整数

    def test_boundaries(self):
        cases = {
            "2026-09-10": 10.0,   # 0 天
            "2026-08-26": 5.0,    # 15 天
            "2026-08-11": 0.0,    # 30 天
            "2026-07-01": 0.0,    # 远超窗口
        }
        for pub, expect in cases.items():
            a = _art(1, "x", pub_date=pub)
            self.assertAlmostEqual(freshness_score(a, now=self.NOW_MIDNIGHT), expect, places=6,
                                   msg=f"pub_date={pub}")

    def test_future_date_capped_at_ten(self):
        a = _art(1, "x", pub_date="2026-09-20")
        self.assertEqual(freshness_score(a, now=self.NOW_MIDNIGHT), 10.0)


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


class TestValidateDigestConfig(unittest.TestCase):
    """digest 配置校验：0 是合法值，非法配置必须报错（规范 §4）。"""

    def _cfg(self, **daily):
        return {"digest": {"daily": daily}, "relevance_threshold": 7.0}

    def test_valid_default_config(self):
        self.assertEqual(validate_digest_config(self._cfg()), [])

    def test_repeat_window_zero_is_legal(self):
        # 回归：旧解析 `or 30` 会把显式 0（关闭防重）吞成 30
        errors = validate_digest_config(self._cfg(repeat_window_days=0))
        self.assertEqual(errors, [])

    def test_limit_must_be_positive(self):
        for bad in (0, -1):
            errors = validate_digest_config(self._cfg(limit=bad))
            self.assertTrue(any("digest.daily.limit" in e for e in errors), msg=f"limit={bad}")

    def test_negative_repeat_window_rejected(self):
        errors = validate_digest_config(self._cfg(repeat_window_days=-3))
        self.assertTrue(any("repeat_window_days" in e for e in errors))

    def test_pool_window_smaller_than_repeat_rejected(self):
        errors = validate_digest_config(self._cfg(repeat_window_days=30, pool_window_days=20))
        self.assertTrue(any("pool_window_days" in e for e in errors))

    def test_threshold_range(self):
        self.assertEqual(validate_digest_config({"relevance_threshold": 0}), [])
        errors = validate_digest_config({"relevance_threshold": 12})
        self.assertTrue(any("relevance_threshold" in e for e in errors))
        errors = validate_digest_config({"relevance_threshold": "abc"})
        self.assertTrue(any("relevance_threshold" in e for e in errors))

    def test_category_min_greater_than_max_rejected(self):
        cfg = self._cfg(category_limits={"mlip": {"min": 3, "max": 1}})
        errors = validate_digest_config(cfg)
        self.assertTrue(any("mlip" in e for e in errors))

    def test_category_min_sum_exceeds_limit_rejected(self):
        cfg = self._cfg(limit=3, category_limits={
            "mlip": {"min": 2, "max": 4}, "dft": {"min": 2, "max": 3}})
        errors = validate_digest_config(cfg)
        self.assertTrue(any("min 之和" in e for e in errors))

    def test_non_integer_rejected(self):
        errors = validate_digest_config(self._cfg(limit="many"))
        self.assertTrue(any("digest.daily.limit" in e for e in errors))

    def test_main_validate_config_integrates_digest(self):
        from main import validate_config
        base = {"database": {"path": ":memory:"},
                "llm": {"provider": "deepseek", "deepseek": {"api_key": "k"}}}
        validate_config(base)  # 无 digest 配置 = 合法
        bad = dict(base, digest={"daily": {"limit": 0}})
        with self.assertRaises(ValueError) as cm:
            validate_config(bad)
        self.assertIn("digest 配置无效", str(cm.exception))


class TestDigestConfigAdversarial(unittest.TestCase):
    """反例验收：配额合并、类型严格性、未知类别（规范 §4，2026-09-11 补）。"""

    def test_default_quota_merge_exceeds_limit(self):
        # 未配类别时也要按合并默认配额校验：默认 Σmin=4，limit=3 非法
        errors = validate_digest_config({"digest": {"daily": {"limit": 3}}})
        self.assertTrue(any("min 之和" in e for e in errors), msg=str(errors))

    def test_partial_override_merge_exceeds_limit(self):
        # 用户只覆盖 dft：合并后 Σmin = mlip2 + ai1 + dft5 = 8 > 7
        cfg = {"digest": {"daily": {"limit": 7,
                                    "category_limits": {"dft": {"min": 5, "max": 8}}}}}
        errors = validate_digest_config(cfg)
        self.assertTrue(any("min 之和" in e and "8" in e for e in errors), msg=str(errors))

    def test_legacy_quota_min_max_conflict(self):
        # 旧写法 {mlip: 1} → 合并默认后 min=2 > max=1，必须报错
        errors = validate_digest_config({"digest": {"daily": {"quotas": {"mlip": 1}}}})
        self.assertTrue(any("min(2) > max(1)" in e for e in errors), msg=str(errors))

    def test_non_integer_float_rejected(self):
        errors = validate_digest_config({"digest": {"daily": {"limit": 3.5}}})
        self.assertTrue(any("digest.daily.limit" in e for e in errors))
        errors = validate_digest_config({"digest": {"daily": {"repeat_window_days": 2.5}}})
        self.assertTrue(any("repeat_window_days" in e for e in errors))

    def test_integral_float_accepted(self):
        errors = validate_digest_config({"digest": {"daily": {"limit": 10.0}}})
        self.assertEqual(errors, [])

    def test_bool_rejected(self):
        # bool 是 int 子类：int(True)=1、int(False)=0 会被静默放行，必须显式拒绝
        errors = validate_digest_config({"digest": {"daily": {"limit": True}}})
        self.assertTrue(any("布尔" in e for e in errors), msg=str(errors))
        errors = validate_digest_config({"digest": {"daily": {"repeat_window_days": False}}})
        self.assertTrue(any("布尔" in e for e in errors), msg=str(errors))

    def test_threshold_bool_rejected(self):
        errors = validate_digest_config({"relevance_threshold": True})
        self.assertTrue(any("relevance_threshold" in e for e in errors))

    def test_category_limits_wrong_mapping_type(self):
        errors = validate_digest_config(
            {"digest": {"daily": {"category_limits": ["mlip", "dft"]}}})
        self.assertTrue(any("必须是映射" in e for e in errors), msg=str(errors))

    def test_category_value_wrong_type(self):
        errors = validate_digest_config(
            {"digest": {"daily": {"category_limits": {"mlip": "three"}}}})
        self.assertTrue(any("mlip" in e for e in errors), msg=str(errors))

    def test_unknown_category_rejected(self):
        errors = validate_digest_config(
            {"digest": {"daily": {"category_limits": {"typo_mlip": {"min": 1, "max": 2}}}}})
        self.assertTrue(any("未知类别" in e and "typo_mlip" in e for e in errors), msg=str(errors))

    def test_negative_quota_min_rejected(self):
        # 合并后非负检查：min=-1 必须拒绝（旧实现只查 min>max，负数漏过）
        errors = validate_digest_config(
            {"digest": {"daily": {"category_limits": {"mlip": {"min": -1, "max": 4}}}}})
        self.assertTrue(any("min(-1) < 0" in e for e in errors), msg=str(errors))

    def test_negative_quota_min_and_max_rejected(self):
        errors = validate_digest_config(
            {"digest": {"daily": {"category_limits": {"other": {"min": -2, "max": -1}}}}})
        self.assertTrue(any("min(-2) < 0" in e for e in errors), msg=str(errors))
        self.assertTrue(any("max(-1) < 0" in e for e in errors), msg=str(errors))

    def test_legacy_quota_float_rejected_not_truncated(self):
        # 旧标量同样走严格整数：3.5 不得被 int() 静默截断成 3
        errors = validate_digest_config({"digest": {"daily": {"quotas": {"mlip": 3.5}}}})
        self.assertTrue(any("mlip" in e and "3.5" in e for e in errors), msg=str(errors))

    def test_legacy_quota_infinity_rejected_without_overflow(self):
        # 无穷值：不得外溢异常（inf 传到 normalize_limits 的 int() 会抛
        # OverflowError），须转为错误列表
        errors = validate_digest_config({"digest": {"daily": {"quotas": {"mlip": float("inf")}}}})
        self.assertTrue(any("mlip" in e for e in errors), msg=str(errors))

    def test_falsy_category_limits_not_swallowed_to_defaults(self):
        # 键存在性优先于值真假：显式 false / [] 不得被 `or` 吞成默认配额
        for falsy in (False, []):
            errors = validate_digest_config({"digest": {"daily": {"category_limits": falsy}}})
            self.assertTrue(any("必须是映射" in e for e in errors), msg=repr(falsy))

    def test_daily_field_infinity_rejected(self):
        errors = validate_digest_config({"digest": {"daily": {"limit": float("inf")}}})
        self.assertTrue(any("digest.daily.limit" in e for e in errors), msg=str(errors))


class TestTieBreakDeterminism(unittest.TestCase):
    """平分决胜键：final desc → relevance desc → pub_date desc → id asc（规范 §3.5）。"""

    def _row(self, rid, final, rel, pub="2026-09-10"):
        return {"id": rid, "title": f"t{rid}", "pub_date": pub,
                "scores": {"final": final, "relevance": rel, "category": "other"}}

    def test_final_tie_broken_by_relevance(self):
        res = select_daily_top([self._row(1, 8.0, 7.0), self._row(2, 8.0, 9.0)], limit=2)
        self.assertEqual([r["id"] for r in res.selected], [2, 1])

    def test_full_tie_broken_by_pub_date_then_id(self):
        # 类别用 mlip（max=4 ≥ 3 篇），避免 other.max=2 干扰排序断言
        rows = [self._row(3, 8.0, 8.0, "2026-09-01"),
                self._row(1, 8.0, 8.0, "2026-09-05"),
                self._row(2, 8.0, 8.0, "2026-09-05")]
        for r in rows:
            r["scores"]["category"] = "mlip"
        res = select_daily_top(rows, limit=3)
        self.assertEqual([r["id"] for r in res.selected], [1, 2, 3])

    def test_missing_pub_date_ranks_last_among_equals(self):
        dated = self._row(1, 8.0, 8.0, "2026-09-05")
        undated = self._row(2, 8.0, 8.0, "")
        res = select_daily_top([undated, dated], limit=2)
        self.assertEqual([r["id"] for r in res.selected], [1, 2])

    def test_input_order_does_not_matter(self):
        import random
        rows = [self._row(i, 8.0, 8.0, "2026-09-05") for i in range(1, 9)]
        expect = [x["id"] for x in select_daily_top(list(rows), limit=8).selected]
        shuffled = list(rows)
        random.Random(42).shuffle(shuffled)
        got = [x["id"] for x in select_daily_top(shuffled, limit=8).selected]
        self.assertEqual(expect, got)


class TestBuilderPoolAndRepeatWindow(unittest.TestCase):
    """候选池时间窗与防重窗口的边界行为（规范 §3.2/§3.6）。"""

    NOW_DATE = "2026-09-10"

    def _db(self):
        from core.db import Database
        return Database(":memory:")

    def test_pool_window_excludes_old_articles(self):
        db = self._db()
        db.save_articles_batch([
            {"doi": "10.1/new", "title": "machine learning interatomic potential fresh",
             "pub_date": "2026-09-09", "abstract": "x"},
            {"doi": "10.1/old", "title": "machine learning interatomic potential ancient",
             "pub_date": "2026-01-01", "abstract": "y"},
        ])
        conn = db._memory_conn
        conn.execute("UPDATE articles SET relevance = 9.0, created_at = '2026-09-09 08:00:00'")
        conn.commit()
        config = {"digest": {"daily": {"limit": 10}}, "relevance_threshold": 5}
        result = build_daily_digest(db, config, date_str=self.NOW_DATE, dry_run=True)
        self.assertEqual(result["articles_above_threshold"], 1)  # 1 月的文章出池
        selected_ids = {r["id"] for r in result["selection"].selected}
        self.assertEqual(len(selected_ids), 1)

    def test_created_at_fallback_for_missing_pub_date(self):
        db = self._db()
        db.save_articles_batch([
            {"doi": "10.1/nodate", "title": "machine learning interatomic potential nodate",
             "abstract": "x"},
        ])
        conn = db._memory_conn
        conn.execute("UPDATE articles SET relevance = 9.0, created_at = '2026-01-01 08:00:00'")
        conn.commit()
        config = {"digest": {"daily": {"limit": 10}}, "relevance_threshold": 5}
        result = build_daily_digest(db, config, date_str=self.NOW_DATE, dry_run=True)
        # 无 pub_date → 回退 created_at（1 月）→ 出池
        self.assertEqual(result["articles_above_threshold"], 0)

    def test_future_pub_date_excluded_by_report_date_cap(self):
        # 候选池上限即报告日期：未来发表的论文（多为日期错误）不进池
        db = self._db()
        db.save_articles_batch([
            {"doi": "10.1/future", "title": "machine learning interatomic potential future",
             "pub_date": "2026-12-01", "abstract": "x"},
            {"doi": "10.1/ok", "title": "machine learning interatomic potential ok",
             "pub_date": "2026-09-08", "abstract": "y"},
        ])
        conn = db._memory_conn
        conn.execute("UPDATE articles SET relevance = 9.0, created_at = '2026-09-09 08:00:00'")
        conn.commit()
        config = {"digest": {"daily": {"limit": 10}}, "relevance_threshold": 5}
        result = build_daily_digest(db, config, date_str=self.NOW_DATE, dry_run=True)
        self.assertEqual(result["articles_above_threshold"], 1)
        selected_ids = {r["id"] for r in result["selection"].selected}
        ok_id = conn.execute("SELECT id FROM articles WHERE doi='10.1/ok'").fetchone()[0]
        self.assertEqual(selected_ids, {ok_id})

    def test_historical_replay_excludes_later_ingested_articles(self):
        # 历史重放（date_str=09-05）：入库时间晚于报告日期的文章不进池；
        # 以入库日（09-11）为报告日期时同一篇文章正常进池
        db = self._db()
        db.save_articles_batch([
            {"doi": "10.1/late", "title": "machine learning interatomic potential late",
             "abstract": "x"},  # 无 pub_date → 回退 created_at
        ])
        conn = db._memory_conn
        conn.execute("UPDATE articles SET relevance = 9.0, created_at = '2026-09-11 08:00:00'")
        conn.commit()
        config = {"digest": {"daily": {"limit": 10}}, "relevance_threshold": 5}
        replay = build_daily_digest(db, config, date_str="2026-09-05", dry_run=True)
        self.assertEqual(replay["articles_above_threshold"], 0)
        today = build_daily_digest(db, config, date_str="2026-09-11", dry_run=True)
        self.assertEqual(today["articles_above_threshold"], 1)

    def test_pub_early_but_ingested_after_report_date(self):
        # 发表日期早于报告日、入库日期晚于报告日：created_at 独立受限，
        # 历史重放（09-10）排除；报告日期推到入库日（09-20）之后才收录
        db = self._db()
        db.save_articles_batch([
            {"doi": "10.1/late2", "title": "machine learning interatomic potential late2",
             "pub_date": "2026-09-01", "abstract": "x"},
        ])
        conn = db._memory_conn
        conn.execute("UPDATE articles SET relevance = 9.0, created_at = '2026-09-15 10:00:00'")
        conn.commit()
        config = {"digest": {"daily": {"limit": 10}}, "relevance_threshold": 5}
        replay = build_daily_digest(db, config, date_str="2026-09-10", dry_run=True)
        self.assertEqual(replay["articles_above_threshold"], 0)
        later = build_daily_digest(db, config, date_str="2026-09-20", dry_run=True)
        self.assertEqual(later["articles_above_threshold"], 1)

    def test_sql_truncation_deterministic_under_ties(self):
        # LIMIT 截断前按 (pool_date desc, relevance desc, id asc) 排序：
        # 同日期同分数的文章，保留的恰好是 id 最小的 POOL_HARD_LIMIT 篇
        import unittest.mock
        from digest import builder as builder_mod
        db = self._db()
        db.save_articles_batch([
            {"doi": f"10.1/t{i}", "title": f"machine learning interatomic potential t{i}",
             "pub_date": "2026-09-08", "abstract": "x"}
            for i in range(1, 5)
        ])
        conn = db._memory_conn
        conn.execute("UPDATE articles SET relevance = 9.0, created_at = '2026-09-08 08:00:00'")
        conn.commit()
        config = {"digest": {"daily": {"limit": 4}}, "relevance_threshold": 5}
        with unittest.mock.patch.object(builder_mod, "POOL_HARD_LIMIT", 2):
            result = build_daily_digest(db, config, date_str=self.NOW_DATE, dry_run=True)
        kept = sorted(r["id"] for r in result["selection"].selected)
        self.assertEqual(kept, [1, 2])  # id 唯一决胜，截断结果确定

    def test_builder_raises_on_invalid_config(self):
        db = self._db()
        with self.assertRaises(ValueError) as cm:
            build_daily_digest(db, {"digest": {"daily": {"limit": 0}}},
                               date_str=self.NOW_DATE, dry_run=True)
        self.assertIn("digest 配置无效", str(cm.exception))

    def test_repeat_window_zero_disables_exclusion(self):
        db = self._db()
        db.save_articles_batch([
            {"doi": "10.1/dup", "title": "machine learning interatomic potential dup",
             "pub_date": "2026-09-04", "abstract": "x"},  # 早于 09-05 报告日，可进 09-05 池
        ])
        conn = db._memory_conn
        conn.execute("UPDATE articles SET relevance = 9.0, created_at = '2026-09-04 08:00:00'")
        conn.commit()
        config = {"digest": {"daily": {"limit": 10}}, "relevance_threshold": 5}
        # 9-5 先"发布"一次
        build_daily_digest(db, config, date_str="2026-09-05", dry_run=False)
        # 默认 30 天防重 → 被排除
        r30 = build_daily_digest(db, config, date_str=self.NOW_DATE, dry_run=True)
        self.assertEqual(r30["selection"].stats.get("excluded_repeat"), 1)
        # repeat_window_days=0 → 关闭防重，文章仍可入选（0 不得被默认值吞掉）
        config0 = {"digest": {"daily": {"limit": 10, "repeat_window_days": 0}},
                   "relevance_threshold": 5}
        r0 = build_daily_digest(db, config0, date_str=self.NOW_DATE, dry_run=True)
        self.assertEqual(r0["selection"].stats.get("excluded_repeat"), 0)
        self.assertEqual(len(r0["selection"].selected), 1)

    def test_builder_rejects_default_quota_merge_overflow(self):
        # limit=3 且未配类别：合并默认配额后 Σmin=4 > 3，必须在构建时即拒绝
        db = self._db()
        with self.assertRaises(ValueError) as cm:
            build_daily_digest(db, {"digest": {"daily": {"limit": 3}}},
                               date_str=self.NOW_DATE, dry_run=True)
        self.assertIn("min 之和", str(cm.exception))


class TestCollectDailyConfig(unittest.TestCase):
    """统一归一化出口：builder 与校验器共用，显式 0 必须保留。"""

    def test_explicit_zero_preserved(self):
        values, errors = collect_daily_config(
            {"digest": {"daily": {"repeat_window_days": 0}}})
        self.assertEqual(errors, [])
        self.assertEqual(values["repeat_window_days"], 0)

    def test_defaults_when_missing(self):
        values, errors = collect_daily_config({})
        self.assertEqual(errors, [])
        self.assertEqual(values["limit"], 10)
        self.assertEqual(values["repeat_window_days"], 30)
        self.assertEqual(values["pool_window_days"], 60)
        self.assertEqual(values["min_score"], 5.0)

    def test_legacy_quotas_normalized(self):
        values, errors = collect_daily_config(
            {"digest": {"daily": {"quotas": {"mlip": 3}}}})
        self.assertEqual(errors, [])
        self.assertEqual(values["category_limits"]["mlip"]["max"], 3)

    def test_validator_matches_collector(self):
        # validate 是 collect 的薄封装：同一配置两条路径结论一致
        cfg = {"digest": {"daily": {"limit": 0}}}
        self.assertEqual(validate_digest_config(cfg), collect_daily_config(cfg)[1])


class TestDryRunFileSemantics(unittest.TestCase):
    """CLI dry-run 唯一落盘产物：审阅快照，同日覆盖；失败不影响预览（规范 §5）。"""

    def test_write_and_same_day_overwrite(self):
        import main as main_mod
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        cfg = {"output": {"output_dir": tmp}}  # 绝对路径原样使用
        p1 = main_mod._write_dry_run_report(cfg, "2026-09-10", "v1")
        self.assertEqual(Path(p1).name, "digest-dry-run-2026-09-10.txt")
        self.assertEqual(Path(p1).read_text(encoding="utf-8"), "v1")
        p2 = main_mod._write_dry_run_report(cfg, "2026-09-10", "v2")
        self.assertEqual(p1, p2)  # 同日同路径
        self.assertEqual(Path(p2).read_text(encoding="utf-8"), "v2")  # 覆盖而非追加

    def test_write_failure_returns_none(self):
        import main as main_mod
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        blocker = Path(tmp) / "blocker"
        blocker.write_text("x", encoding="utf-8")  # 用文件冒充目录 → mkdir 失败
        cfg = {"output": {"output_dir": str(blocker)}}
        self.assertIsNone(main_mod._write_dry_run_report(cfg, "2026-09-10", "v1"))


if __name__ == "__main__":
    unittest.main()
