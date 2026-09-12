"""回归：审查发现的六个 P1/P2 问题，覆盖真实触发条件（临时库/mock，只读生产无接触）。"""
import hashlib
from types import SimpleNamespace
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core.db import Database
from core.notifier import EmailDeliveryUnknown, Notifier


class DeleteRollbackTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.db = Database(str(Path(tmp.name) / "t.db"))
        self.addCleanup(self.db.close)
        ids = self.db.save_articles_batch([
            {"doi": "10.test/rb", "title": "Rollback case", "abstract": "x"}])
        self.aid = ids[0]
        self.assertTrue(self.db.update_article_note(self.aid, "keep my note"))
        self.db.update_article_fields(self.aid, relevance=7.0)
        self.conn = self.db._conn()
        self.conn.execute(
            "CREATE TRIGGER block_article_delete BEFORE DELETE ON articles "
            "BEGIN SELECT RAISE(ABORT, 'mock article delete failure'); END")
        self.conn.commit()

    def test_failed_delete_keeps_related_data_and_allows_later_saves(self):
        with self.assertRaises(sqlite3.Error):
            self.db.delete_articles_by_ids([self.aid])
        # 后续正常保存不得提交残留删除
        self.db.update_article_fields(self.aid, relevance=8.0)
        row = self.conn.execute(
            "SELECT a.note, (SELECT COUNT(*) FROM chat_messages WHERE article_id=a.id) "
            "FROM articles a WHERE a.id=?", (self.aid,)).fetchone()
        self.assertEqual(row[0], "keep my note")
        cur = self.conn.execute(
            "SELECT (SELECT COUNT(*) FROM articles)=1 AND (SELECT note FROM articles WHERE id=?)='keep my note'",
            (self.aid,))
        self.assertEqual(cur.fetchone()[0], 1)

    def test_delete_recovers_after_trigger_removed(self):
        with self.assertRaises(sqlite3.Error):
            self.db.delete_articles_by_ids([self.aid])
        self.conn.execute("DROP TRIGGER block_article_delete")
        self.conn.commit()
        self.assertEqual(self.db.delete_articles_by_ids([self.aid]), 1)


class EmailDeliveryBoundaryTests(unittest.TestCase):
    """SMTP 边界：DATA 接受后的异常必须归为 unknown，不得普通重发。"""

    DATE = "2026-09-10"

    def _notifier(self):
        cfg = {"smtp_server": "smtp.test", "smtp_port": 465, "username": "u@test",
               "password": "pw", "recipients": ["a@test", "b@test"]}
        return Notifier({"output": {"output_dir": tempfile.mkdtemp(), "email": cfg}}), cfg

    def _patch_transport(self, *, quit_error=None, refused=None, sendmail_error=None):
        server = FakeSMTP(quit_error=quit_error, refused=refused, sendmail_error=sendmail_error)
        patcher = patch("core.notifier.smtplib.SMTP_SSL", return_value=server)
        patcher.start(); self.addCleanup(patcher.stop)
        return server

    def test_quit_failure_after_accept_is_unknown_and_not_retried(self):
        from test_digest_service import FakeNotifier, _make_config, _seed_articles, DATE
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        db = Database(str(Path(tmp.name) / "t.db")); self.addCleanup(db.close)
        from digest.service import DigestService
        service_notifier = FakeNotifier(tmp.name)
        # 模拟真实 _send_email 在 DATA 接受后抛 EmailDeliveryUnknown，复用真实 service 分类
        def raise_unknown(**kwargs):
            raise EmailDeliveryUnknown("SMTP QUIT 失败，邮件可能已送达")
        with patch.object(service_notifier, "send_digest_files", side_effect=raise_unknown):
            service = DigestService(db, _make_config(), service_notifier)
            _seed_articles(db, n=3)
            result = service.publish_digest(DATE, channels=["email"])
        self.assertEqual(result["deliveries"][0]["status"], "unknown")
        self.assertEqual(result["overall_status"], "partial")
        # 普通 retry 不得重发 unknown 渠道
        with patch.object(service_notifier, "send_digest_files", side_effect=AssertionError("resend!")):
            retry = service.retry_digest_send(result["version_id"], channels=["email"])
        self.assertNotIn(retry["deliveries"][0].get("status"), ("sent", "failed"))

    def test_partial_refusal_is_unknown_with_recipient_detail(self):
        import smtplib
        notifier, cfg = self._notifier()
        server = FakeSMTP(refused={"b@test": (450, "4.2.0 mailbox busy")})
        with patch("core.notifier.smtplib.SMTP_SSL", return_value=server):
            with self.assertRaises(EmailDeliveryUnknown) as caught:
                notifier._send_email("<p>hi</p>", self.DATE, cfg, is_html=True)
        self.assertIn("b@test", str(caught.exception))
        self.assertIn("a@test", str(caught.exception))

    def test_full_refusal_is_plain_failure(self):
        import smtplib
        notifier, cfg = self._notifier()
        refused = {r: (450, "busy") for r in cfg["recipients"]}
        server = FakeSMTP(refused=refused)
        with patch("core.notifier.smtplib.SMTP_SSL", return_value=server):
            with self.assertRaises(smtplib.SMTPRecipientsRefused):
                notifier._send_email("<p>hi</p>", self.DATE, cfg, is_html=True)


