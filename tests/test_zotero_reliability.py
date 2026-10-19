"""Zotero regression paths use fake services only."""
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from integrations.zotero_client import ZoteroClient, ZoteroError
import web_server


class ZoteroReliabilityTests(unittest.TestCase):
    def client(self):
        c = ZoteroClient.__new__(ZoteroClient)
        c.zot = Mock()
        c.zot.everything.side_effect = lambda items: items
        c.zot.items.return_value = []
        c.zot.children.return_value = []
        c.zot.item_template.side_effect = lambda kind, **kwargs: {"itemType": kind}
        c.zot.create_items.return_value = {"success": {"0": "PARENT01"}}
        c.include_note = True
        c.attach_oa_pdf = True
        c._collection_key = ""
        c._resolve_pdf_url = Mock(return_value="https://example.org/a.pdf")
        c._attach_pdf = Mock()
        return c

    def test_existing_uppercase_doi_is_not_recreated(self):
        c = self.client()
        c.zot.items.return_value = [{"data": {"DOI": "10.1234/ABC", "key": "EXIST01"}}]
        c.include_note = c.attach_oa_pdf = False
        save = Mock(return_value=True)
        result = c.sync_article({"doi": "10.1234/abc"}, save_key=save)
        self.assertEqual(result["key"], "EXIST01")
        save.assert_called_once_with("EXIST01")
        c.zot.create_items.assert_not_called()

    @patch("time.sleep")
    def test_failed_doi_lookup_aborts_creation(self, sleep):
        c = self.client()
        c.zot.items.side_effect = RuntimeError("offline")
        with self.assertRaises(ZoteroError):
            c.sync_article({"doi": "10.1234/a"})
        c.zot.create_items.assert_not_called()

    def test_parent_is_saved_before_children_and_save_failure_is_reported(self):
        c = self.client()
        result = c.sync_article({"title": "No DOI"}, save_key=lambda key: False)
        self.assertFalse(result["ok"])
        self.assertEqual(result["key"], "PARENT01")
        self.assertEqual(c.zot.create_items.call_count, 1)
        c._attach_pdf.assert_not_called()

    def test_partial_failure_retry_reuses_parent_and_successful_note(self):
        c = self.client()
        c._attach_pdf.side_effect = RuntimeError("offline")
        article = {"title": "Paper"}
        result = c.sync_article(article, save_key=lambda key: article.update(zotero_key=key) is None)
        self.assertEqual(result["note"], "created")
        self.assertEqual(result["pdf"], "failed")
        self.assertTrue(result["warnings"])
        c.zot.children.return_value = [{"data": {"itemType": "note", "tags": [
            {"tag": "daily-paper-digest-note"}]}}]
        c.zot.create_items.reset_mock()
        c._attach_pdf.side_effect = None
        result = c.sync_article(article)
        self.assertEqual(result["note"], "present")
        self.assertEqual(result["pdf"], "uploaded")
        self.assertFalse(result["warnings"])
        c.zot.create_items.assert_not_called()

    def test_note_response_failure_and_missing_pdf_are_visible(self):
        c = self.client()
        c.zot.create_items.return_value = {"failed": {"0": "bad"}}
        c._resolve_pdf_url.return_value = None
        result = c.sync_article({"zotero_key": "EXIST01"})
        self.assertEqual(result["note"], "failed")
        self.assertEqual(result["pdf"], "unavailable")
        self.assertEqual(len(result["warnings"]), 2)

    def test_child_lookup_failure_does_not_duplicate_children(self):
        c = self.client()
        c.zot.children.side_effect = RuntimeError("offline")
        result = c.sync_article({"zotero_key": "EXIST01"})
        self.assertTrue(result["warnings"])
        c.zot.create_items.assert_not_called()
        c._attach_pdf.assert_not_called()

    def test_existing_uploaded_pdf_is_preserved(self):
        c = self.client()
        c.include_note = False
        c.zot.children.return_value = [{"data": {"itemType": "attachment",
            "contentType": "application/pdf", "linkMode": "imported_file", "md5": "hash"}}]
        result = c.sync_article({"zotero_key": "EXIST01"})
        self.assertEqual(result["pdf"], "present")
        c._attach_pdf.assert_not_called()

    @patch("integrations.zotero_client.ZoteroClient")
    def test_oversized_batch_is_rejected_before_external_calls(self, client):
        result, status = web_server._zotero_batch(SimpleNamespace(), {"ids": list(range(1, 102))})
        self.assertEqual(status, 400)
        self.assertEqual(result["limit"], 100)
        client.assert_not_called()

    @patch("web_server._get_article_detail")
    @patch("integrations.zotero_client.ZoteroClient")
    def test_http_handlers_report_save_failure_and_retry_existing(self, cls, detail):
        c = self.client()
        cls.return_value = c
        detail.return_value = {"zotero_key": "EXIST01"}
        ctx = SimpleNamespace(config={}, db=Mock())
        ctx.db.update_article_fields.return_value = False
        result, status = web_server._zotero_push(ctx, 1)
        self.assertEqual(status, 500)
        self.assertFalse(result["ok"])
        result, status = web_server._zotero_batch(ctx, {"ids": [1, 1]})
        self.assertEqual(len(result["failed"]), 1)
        self.assertEqual(result["skipped"], 0)
        ctx.db.update_article_fields.return_value = True
        result, status = web_server._zotero_batch(ctx, {"ids": [1]})
        self.assertEqual(result["results"][0]["pdf"], "uploaded")
        self.assertEqual(result["skipped"], 1)

    @patch("requests.get")
    def test_pdf_pending_attachment_is_reused_and_upload_failure_checked(self, get):
        c = self.client()
        get.return_value.content = b"%PDF" + b"x" * 1000
        c.zot.upload_attachments.return_value = {"success": [], "failure": ["bad"]}
        with self.assertRaises(ZoteroError):
            ZoteroClient._attach_pdf(c, "PARENT01", "https://example.org/a.pdf",
                {"key": "ATTACH01", "title": "OA PDF"})
        c.zot.create_items.assert_not_called()
        att = c.zot.upload_attachments.call_args.args[0][0]
        self.assertEqual(att["key"], "ATTACH01")
        basedir = c.zot.upload_attachments.call_args.kwargs["basedir"]
        self.assertFalse((Path(basedir) / att["filename"]).exists())

    @patch("requests.get")
    def test_new_pdf_uses_supported_upload_api_and_checks_result(self, get):
        c = self.client()
        get.return_value.content = b"%PDF" + b"x" * 1000
        c.zot.upload_attachments.return_value = {"unchanged": ["ok"]}
        ZoteroClient._attach_pdf(c, "PARENT01", "https://example.org/a.pdf")
        c.zot.item_template.assert_called_with("attachment", linkmode="imported_file")
        self.assertEqual(c.zot.upload_attachments.call_count, 1)
        att = c.zot.upload_attachments.call_args.args[0][0]
        self.assertEqual(att["contentType"], "application/pdf")

    def test_note_escapes_remote_metadata(self):
        text = ZoteroClient._build_note({"relevance_reason": "<script>x</script>"})
        self.assertNotIn("<script>", text)


if __name__ == "__main__":
    unittest.main()
