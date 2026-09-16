"""旧补发入口与周报兼容：真实临时库/文件，渠道传输全部模拟。"""
import hashlib
import json
import re
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import main
from core.db import Database
from core.notifier import Notifier
from digest.service import DigestError, DigestService


DATE = "2026-09-11"


class ReportCompatTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.config = {
            "database": {"path": str(self.root / "compat.db")},
            "output": {"output_dir": str(self.root / "reports"),
                       "email": {"enabled": True}, "feishu": {"enabled": True}},
            "relevance_threshold": 5,
        }
        self.db = Database(self.config["database"]["path"])
        self.addCleanup(self.db.close)
        self.notifier = Notifier(self.config)
        self.service = DigestService(self.db, self.config, notifier=self.notifier)
        self.email = self.start_patch(patch.object(Notifier, "_send_email"))
        self.real_feishu_text = Notifier._send_feishu_text
        self.feishu = self.start_patch(patch.object(Notifier, "_send_feishu_text"))
        self.opened = []

        def open_db(path):
            self.assertEqual(path, self.config["database"]["path"])
            db = Database(path)
            self.addCleanup(db.close)
            close = self.start_patch(patch.object(db, "close", wraps=db.close))
            self.opened.append(close)
            return db

        self.start_patch(patch.object(main, "Database", side_effect=open_db))

    def start_patch(self, patcher):
        value = patcher.start()
        self.addCleanup(patcher.stop)
        return value

    def version(self, *, render=True, status="published"):
        saved = self.db.save_digest_version(
            digest_date=DATE, status=status,
            stats_json=json.dumps({"candidates": 7}),
            items=[{"rank": 1, "scores_json": "{}", "snapshot_json": json.dumps({
                "title": "Fixed snapshot paper", "abstract": "Snapshot abstract",
                "journal": "Test journal", "relevance": 8,
            })}],
        )
        vid = saved["version_id"]
        if render:
            self.service.render_digest(vid)
        return vid

    def set_sends(self, vid, **states):
        self.db.init_digest_sends(vid, list(states))
        for channel, status in states.items():
            if status != "pending":
                token = f"fixture-{vid}-{channel}"
                self.db.claim_digest_send(vid, channel, token)
                if status != "sending":
                    self.db.finish_digest_send(vid, channel, token, status)

    def legacy_files(self):
        md = self.notifier.output_dir / f"{DATE}.md"
        md.write_text("LEGACY REPORT", encoding="utf-8")
        return md

    def test_latest_version_retries_only_failed_channel_and_updates_legacy_pointer(self):
        legacy = self.legacy_files()
        old = self.version()
        self.set_sends(old, email="failed", feishu="failed")
        latest = self.version()
        self.set_sends(latest, email="sent", feishu="failed")
        self.db.save_report(DATE, str(legacy), 99, 99, {"email": False})
        with patch.object(Notifier, "resend", side_effect=AssertionError("legacy fallback")), \
                patch.object(DigestService, "_compute_selection",
                             side_effect=AssertionError("selection changed")), \
                patch.object(DigestService, "retry_digest_send", autospec=True,
                             side_effect=DigestService.retry_digest_send) as retry:
            result = main.push_only(self.config, DATE)
        self.assertEqual(retry.call_args.args[1], latest)
        self.assertEqual((result["date"], result["version_id"], result["version"]),
                         (DATE, latest, 2))
        self.assertEqual(result["push_results"], {"email": True, "feishu": True})
        self.assertIn("/v2/report.md", result["report_path"])
        self.assertIn("Fixed snapshot paper", self.feishu.call_args.args[0])
        self.assertNotIn("LEGACY REPORT", self.feishu.call_args.args[0])
        self.email.assert_not_called()
        self.feishu.assert_called_once()
        self.assertEqual([s["status"] for s in self.db.list_digest_sends(old)],
                         ["failed", "failed"])
        rep = self.db.get_report(DATE)
        self.assertEqual(rep["file_path"], result["report_path"])
        self.assertEqual((rep["total_found"], rep["total_pushed"]), (7, 1))
        self.assertEqual(json.loads(rep["push_results"]), result["push_results"])
        self.assertEqual(len(self.db.list_digest_versions()), 2)
        self.opened[-1].assert_called_once()

    def test_sent_unknown_and_inflight_are_not_resent(self):
        for status in ("unknown", "sending"):
            with self.subTest(status=status):
                vid = self.version()
                self.set_sends(vid, email="sent", feishu=status)
                before = self.db.list_digest_sends(vid)
                result = main.push_only(self.config, DATE)
                self.assertEqual(result["push_results"], {"email": True, "feishu": None})
                self.assertEqual(self.db.list_digest_sends(vid), before)
                self.assertIn("SKIPPED", [e["code"] for e in result["errors"]])
        self.email.assert_not_called()
        self.feishu.assert_not_called()

    def test_pending_timeout_becomes_unknown_and_next_retry_skips(self):
        vid = self.version()
        self.set_sends(vid, email="pending")
        self.email.side_effect = TimeoutError("simulated timeout")
        first = main.push_only(self.config, DATE)
        second = main.push_only(self.config, DATE)
        self.email.assert_called_once()
        self.assertEqual(first["push_results"], {"email": None})
        self.assertEqual(second["push_results"], {"email": None})
        self.assertEqual(self.db.list_digest_sends(vid)[0]["status"], "unknown")

    def test_failed_delivery_preserves_failed_status(self):
        vid = self.version()
        self.set_sends(vid, email="failed")
        self.email.side_effect = RuntimeError("simulated delivery failure")
        result = main.push_only(self.config, DATE)
        self.assertEqual(result["push_results"], {"email": False})
        self.assertEqual(result["overall_status"], "partial")
        self.assertEqual(self.db.list_digest_sends(vid)[0]["status"], "failed")

    def test_version_without_send_records_does_not_fallback(self):
        self.version()
        self.legacy_files()
        with patch.object(Notifier, "resend", side_effect=AssertionError("legacy fallback")):
            result = main.push_only(self.config, DATE)
        self.assertEqual(result["push_results"], {})
        self.email.assert_not_called()
        self.feishu.assert_not_called()

    def test_missing_version_artifacts_do_not_fallback_to_legacy(self):
        vid = self.version(render=False)
        self.set_sends(vid, email="failed")
        self.legacy_files()
        with patch.object(Notifier, "resend", side_effect=AssertionError("legacy fallback")):
            result = main.push_only(self.config, DATE)
        self.assertEqual(result["version_id"], vid)
        self.assertIsNone(result["report_path"])
        self.assertIn("RENDER_FAILED", [e["code"] for e in result["errors"]])
        self.assertEqual(self.db.get_report(DATE)["file_path"], "")
        self.email.assert_not_called()
        self.feishu.assert_not_called()

    def test_version_lookup_error_never_falls_back_and_closes_db(self):
        self.legacy_files()
        with patch.object(Database, "get_latest_published_digest_version",
                          side_effect=sqlite3.OperationalError("simulated lookup failure")), \
                patch.object(Notifier, "resend") as resend:
            with self.assertRaises(sqlite3.OperationalError):
                main.push_only(self.config, DATE)
        resend.assert_not_called()
        self.opened[-1].assert_called_once()

    def test_unpublished_version_does_not_fallback(self):
        self.version(render=False, status="draft")
        self.legacy_files()
        with patch.object(Notifier, "resend") as resend:
            with self.assertRaises(DigestError):
                main.push_only(self.config, DATE)
        resend.assert_not_called()
        self.opened[-1].assert_called_once()

    def test_legacy_resend_keeps_response_and_report_metadata(self):
        md = self.legacy_files()
        self.db.save_report(DATE, str(md), 9, 3)
        self.feishu.side_effect = RuntimeError("simulated legacy failure")
        with patch.object(DigestService, "retry_digest_send") as retry:
            result = main.push_only(self.config, DATE)
        retry.assert_not_called()
        self.assertEqual(result, {"date": DATE, "report_path": str(md),
                                  "push_results": {"email": True, "feishu": False},
                                  "version_id": None, "version": None})
        self.email.assert_called_once()
        self.feishu.assert_called_once()
        rep = self.db.get_report(DATE)
        self.assertEqual((rep["total_found"], rep["total_pushed"]), (9, 3))
        self.assertEqual(json.loads(rep["push_results"]), result["push_results"])
        self.opened[-1].assert_called_once()

    def test_missing_legacy_files_raises_without_saving_report(self):
        with self.assertRaises(FileNotFoundError):
            main.push_only(self.config, DATE)
        self.assertIsNone(self.db.get_report(DATE))
        self.opened[-1].assert_called_once()
        self.email.assert_not_called()
        self.feishu.assert_not_called()

    def test_real_weekly_files_match_db_counts_and_remain_legacy(self):
        rows = [("2026-09-06", 9, "Previous week"),
                ("2026-09-07", 8, "Monday relevant"),
                ("2026-09-11", 2, "Friday low score"),
                ("2026-09-13", 5, "Sunday threshold"),
                ("2026-09-14", 9, "Next week")]
        ids = self.db.save_articles_batch([
            {"doi": f"10.test/weekly-{i}", "title": title, "journal": "Test journal"}
            for i, (_, _, title) in enumerate(rows)
        ])
        with self.db.get_connection() as conn:
            for aid, (date, relevance, _) in zip(ids, rows):
                conn.execute("UPDATE articles SET created_at = ?, relevance = ? WHERE id = ?",
                             (f"{date} 12:00:00", relevance, aid))
            conn.commit()
        with patch.object(main, "DigestService", side_effect=AssertionError("weekly versioned")):
            result = main.run_weekly(self.config, DATE)
            repeated = main.run_weekly(self.config, DATE)
        self.assertEqual(result, repeated)
        self.assertEqual(result["label"], "2026-W37")
        self.assertEqual((result["total_found"], result["total_pushed"]), (3, 2))
        md = Path(result["report_path"])
        html = Path(result["html_path"])
        self.assertEqual(md.name, "weekly-2026-W37.md")
        self.assertEqual(html.name, "weekly-2026-W37.html")
        text = md.read_text(encoding="utf-8")
        self.assertIn("本周入库 **3** 篇", text)
        self.assertIn("相关（≥阈值）**2** 篇", text)
        self.assertIn("Monday relevant", text)
        self.assertIn("Sunday threshold", text)
        self.assertNotIn("Previous week", text)
        self.assertNotIn("Next week", text)
        self.assertIn("Monday relevant", html.read_text(encoding="utf-8"))
        rep = self.db.get_report("week-2026-W37")
        self.assertEqual(rep["kind"], "weekly")
        self.assertEqual(rep["file_path"], str(md))
        self.assertEqual((rep["total_found"], rep["total_pushed"]), (3, 2))
        self.assertIsNone(rep["push_results"])
        self.assertEqual(self.db.list_digest_versions(), [])
        self.email.assert_not_called()
        self.feishu.assert_not_called()
        for close in self.opened:
            close.assert_called_once()

    def test_real_notifier_preserves_all_snapshot_ids_and_ranks(self):
        articles = [{"doi": f"10.test/rank-{i}", "title": f"Snapshot paper {i}"}
                    for i in range(18)]
        ids = self.db.save_articles_batch(articles)
        items = [{"article_id": aid, "rank": rank, "category": "mlip" if rank % 2 else "dft",
                  "reason": f"Recommendation {rank}",
                  "scores_json": json.dumps({"final": 20 - rank}),
                  "snapshot_json": json.dumps({
                      "title": f"Snapshot paper {rank}", "relevance": rank / 2,
                      "journal": "Test", "abstract": "Source abstract",
                      "evidence_level": "FULLTEXT" if rank % 2 else "ABSTRACT",
                      "analysis": "Model inference" if rank % 2 else None,
                      "url": "javascript:alert(1)" if rank == 1 else "https://example.test/paper",
                  })} for rank, aid in enumerate(reversed(ids), 1)]
        saved = self.db.save_digest_version(
            digest_date=DATE, items=list(reversed(items)),
            stats_json=json.dumps({"selected": 18, "candidates": 40, "excluded_repeat": 2,
                                   "by_category": {"mlip": 9, "dft": 9}}))
        result = self.service.render_digest(saved["version_id"])
        artifacts = {a["format"]: Path(a["path"]) for a in result["artifacts"]}
        md = artifacts["markdown"].read_text(encoding="utf-8")
        html = artifacts["html"].read_text(encoding="utf-8")
        self.assertNotIn("版本 v", md)  # 版本号属于元数据，不进入面向读者的报告正文
        self.assertNotIn("快照 ", md)
        self.assertIn("从 40 篇候选文献中精选 18 篇", md)
        self.assertIn("机器学习势函数 9 篇、DFT / 第一性原理 9 篇", md)
        self.assertNotIn("近期已推送而排除", md)
        self.assertNotIn("本页内容取自", md)
        self.assertNotIn("本页内容取自", html)
        self.assertEqual(re.findall(r"^## (\d+)\. Snapshot paper", md, re.M),
                         [str(i) for i in range(1, 19)])
        self.assertNotIn("文章 ID：", md)
        self.assertEqual(re.findall(r'id="paper-(\d+)-rank-(\d+)"', md),
                         [(str(aid), str(rank)) for rank, aid in enumerate(reversed(ids), 1)])
        self.assertEqual(re.findall(r'id="paper-(\d+)-rank-(\d+)"', html),
                         re.findall(r'id="paper-(\d+)-rank-(\d+)"', md))
        for rank in range(1, 19):
            self.assertIn(f"Recommendation {rank}", md)
        self.assertIn("阅读材料：已获取全文", md)
        self.assertIn("阅读材料：仅有摘要", md)
        self.assertNotIn("不据此推断", md)
        self.assertNotIn("综合分：", md)
        self.assertNotIn("javascript:", html)
        # 数据库原文及当前配置变化不影响固定版本再次渲染。
        original_bytes = {fmt: p.read_bytes() for fmt, p in artifacts.items()}
        with self.db.get_connection() as conn:
            conn.execute("UPDATE articles SET title = 'MUTATED ARTICLE'")
            conn.commit()
        self.config["research_topics"] = ["MUTATED CONFIG"]
        self.service.render_digest(saved["version_id"])
        for fmt, p in artifacts.items():
            self.assertEqual(p.read_bytes(), original_bytes[fmt])
        for artifact in self.db.list_digest_artifacts(saved["version_id"]):
            self.assertEqual(artifact["content_hash"],
                             hashlib.sha256(Path(artifact["path"]).read_bytes()).hexdigest())

    def test_real_renderer_recovery_touches_only_requested_format(self):
        vid = self.version()
        artifacts = {a["format"]: Path(a["path"]) for a in self.db.list_digest_artifacts(vid)}
        artifacts["html"].write_text("KEEP HTML UNTOUCHED", encoding="utf-8")
        self.service.render_digest(vid, formats=["markdown"])
        self.assertEqual(artifacts["html"].read_text(encoding="utf-8"), "KEEP HTML UNTOUCHED")

    def test_version_email_has_no_mutable_index_attachment(self):
        vid = self.version()
        (self.notifier.output_dir / "paper_index.html").write_text("MUTABLE", encoding="utf-8")
        self.set_sends(vid, email="pending")
        main.push_only(self.config, DATE)
        self.assertEqual(self.email.call_args.kwargs["attachments"], [])
        self.assertIn("Fixed snapshot paper", self.email.call_args.args[0])

    def test_feishu_full_content_including_long_reports_never_truncates(self):
        md = self.legacy_files()
        text = "a" * 3500 + "END OF SNAPSHOT"
        md.write_text(text, encoding="utf-8")
        self.notifier.send_digest_files(md_path=str(md), html_path=None,
                                        date_str=DATE, channels=["feishu"])
        self.assertTrue(self.feishu.call_args.args[0].endswith(text))
        self.feishu.reset_mock()
        text = "长" * 10000 + "END OF SNAPSHOT"
        md.write_text(text, encoding="utf-8")
        self.notifier.send_digest_files(md_path=str(md), html_path=None,
                                        date_str=DATE, channels=["feishu"])
        parts = [call.args[0] for call in self.feishu.call_args_list]
        self.assertGreater(len(parts), 1)
        self.assertEqual("".join(part.split("\n\n", 1)[1] for part in parts), text)

    def test_feishu_http_success_requires_business_success_code(self):
        for body in ({"code": 0}, {"StatusCode": 0}, {"code": 1}, {}, []):
            with self.subTest(body=body):
                response = Mock()
                response.json.return_value = body
                with patch("core.notifier.requests.post", return_value=response) as post:
                    if body in ({"code": 0}, {"StatusCode": 0}):
                        self.real_feishu_text(self.notifier, "Full snapshot", {"webhook_url": "https://example.test/hook"})
                    else:
                        with self.assertRaises(RuntimeError if body == {"code": 1} else TimeoutError):
                            self.real_feishu_text(self.notifier, "Full snapshot", {"webhook_url": "https://example.test/hook"})
                self.assertEqual(post.call_args.kwargs["timeout"], 10)

    def test_weekly_build_failure_closes_db_and_saves_no_report(self):
        with patch("utils.weekly.build_weekly", side_effect=OSError("simulated write failure")):
            with self.assertRaises(OSError):
                main.run_weekly(self.config, DATE)
        self.assertIsNone(self.db.get_report("week-2026-W37"))
        self.opened[-1].assert_called_once()


if __name__ == "__main__":
    unittest.main()
