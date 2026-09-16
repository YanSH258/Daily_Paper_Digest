"""Versioned Feishu delivery: real temporary artifacts/DB, mocked requests only."""
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import requests

from core.db import Database
from core.notifier import FeishuDigestDeliveryUnknown, Notifier
from digest.service import DigestService


DATE = "2026-09-11"
WEBHOOK = "https://example.test/mock-feishu"
HEADER = f"📚 化学文献日报 {DATE}\n\n"
LONG_TEXT = ('# 原文 Markdown\r\n\n**中文🧪👩\u200d🔬** "quote" \\path\t\x00\r\n' * 2000
             + "\n最后一行 END  \r\n")


def response(body=None, *, http_error=None, json_error=None):
    result = Mock()
    result.raise_for_status.side_effect = http_error
    result.json.return_value = {"code": 0} if body is None else body
    result.json.side_effect = json_error
    return result


class FeishuLongReportTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.md = self.root / "report.md"
        self.config = {"output": {"output_dir": str(self.root / "output"),
                                  "feishu": {"enabled": True, "webhook_url": WEBHOOK}}}
        self.notifier = Notifier(self.config)
        self.db = Database(str(self.root / "test.db"))
        self.addCleanup(self.db.close)
        self.service = DigestService(self.db, self.config, self.notifier)
        patcher = patch("core.notifier.requests.post", return_value=response())
        self.post = patcher.start()
        self.addCleanup(patcher.stop)

    def write_report(self, text):
        self.md.write_bytes(text.encode("utf-8"))

    def send(self, text):
        self.write_report(text)
        self.notifier.send_digest_files(md_path=str(self.md), html_path=None,
                                        date_str=DATE, channels=["feishu"])

    def bodies(self):
        bodies = []
        for call in self.post.call_args_list:
            self.assertEqual(call.args, (WEBHOOK,))
            self.assertEqual(call.kwargs["timeout"], 10)
            self.assertEqual(set(call.kwargs), {"json", "timeout"})
            # Actual Requests json= wire serialization, independent of split helpers.
            prepared = requests.Request("POST", WEBHOOK, json=call.kwargs["json"]).prepare()
            self.assertIsInstance(prepared.body, bytes)
            self.assertLessEqual(len(prepared.body), 20000)
            self.assertEqual(int(prepared.headers["Content-Length"]), len(prepared.body))
            bodies.append(prepared.body)
        return bodies

    def assert_complete(self, original, *, multipart):
        bodies = self.bodies()
        texts = [json.loads(body)["content"]["text"] for body in bodies]
        if not multipart:
            self.assertEqual(texts, [HEADER + original])
        else:
            self.assertGreater(len(texts), 1)
            chunks = []
            for index, text in enumerate(texts, 1):
                prefix = f"📚 化学文献日报 {DATE}（第 {index}/{len(texts)} 段）\n\n"
                self.assertTrue(text.startswith(prefix))
                chunk = text[len(prefix):]
                self.assertTrue(chunk)
                chunks.append(chunk)
            self.assertEqual("".join(chunks), original)
        self.assertEqual(self.md.read_bytes(), original.encode("utf-8"))
        return bodies

    def version(self, text=LONG_TEXT):
        self.write_report(text)
        saved = self.db.save_digest_version(
            digest_date=DATE, status="published", channels=["feishu"],
            items=[{"rank": 1, "scores_json": "{}",
                    "snapshot_json": '{"title": "fixed snapshot"}'}])
        vid = saved["version_id"]
        self.db.upsert_digest_artifact(
            vid, "markdown", path=str(self.md), status="rendered",
            content_hash=hashlib.sha256(self.md.read_bytes()).hexdigest())
        return vid

    def test_short_and_empty_keep_old_format(self):
        for text in ("", "# 中文🧪\r\n  \"quoted\" \\ backslash\n", "a" * 3500):
            with self.subTest(text_length=len(text)):
                self.post.reset_mock()
                self.send(text)
                self.assert_complete(text, multipart=False)

    def test_complete_markdown_chinese_emoji_and_mixed_newlines(self):
        self.send(LONG_TEXT)
        self.assert_complete(LONG_TEXT, multipart=True)

    def test_single_huge_line_and_json_escaping(self):
        for unit in ("中", "🧪", '"\\\t\x00\b\f', "x"):
            with self.subTest(unit=unit):
                self.post.reset_mock()
                text = unit * 21000 + "END"
                self.send(text)
                self.assert_complete(text, multipart=True)

    def test_actual_serialized_request_boundary(self):
        for suffix in ("", "中文🧪", '"\\\n\r\t\x00'):
            payload = {"msg_type": "text", "content": {"text": HEADER + suffix}}
            overhead = len(requests.Request("POST", WEBHOOK, json=payload).prepare().body)
            for delta in (-1, 0, 1):
                with self.subTest(suffix=suffix, bytes=20000 + delta):
                    self.post.reset_mock()
                    text = "a" * (20000 - overhead + delta) + suffix
                    self.send(text)
                    bodies = self.assert_complete(text, multipart=delta > 0)
                    if delta <= 0:
                        self.assertEqual(len(bodies[0]), 20000 + delta)

    def test_part_count_digit_boundaries(self):
        for length, minimum in ((200000, 10), (2000000, 100)):
            with self.subTest(minimum=minimum):
                self.post.reset_mock()
                text = "x" * length + "END"
                self.send(text)
                self.assertGreaterEqual(self.post.call_count, minimum)
                self.assert_complete(text, multipart=True)

    def test_unrepresentable_header_fails_before_any_request(self):
        self.write_report("full report")
        with self.assertRaisesRegex(ValueError, "20000"):
            self.notifier.send_digest_files(md_path=str(self.md), html_path=None,
                                            date_str="x" * 20000, channels=["feishu"])
        self.post.assert_not_called()

    def test_missing_artifact_fails_before_any_request(self):
        with self.assertRaises(FileNotFoundError):
            self.notifier.send_digest_files(md_path=str(self.md), html_path=None,
                                            date_str=DATE, channels=["feishu"])
        self.post.assert_not_called()

    def test_partial_failure_exception_has_safe_counts(self):
        self.post.side_effect = [response(), response({"code": 1})]
        with self.assertRaises(FeishuDigestDeliveryUnknown) as caught:
            self.send(LONG_TEXT)
        error = caught.exception
        self.assertIsInstance(error, TimeoutError)
        self.assertEqual(error.delivered_parts, 1)
        self.assertIs(type(error.delivered_parts), int)
        self.assertIs(type(error.total_parts), int)
        self.assertGreater(error.total_parts, 2)
        self.assertNotIn(WEBHOOK, str(error))
        self.assertIsInstance(error.__cause__, RuntimeError)
        self.assertEqual(self.post.call_count, 2)
        self.post.side_effect = None
        self.post.reset_mock()
        self.send(LONG_TEXT)
        self.assertEqual(error.total_parts, self.post.call_count)

    def test_explicit_first_failure_is_failed_and_can_retry(self):
        failures = [response({"code": 1}), response({"StatusCode": 1})]
        for failure in failures:
            with self.subTest(failure=failure):
                self.post.reset_mock()
                self.post.side_effect = [failure]
                vid = self.version()
                result = self.service.send_digest(vid, ["feishu"])
                self.assertEqual(result["deliveries"][0]["status"], "failed")
                self.assertIn("DELIVERY_FAILED", [e["code"] for e in result["errors"]])
                self.assertEqual(self.post.call_count, 1)
                self.post.side_effect = None
                self.post.reset_mock()
                retried = self.service.retry_digest_send(vid, ["feishu"])
                self.assertEqual(retried["deliveries"][0]["status"], "sent")
                self.assert_complete(LONG_TEXT, multipart=True)

    def test_any_failure_after_confirmed_part_is_unknown_and_retry_skips(self):
        failures = [response({"code": 1}), response({"StatusCode": 1}),
                    response({"code": 0, "StatusCode": 1}), response({}), response([]),
                    response(json_error=ValueError("invalid JSON")),
                    response(http_error=requests.HTTPError("503 simulated")),
                    requests.ConnectionError("simulated disconnect"),
                    RuntimeError("simulated unexpected transport failure")]
        for failure in failures:
            with self.subTest(failure=failure):
                self.post.reset_mock()
                self.post.side_effect = [response(), failure]
                vid = self.version()
                result = self.service.send_digest(vid, ["feishu"])
                self.assertEqual(result["deliveries"][0]["status"], "unknown")
                self.assertIn("DELIVERY_UNKNOWN", [e["code"] for e in result["errors"]])
                self.assertEqual(self.post.call_count, 2)
                for channels in (None, ["feishu"]):
                    retried = self.service.retry_digest_send(vid, channels)
                    self.assertEqual(retried["deliveries"][0]["status"], "unknown")
                    self.assertEqual(self.post.call_count, 2)

    def test_timeout_first_middle_and_last_part_is_unknown_and_never_retried(self):
        self.send(LONG_TEXT)
        total = self.post.call_count
        for error_type in (requests.Timeout, requests.ConnectTimeout,
                           requests.ReadTimeout, TimeoutError):
            for confirmed in (0, 1, total - 1):
                with self.subTest(error_type=error_type, confirmed=confirmed):
                    self.post.reset_mock()
                    self.post.side_effect = ([response()] * confirmed
                                             + [error_type("simulated timeout")])
                    with self.assertRaises(FeishuDigestDeliveryUnknown) as caught:
                        self.send(LONG_TEXT)
                    self.assertEqual(caught.exception.delivered_parts, confirmed)
                    self.assertEqual(caught.exception.total_parts, total)
                    self.assertEqual(self.post.call_count, confirmed + 1)
                    self.post.reset_mock()
                    self.post.side_effect = ([response()] * confirmed
                                             + [error_type("simulated timeout")])
                    vid = self.version()
                    result = self.service.send_digest(vid, ["feishu"])
                    self.assertEqual(result["deliveries"][0]["status"], "unknown")
                    self.service.retry_digest_send(vid, ["feishu"])
                    self.assertEqual(self.post.call_count, confirmed + 1)

    def test_short_timeout_is_unknown_with_zero_of_one_confirmed(self):
        self.post.side_effect = requests.ReadTimeout("simulated timeout")
        with self.assertRaises(FeishuDigestDeliveryUnknown) as caught:
            self.send("short")
        self.assertEqual((caught.exception.delivered_parts, caught.exception.total_parts), (0, 1))
        self.assertEqual(self.post.call_count, 1)

    def test_all_parts_success_marks_sent_and_retry_does_not_resend(self):
        self.post.side_effect = lambda *args, **kwargs: response({"StatusCode": 0})
        vid = self.version()
        result = self.service.send_digest(vid, ["feishu"])
        self.assertEqual(result["deliveries"][0]["status"], "sent")
        self.assert_complete(LONG_TEXT, multipart=True)
        count = self.post.call_count
        self.service.retry_digest_send(vid, ["feishu"])
        self.service.send_digest(vid, ["feishu"])
        self.assertEqual(self.post.call_count, count)


if __name__ == "__main__":
    unittest.main()
