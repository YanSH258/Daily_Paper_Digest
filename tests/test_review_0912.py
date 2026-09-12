"""Review regressions: uncertain delivery and effective selection configuration."""
import copy
import unittest
import requests
from unittest.mock import patch
import test_feishu_long_report as feishu_tests
from test_feishu_long_report import response
from test_digest_service import DigestServiceTestBase, DATE, _seed_articles
from digest.service import DigestError


class DeliveryResponseTests(unittest.TestCase):
    setUp = feishu_tests.FeishuLongReportTests.setUp
    write_report = feishu_tests.FeishuLongReportTests.write_report
    version = feishu_tests.FeishuLongReportTests.version

    def test_lost_response_blocks_retry_for_short_and_multipart(self):
        for text in ("short", "long " * 10000):
            for failure in (requests.exceptions.ConnectionError("response lost"),
                            requests.exceptions.ChunkedEncodingError("truncated")):
                with self.subTest(size=len(text), failure=type(failure).__name__):
                    self.post.reset_mock()
                    self.post.side_effect = failure
                    vid = self.version(text)
                    result = self.service.retry_digest_send(vid, ["feishu"])
                    self.assertEqual(result["deliveries"][0]["status"], "unknown")
                    self.service.retry_digest_send(vid, ["feishu"])
                    self.assertEqual(self.post.call_count, 1)

    def test_unreadable_response_is_unknown_but_explicit_rejection_is_failed(self):
        for reply, expected in ((response(json_error=ValueError("bad json")), "unknown"),
                                (response(body={}), "unknown"),
                                (response(http_error=requests.HTTPError("503")), "unknown"),
                                (response(body={"code": 123}), "failed")):
            with self.subTest(expected=expected):
                self.post.side_effect = None
                self.post.return_value = reply
                vid = self.version("short")
                result = self.service.retry_digest_send(vid, ["feishu"])
                self.assertEqual(result["deliveries"][0]["status"], expected)


class JournalSnapshotTests(DigestServiceTestBase):
    def test_effective_journal_list_is_frozen_and_changes_conflict(self):
        _seed_articles(self.db)
        cfg = copy.deepcopy(self.config)
        cfg["digest"]["tracks"] = {"top_chemistry": {"journals": [" Custom Journal ", "CUSTOM JOURNAL"]}}
        first = self.service.publish_digest(DATE, request_key="journals", config=cfg)
        equivalent = copy.deepcopy(cfg)
        equivalent["digest"]["tracks"]["top_chemistry"]["journals"] = ["custom journal"]
        again = self.service.publish_digest(DATE, request_key="journals", config=equivalent)
        self.assertEqual(first["version_id"], again["version_id"])
        changed = copy.deepcopy(cfg)
        changed["digest"]["tracks"]["top_chemistry"]["journals"] = ["Other Journal"]
        with self.assertRaises(DigestError) as caught:
            self.service.publish_digest(DATE, request_key="journals", config=changed)
        self.assertEqual(caught.exception.code, "CONFLICT")
        import json
        raw = self.db._conn().execute("SELECT config_json FROM digest_versions WHERE id=?", (first["version_id"],)).fetchone()[0]
        self.assertEqual(json.loads(raw)["top_chemistry_journals"], ["custom journal"])