class FakeSMTP:
    def __init__(self, *, quit_error=None, refused=None, sendmail_error=None):
        self.quit_error = quit_error
        self.refused = refused
        self.sendmail_error = sendmail_error
        self.quit_calls = 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.quit_calls += 1
        return False

    def login(self, user, password):
        pass

    def sendmail(self, from_addr, to_addrs, msg):
        if self.sendmail_error:
            raise self.sendmail_error
        return self.refused

    def quit(self):
        self.quit_calls += 1
        if self.quit_error:
            raise self.quit_error


class HtmlFulltextBoundaryTests(unittest.TestCase):
    """摘要选择器命中的内容不得冒充全文证据等级。"""

    def _html(self, body: str) -> str:
        return f"<html><body>{''.join(f'<p>{p}</p>' for p in body.split('|'))}</body></html>"

    def test_abstract_selector_hit_stays_abstract_even_if_long(self):
        from fetchers.html_fetcher import _extract_text
        html = self._html("摘要内容，足够长，用于测试。" * 120)  # 远超全文下限
        text, is_fulltext = _extract_text(html, {"abstract": ["p"], "fulltext": []})
        self.assertGreater(len(text), 800)
        self.assertFalse(is_fulltext)

    def test_fulltext_selector_and_generic_container_stay_fulltext(self):
        from fetchers.html_fetcher import _extract_text
        body = "正文句子内容，足够长。"
        paragraphs = "|".join([body] * 120)  # 120 段（全文选择器要求 ≥3 段）
        html = self._html(paragraphs)
        text, is_fulltext = _extract_text(html, {"fulltext": ["p"], "abstract": ["p"]})
        self.assertTrue(is_fulltext)
        text2, full2 = _extract_text(html, {})
        self.assertTrue(full2)


if __name__ == "__main__":
    unittest.main()


