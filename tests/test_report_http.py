"""Report HTTP contracts using only temporary files, SQLite, and mocked services."""
import copy
import http.client
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import web_server
from digest.service import DigestError


class ReportHTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.output = root / "output"
        self.version_dir = self.output / "daily" / "2026-09-11" / "v2"
        self.version_dir.mkdir(parents=True)
        self.md = self.version_dir / "report.md"
        self.md.write_text("# fixed version two", encoding="utf-8")
        self.html = self.md.with_suffix(".html")
        self.html.write_text("<h1>fixed version two</h1>", encoding="utf-8")
        self.db_path = root / "test.sqlite"
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("CREATE TABLE daily_reports (report_date TEXT, file_path TEXT, total_found INT, total_pushed INT, push_results TEXT, kind TEXT, created_at TEXT)")
            conn.execute("INSERT INTO daily_reports VALUES (?, ?, 4, 2, '{}', 'daily', 'now')", ("2026-09-11", str(self.md)))
        self.state = {"running": False, "last_stats": {"digest_version_id": 7, "digest_version": 2}}
        self.ctx = SimpleNamespace(
            output_dir=self.output, config={"web": {"protect_read": True}},
            api_token="test-only-token", db_path=self.db_path,
            connect_db=self.connect, runner=SimpleNamespace(get_state=lambda: copy.deepcopy(self.state)),
        )
        self.detail = {
            "version_id": 7, "date": "2026-09-11", "version": 2, "status": "published",
            "selected_count": 2, "overall_status": "partial", "errors": [],
            "artifacts": [{"format": "markdown", "status": "rendered", "path": str(self.md)},
                          {"format": "html", "status": "rendered", "path": str(self.html)}],
            "deliveries": [{"channel": "email", "status": "failed", "error": "offline"}],
            "items": [{"rank": 1, "snapshot": {"title": "private paper"}}],
        }
        self.service = Mock()
        self.service.get_digest.side_effect = lambda version_id: copy.deepcopy(self.detail)
        self.service.list_digest_history.return_value = {
            "items": [{"version_id": 7, "date": "2026-09-11", "version": 2, "status": "published", "created_at": "now"}],
            "has_more": False, "next_offset": None,
        }
        self.service.render_digest.return_value = self.detail
        self.service.retry_digest_send.return_value = self.detail
        service_patch = patch.object(web_server, "_digest_service", return_value=self.service)
        service_patch.start()
        self.addCleanup(service_patch.stop)
        summary_patch = patch.object(web_server, "_db_summary", return_value={})
        summary_patch.start()
        self.addCleanup(summary_patch.stop)
        self.server = web_server.ThreadingHTTPServer(("127.0.0.1", 0), web_server.Handler)
        self.server.context = self.ctx
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_server)

    def stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def connect(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def request(self, path, method="GET", body=None, authenticated=True):
        conn = http.client.HTTPConnection(*self.server.server_address, timeout=5)
        headers = {"X-API-Token": "test-only-token"} if authenticated else {}
        if body is not None:
            headers["Content-Type"] = "application/json"
        try:
            conn.request(method, path, body=json.dumps(body) if body is not None else None, headers=headers)
            response = conn.getresponse()
            content = response.read()
            return response.status, content
        finally:
            conn.close()

    def get_json(self, path):
        status, body = self.request(path)
        self.assertEqual(status, 200, body)
        return json.loads(body)

    def test_reports_require_header_and_reject_query_tokens(self):
        url = "/reports/daily/2026-09-11/v2/report.html"
        for path in (url, url + "?token=test-only-token", "/api/reports?token=test-only-token"):
            status, _ = self.request(path, authenticated=False)
            self.assertEqual(status, 401)
        status, content = self.request(url)
        self.assertEqual(status, 200)
        self.assertIn(b"fixed version two", content)

    def test_legacy_index_preserves_version_directory(self):
        row = self.get_json("/api/reports")["items"][0]
        self.assertEqual(row["md_url"], "/reports/daily/2026-09-11/v2/report.md")
        self.assertEqual(row["html_url"], "/reports/daily/2026-09-11/v2/report.html")
        self.assertEqual(self.request(row["md_url"])[0], 200)

    def test_history_contains_summaries_without_snapshot_items(self):
        row = self.get_json("/api/digests?limit=30")["items"][0]
        self.assertEqual(row["selected_count"], 2)
        self.assertEqual(row["deliveries"][0]["status"], "failed")
        self.assertNotIn("items", row)
        self.assertEqual(row["artifacts"][1]["url"], "/reports/daily/2026-09-11/v2/report.html")
        self.assertEqual(row["created_at"], "now")
        self.service.get_digest.assert_called_once_with(7)

    def test_summary_failure_does_not_drop_history(self):
        self.service.get_digest.side_effect = DigestError("history offline", code="HISTORY_UNAVAILABLE")
        row = self.get_json("/api/digests")["items"][0]
        self.assertEqual(row["version_id"], 7)
        self.assertEqual(row["summary_error"]["code"], "HISTORY_UNAVAILABLE")

    def test_detail_and_recovery_return_fixed_artifact_urls(self):
        detail = self.get_json("/api/digests/7")
        self.assertIn("url", detail["artifacts"][0])
        for action in ("render", "retry-send"):
            status, body = self.request(f"/api/digests/7/{action}", "POST", {"channels": ["email"]})
            self.assertEqual(status, 200, body)
            self.assertEqual(json.loads(body)["artifacts"][0]["url"], detail["artifacts"][0]["url"])
        self.service.render_digest.assert_called_once_with(7)
        self.service.retry_digest_send.assert_called_once_with(7, channels=["email"])
        self.assertEqual(self.request("/api/digests/7/render?token=test-only-token", "POST", {}, authenticated=False)[0], 401)

    def test_explicit_send_recovery_is_authenticated_and_never_retries(self):
        self.service.recover_digest_send.return_value = self.detail
        self.service.resolve_digest_send.return_value = self.detail
        for action, body in (
            ("recover-send", {"channel": "email", "claim_token": "old-claim"}),
            ("resolve-send", {"channel": "email", "delivered": False}),
        ):
            route = f"/api/digests/7/{action}"
            self.assertEqual(self.request(route, "POST", body, authenticated=False)[0], 401)
            self.assertEqual(self.request(route, "POST", body)[0], 200)
        self.service.recover_digest_send.assert_called_once_with(7, channel="email", claim_token="old-claim")
        self.service.resolve_digest_send.assert_called_once_with(7, channel="email", delivered=False)
        self.service.retry_digest_send.assert_not_called()

    def test_recovery_routes_integrate_with_service_without_sending(self):
        from core.db import Database
        from digest.service import DigestService
        db = Database(str(Path(self.temp.name) / "recovery.sqlite"))
        self.addCleanup(db.close)
        version_id = db.save_digest_version(digest_date="2026-09-11", items=[])["version_id"]
        db.init_digest_sends(version_id, ["email"])
        db.claim_digest_send(version_id, "email", "original-claim")
        notifier = Mock()
        service = DigestService(db, {}, notifier=notifier)
        with patch.object(web_server, "_digest_service", return_value=service):
            detail = self.get_json(f"/api/digests/{version_id}")
            self.assertEqual(detail["deliveries"][0]["claim_token"], "original-claim")
            status, body = self.request(f"/api/digests/{version_id}/recover-send", "POST",
                                        {"channel": "email", "claim_token": "original-claim"})
            self.assertEqual(status, 200, body)
            self.assertEqual(json.loads(body)["deliveries"][0]["status"], "unknown")
            status, body = self.request(f"/api/digests/{version_id}/resolve-send", "POST",
                                        {"channel": "email", "delivered": False})
            self.assertEqual(status, 200, body)
            self.assertEqual(json.loads(body)["deliveries"][0]["status"], "pending")
            notifier.send_digest_files.assert_not_called()

    def test_recovery_conflict_preserves_structured_error(self):
        self.service.recover_digest_send.side_effect = DigestError(
            "claim changed", code="CONFLICT", recovery_action="refresh history")
        status, body = self.request("/api/digests/7/recover-send", "POST", {"channel": "email", "claim_token": "stale"})
        self.assertEqual(status, 409)
        self.assertEqual(json.loads(body)["error"]["recovery_action"], "refresh history")

    def test_task_has_fixed_digest_link_and_failure_details(self):
        task = self.get_json("/api/status")["task"]
        self.assertEqual(task["digest_url"], "/#digest_version=7")
        self.assertEqual(task["digest"]["overall_status"], "partial")
        self.assertEqual(task["digest"]["deliveries"][0]["error"], "offline")
        self.assertNotIn("items", task["digest"])
        self.assertNotIn("digest", self.state)

    def test_outside_artifacts_and_encoded_traversal_are_rejected(self):
        outside = Path(self.temp.name) / "secret.html"
        outside.write_text("secret", encoding="utf-8")
        self.detail["artifacts"][1]["path"] = str(outside)
        self.assertIsNone(self.get_json("/api/digests/7")["artifacts"][1]["url"])
        for path in ("/reports/%2e%2e/secret.html", "/reports/%2fetc/passwd", "/reports/%5csecret.html"):
            self.assertEqual(self.request(path)[0], 400)
        (self.output / "escape.html").symlink_to(outside)
        self.assertEqual(self.request("/reports/escape.html")[0], 400)

    def test_encoded_artifact_names_can_be_downloaded(self):
        name = self.version_dir / "report copy.html"
        name.write_text("encoded", encoding="utf-8")
        self.detail["artifacts"][1]["path"] = str(name)
        url = self.get_json("/api/digests/7")["artifacts"][1]["url"]
        self.assertIn("%20", url)
        self.assertEqual(self.request(url), (200, b"encoded"))


class TaskDigestStatusTests(unittest.TestCase):
    def test_digest_failure_and_partial_persist_actual_task_status(self):
        for digest_status, expected in (("failed", "failed"), ("partial", "partial")):
            with self.subTest(digest_status=digest_status):
                runner = web_server.TaskRunner({})
                db = Mock()
                runner.attach_db(db)
                stats = {"digest_overall_status": digest_status, "digest_errors": [{"message": "render unavailable"}]}
                with patch.object(web_server, "run_once", return_value=stats):
                    runner._run_task("test-task", "manual", "default", "2026-09-11")
                self.assertEqual(db.task_finish.call_args.kwargs["status"], expected)
                self.assertEqual(runner.state["failure_count"], int(digest_status == "failed"))
                self.assertEqual(runner.state["last_stats"], stats)


if __name__ == "__main__":
    unittest.main()
