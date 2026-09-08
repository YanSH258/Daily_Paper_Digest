"""流水线端到端回归：分阶段状态、失败重试、全文/摘要分离、日报防覆盖。

抓取器 / LLM 分析器 / 推送全部使用进程内模拟，不联网、不调用真实模型。
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from fetchers.models import FetchResult, FetchStatus, BestFormat  # noqa: E402
from core.analyzer import LLMQuotaExhaustedError, LLMTimeoutError  # noqa: E402


ARTICLES = [
    {"doi": "10.1/a", "title": "Paper A MLIP", "journal": "J", "url": "https://x/a",
     "abstract": "abstract of A", "authors": ["X"], "pub_date": "2026-09-08", "publisher": "ACS"},
    {"doi": "10.1/b", "title": "Paper B DFT", "journal": "J", "url": "https://x/b",
     "abstract": "abstract of B", "authors": ["Y"], "pub_date": "2026-09-08", "publisher": "ACS"},
    {"doi": "10.1/c", "title": "Paper C econ", "journal": "J", "url": "https://x/c",
     "abstract": "abstract of C", "authors": ["Z"], "pub_date": "2026-09-08", "publisher": "DEFAULT"},
    {"doi": "10.1/b", "title": "Paper B DFT", "journal": "J2", "url": "https://y/b",
     "abstract": "dup", "authors": ["Y"], "pub_date": "2026-09-08", "publisher": "ACS"},
]


class FakeFetcher:
    def __init__(self, config):
        pass

    def fetch_all(self):
        return [dict(a) for a in ARTICLES]

    def fetch_fulltext_batch(self, arts):
        return [
            FetchResult(text="FULL " + "x" * 1000,
                        best_available_format=BestFormat.HTML_FULLTEXT,
                        fetch_status=FetchStatus.SUCCESS, source_url="https://oa/a")
            if a["doi"] == "10.1/a" else
            FetchResult(fetch_status=FetchStatus.HTML_FETCH_FAIL, error_code="ALL_FAILED")
            for a in arts
        ]


class FakeAnalyzer:
    model = "fake-model"
    score_calls = {}    # 类级计数：跨 run_once 调用共享，模拟真实调用历史
    analyze_calls = {}

    def __init__(self, config):
        pass

    def filter_relevance(self, a):
        n = FakeAnalyzer.score_calls.get(a["doi"], 0) + 1
        FakeAnalyzer.score_calls[a["doi"]] = n
        if a["doi"] == "10.1/a":
            return {"score": 8.5, "reason": "命中MLIP", "matched_topics": ["MLIP"],
                    "model": "fake-model", "basis": "abstract"}
        if a["doi"] == "10.1/b":
            if n < 2:
                raise LLMTimeoutError("timeout")
            return {"score": 7.0, "reason": "DFT相关", "matched_topics": [],
                    "model": "fake-model", "basis": "abstract"}
        return {"score": 2.0, "reason": "无关", "matched_topics": [],
                "model": "fake-model", "basis": "abstract"}

    def analyze_article(self, a):
        n = FakeAnalyzer.analyze_calls.get(a["doi"], 0) + 1
        FakeAnalyzer.analyze_calls[a["doi"]] = n
        if a["doi"] == "10.1/a":
            # 全文解读必须来自独立字段，且摘要保持原文
            assert a.get("fulltext_text"), "A 必须携带独立全文"
            assert a["abstract"] == "abstract of A", "摘要被覆盖"
        if a["doi"] == "10.1/b" and n < 2:
            raise LLMQuotaExhaustedError("quota")
        return {"success": True, "analysis": f"GOOD {a['doi']}",
                "evidence_level": "FULLTEXT", "error": ""}


class FakeNotifier:
    def __init__(self, config):
        self.output_dir = Path(config["output"]["output_dir"])

    def notify(self, rel, all_articles=None, date_str=None):
        p = self.output_dir / f"{date_str}.md"
        p.write_text("REPORT", encoding="utf-8")
        return str(p), {"email": True, "feishu": None}


class TestPipelineRetry(unittest.TestCase):
    def setUp(self):
        FakeAnalyzer.score_calls = {}
        FakeAnalyzer.analyze_calls = {}
        self.tmp = tempfile.mkdtemp()
        out_dir = os.path.join(self.tmp, "out")
        os.makedirs(out_dir)
        self.cfg = {
            "database": {"path": os.path.join(self.tmp, "pipe.db")},
            "llm": {"provider": "fake", "fake": {"api_key": "k"}},
            "output": {"output_dir": out_dir},
            "relevance_threshold": 5,
            "fetcher": {"use_fulltext": True, "retry_window_days": 7},
        }
        import main as M
        self.M = M
        M.JournalFetcher = FakeFetcher
        M.LLMAnalyzer = FakeAnalyzer
        M.Notifier = FakeNotifier

    def test_full_retry_cycle(self):
        from core.db import Database

        # RUN1: 批次去重 + B 评分失败保留 + A 全文/摘要分离 + A 分析成功
        s1 = self.M.run_once(self.cfg, date_str="2026-09-08")
        self.assertEqual(s1["batch_duplicates"], 1)
        self.assertEqual(s1["scored_failed"], 1)
        self.assertEqual(s1["analyzed_ok"], 1)
        self.assertEqual(s1["fulltext_fetched"], 1)

        db = Database(self.cfg["database"]["path"])
        rows = {r["doi"]: r for r in db.get_articles_by_ids([1, 2, 3])}
        a, b, c = rows["10.1/a"], rows["10.1/b"], rows["10.1/c"]
        self.assertEqual((a["processed"], a["analysis_status"]), (1, "ok"))
        self.assertTrue(a["fulltext_text"].startswith("FULL"))
        self.assertEqual(a["abstract"], "abstract of A")            # 摘要保持原文
        self.assertEqual(a["evidence_level"], "FULLTEXT")
        self.assertEqual(a["fulltext_url"], "https://oa/a")
        self.assertEqual(a["relevance_reason"], "命中MLIP")          # 可解释评分落库
        self.assertEqual((b["score_status"], b["analysis_status"]), ("failed", ""))
        self.assertEqual((c["processed"], c["score_status"]), (1, "ok"))
        runs = db.list_task_runs()
        self.assertEqual(runs[0]["status"], "partial")  # 有失败阶段 → partial
        db.close()

        # RUN2: B 评分重试成功 → 进入候选 → 分析失败（不写伪文本）
        s2 = self.M.run_once(self.cfg, date_str="2026-09-08")
        self.assertEqual(s2["retried_score"], 1)
        self.assertEqual(s2["scored_ok"], 1)
        self.assertEqual(s2["analyzed_failed"], 1)
        db = Database(self.cfg["database"]["path"])
        b = db.get_articles_by_ids([2])[0]
        self.assertEqual(b["score_status"], "ok")
        self.assertEqual(b["analysis_status"], "failed")
        self.assertIsNone(b["analysis"])
        db.close()

        # RUN3: B 分析重试成功；已成功阶段不重复调用
        s3 = self.M.run_once(self.cfg, date_str="2026-09-08")
        self.assertEqual(s3["retried_analysis"], 1)
        self.assertEqual(s3["analyzed_ok"], 1)
        self.assertEqual(s3["analyzed_failed"], 0)

        # RUN4: 全部完成 → 无新增且当日报告存在 → 跳过生成与推送
        s4 = self.M.run_once(self.cfg, date_str="2026-09-08")
        self.assertTrue(s4["report_skipped_reason"])

        db = Database(self.cfg["database"]["path"])
        self.assertEqual(db.get_articles_by_ids([1])[0]["analysis_status"], "ok")
        self.assertEqual(FakeAnalyzer.analyze_calls.get("10.1/a"), 1, "A 不应重复分析")
        self.assertEqual(FakeAnalyzer.score_calls.get("10.1/c"), 1, "低分文章不应重复评分")
        rep = db.get_report("2026-09-08")
        self.assertEqual(rep["push_results"], '{"email": true, "feishu": null}')
        db.close()


if __name__ == "__main__":
    unittest.main()