class TrackingCursorTests(unittest.TestCase):
    """追踪游标：失败/未入库不推进，入库成功才推进。"""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.db = Database(str(Path(tmp.name) / "t.db"))
        self.addCleanup(self.db.close)
        ids = self.db.save_articles_batch([
            {"doi": "10.test/seed", "title": "Seed paper"}])
        conn = self.db._conn()
        conn.execute("INSERT INTO watched_seeds (article_id, active, last_checked_at) VALUES (?, 1, NULL)",
                     (ids[0],))
        conn.commit()

    def _tracking_meta(self):
        import importlib
        from core.tracking import collect_tracking_articles
        with patch("integrations.openalex.get_citing_works") as citing, \
             patch("integrations.openalex.get_author_recent_works", return_value=[]):
            return collect_tracking_articles({"enabled": True, "citation_check_days": 3, "unpaywall_email": "t@x"}, self.db)

    def test_openalex_failure_keeps_cursor(self):
        with patch("integrations.openalex.get_citing_works",
                   side_effect=RuntimeError("OpenAlex 请求重试耗尽")):
            from core.tracking import collect_tracking_articles
            articles, meta = collect_tracking_articles(
                {"enabled": True, "citation_check_days": 3, "unpaywall_email": "t@x"}, self.db)
        self.assertEqual(articles, [])
        row = self.db._conn().execute(
            "SELECT last_checked_at FROM watched_seeds").fetchone()
        self.assertIsNone(row[0])  # 失败不得推进游标
        self.assertEqual(meta.get("seed_windows", {}), {})

    def test_cursor_advances_only_after_articles_persisted(self):
        with patch("integrations.openalex.get_citing_works") as citing:
            citing.return_value = [{"doi": "10.test/new-cite", "title": "Citing work",
                                    "url": "", "abstract": "", "authors": []}]
            from core.tracking import collect_tracking_articles, mark_tracking_cursor
            articles, meta = collect_tracking_articles(
                {"enabled": True, "citation_check_days": 3, "unpaywall_email": "t@x"}, self.db)
        self.assertEqual(len(articles), 1)
        row = self.db._conn().execute(
            "SELECT last_checked_at FROM watched_seeds").fetchone()
        self.assertIsNone(row[0])  # 采集成功但尚未入库：游标仍未推进
        # 模拟入库成功后推进
        inserted = self.db.save_articles_batch(articles)
        mark_tracking_cursor(self.db, meta)
        row = self.db._conn().execute(
            "SELECT last_checked_at FROM watched_seeds").fetchone()
        self.assertIsNotNone(row[0])
        self.assertIsNotNone(inserted[0])


class AnalysisPromptStrategyTests(unittest.TestCase):
    """解读策略：摘要路径只做中文翻译；全文路径保持深度解读提示词。"""

    def _config(self):
        return {"llm": {"provider": "fake", "fake": {"api_key": "k", "base_url": "https://x",
                                                    "model": "m", "max_tokens": 8192}}}

    def _analyzer_capture(self):
        from core.analyzer import LLMAnalyzer
        analyzer = LLMAnalyzer(self._config())
        from types import SimpleNamespace
        captured = {}
        def fake_create(**kwargs):
            captured.update(kwargs)
            return _fake_response()
        analyzer.client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=fake_create)))
        return analyzer, captured

    def test_abstract_prompt_is_translation_only(self):
        analyzer, captured = self._analyzer_capture()
        result = analyzer.analyze_article({"title": "T", "journal": "J", "authors": ["A"],
                                           "doi": "10.test/x", "evidence_level": "ABSTRACT_ONLY",
                                           "abstract": "Original abstract."})
        self.assertTrue(result["success"], result.get("error"))
        prompt = captured["messages"][0]["content"]
        for required in ("翻译为中文", "不增不减", "忠实原文"):
            self.assertIn(required, prompt)
        for banned in ("方法动机", "速记版 Pipeline", "无此信息", "一句话核心思想"):
            self.assertNotIn(banned, prompt)

    def test_fulltext_prompt_keeps_deep_analysis(self):
        analyzer, captured = self._analyzer_capture()
        result = analyzer.analyze_article({"title": "T", "journal": "J", "authors": ["A"],
                                           "doi": "10.test/x", "evidence_level": "FULLTEXT",
                                           "abstract": "Abstract.", "fulltext_text": "Full text " * 400})
        self.assertTrue(result["success"])
        prompt = captured["messages"][0]["content"]
        for kept in ("方法设计", "实验表现", "一句话核心思想"):
            self.assertIn(kept, prompt)
        self.assertEqual(captured["max_tokens"], 8192)

    def test_abstract_translation_token_budget(self):
        analyzer, captured = self._analyzer_capture()
        analyzer.analyze_article({"title": "T", "journal": "J", "authors": ["A"],
                                  "doi": "10.test/x", "evidence_level": "ABSTRACT_ONLY",
                                  "abstract": "Abstract."})
        self.assertEqual(captured["max_tokens"], 2048)


def _fake_response():
    from types import SimpleNamespace
    choice = SimpleNamespace(message=SimpleNamespace(content="二维（2D）材料的化学气相沉积（CVD）经历了一个复杂的非平衡高温表面反应级联过程，这对原子尺度的原位实验探测与理论模拟都是重大挑战。研究人员开发了化学感知的主动学习蒸馏（Chem-ALD）框架来构建机器学习力场，并以接近量子化学的精度揭示了由多过渡态机制主导的普适非经典成核路径。", reasoning_content=None),
                             finish_reason="stop")
    return SimpleNamespace(choices=[choice], usage=None)


class TranslationQualityGateTests(unittest.TestCase):
    """翻译输出质量门槛：推理草稿/英文回显不得入库，失败触发重试。"""

    def _analyzer(self):
        from core.analyzer import LLMAnalyzer
        analyzer = LLMAnalyzer({"llm": {"provider": "fake", "fake": {
            "api_key": "k", "base_url": "https://x", "model": "m"}}})
        return analyzer

    def _set_output(self, analyzer, content):
        from types import SimpleNamespace
        def create(**kwargs):
            choice = SimpleNamespace(message=SimpleNamespace(
                content=content, reasoning_content=None), finish_reason="stop")
            return SimpleNamespace(choices=[choice], usage=None)
        analyzer.client = SimpleNamespace(chat=SimpleNamespace(
            completions=SimpleNamespace(create=create)))

    def test_deliberation_riddled_translation_fails_gate(self):
        analyzer = self._analyzer()
        dirty = ("我们需要回答用户。必须翻译摘要为中文。要求只输出译文。\n"
                 "原文： Abstract The CVD of 2D materials proceeds through reactions.\n"
                 "- chemical vapor deposition (CVD): 化学气相沉积，可保留 CVD。\n"
                 "可以译为\u201c通用多过渡态机制\u201d。")
        self._set_output(analyzer, dirty)
        result = analyzer.analyze_article({
            "title": "T", "journal": "J", "authors": ["A"], "doi": "10.t/q",
            "evidence_level": "ABSTRACT_ONLY", "abstract": "Abstract text."})
        self.assertFalse(result["success"])
        self.assertIn("translation_quality_failed", result["error"])

    def test_clean_translation_passes_gate(self):
        analyzer = self._analyzer()
        good = "二维（2D）材料的化学气相沉积（CVD）经历了一个复杂的非平衡高温表面反应级联过程。" * 3
        self._set_output(analyzer, good)
        result = analyzer.analyze_article({
            "title": "T", "journal": "J", "authors": ["A"], "doi": "10.t/q",
            "evidence_level": "ABSTRACT_ONLY", "abstract": "Abstract text."})
        self.assertTrue(result["success"])
        self.assertEqual(result["analysis"], good)

    def test_strip_helper_extracts_clean_translation(self):
        analyzer = self._analyzer()
        dirty = ("我们需要回答用户。必须翻译摘要为中文。\n"
                 "原文： Abstract Fast lithium transport across the solid-state electrolyte.\n"
                 "需要准确翻译。专业术语： solid-state electrolyte (SSE) = 固态电解质。\n"
                 "快速锂传输跨过 solid-state electrolyte (SSE)（固态电解质）/lithium metal anode（锂金属负极）的界面。")
        cleaned = analyzer._strip_model_deliberation(dirty, is_translation=True)
        self.assertIn("快速锂传输", cleaned)
        self.assertNotIn("我们需要", cleaned)
