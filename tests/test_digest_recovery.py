"""Recovery regressions: temporary file databases, real threads, mock transport only."""
import hashlib
import sqlite3
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from core.db import Database
from digest.service import DigestError, DigestService
from test_digest_service import DATE, FakeNotifier, _make_config, _seed_articles


class DigestRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Database(str(Path(self.tmp.name) / "test.db"))
        self.addCleanup(self.db.close)
        self.cfg = _make_config()
        self.notifier = FakeNotifier(self.tmp.name)
        self.service = DigestService(self.db, self.cfg, self.notifier)
        _seed_articles(self.db, n=8)

    def parallel(self, fn):
        def worker(i):
            try:
                return fn(i)
            finally:
                self.db.close()
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(worker, i) for i in range(2)]
            return [f.result(timeout=15) for f in futures]

    def test_delivery_uses_verified_copy_after_source_replaced(self):
        published = self.service.publish_digest(DATE)
        vid = published["version_id"]
        self.db.init_digest_sends(vid, ["email"])
        originals = {a["format"]: Path(a["path"]) for a in published["artifacts"]}
        expected = {fmt: path.read_bytes() for fmt, path in originals.items()}
        delivered_paths = []
        def replace_then_read(**kwargs):
            for path in originals.values():
                path.write_text("REPLACED AFTER VERIFICATION", encoding="utf-8")
            for fmt, key in (("markdown", "md_path"), ("html", "html_path")):
                delivered_paths.append(Path(kwargs[key]))
                self.assertEqual(Path(kwargs[key]).read_bytes(), expected[fmt])
        with patch.object(self.notifier, "send_digest_files", side_effect=replace_then_read):
            result = self.service.retry_digest_send(vid, ["email"])
        self.assertEqual(result["deliveries"][0]["status"], "sent")
        self.assertTrue(all(not p.exists() for p in delivered_paths))

    def test_concurrent_publish_rechecks_under_write_lock(self):
        barrier = threading.Barrier(2)
        compute = self.service._compute_selection
        def synchronized(*args):
            result = compute(*args)
            barrier.wait(timeout=10)
            return result
        with patch.object(self.service, "_compute_selection", side_effect=synchronized):
            results = self.parallel(lambda i: self.service.publish_digest(
                DATE, request_key=f"publish-{i}", channels=["email"]))
        self.assertEqual(len({r["version_id"] for r in results}), 1)
        self.assertEqual(sum(r["created"] for r in results), 1)
        self.assertEqual(len(self.notifier.sent), 1)
        self.assertEqual(len(self.db.list_digest_versions()), 1)

    def test_concurrent_same_key_regenerate_reuses_without_rendering_loser(self):
        barrier = threading.Barrier(2)
        compute = self.service._compute_selection
        def synchronized(*args):
            result = compute(*args)
            barrier.wait(timeout=10)
            return result
        with patch.object(self.service, "_compute_selection", side_effect=synchronized):
            results = self.parallel(lambda _: self.service.regenerate_digest(
                DATE, request_key="same", channels=["email"]))
        self.assertEqual(sum(r["created"] for r in results), 1)
        self.assertEqual(len(self.notifier.sent), 1)
        self.assertEqual(self.notifier.render_calls, 1)

    def test_request_conflicts_and_channel_set_normalization(self):
        result = self.service.publish_digest(DATE, request_key="key", channels=["email", "feishu"])
        again = self.service.publish_digest(DATE, request_key="key", channels=["feishu", "email", "email"])
        self.assertEqual(result["version_id"], again["version_id"])
        for kwargs in ({"channels": ["email"]},
                       {"channels": ["email", "feishu"], "config": _make_config(limit=3)}):
            with self.subTest(kwargs=kwargs), self.assertRaises(DigestError) as cm:
                self.service.publish_digest(DATE, request_key="key", **kwargs)
            self.assertEqual(cm.exception.code, "CONFLICT")
        with self.assertRaises(DigestError) as cm:
            self.service.regenerate_digest(DATE, request_key="key", channels=["email", "feishu"])
        self.assertEqual(cm.exception.code, "CONFLICT")

    def test_pending_records_exist_before_render_and_without_notifier(self):
        self.service.notifier = None
        r = self.service.publish_digest(DATE, channels=["email"])
        self.assertEqual([a["status"] for a in r["artifacts"]], ["pending", "pending"])
        self.assertEqual(r["deliveries"][0]["status"], "pending")
        self.assertEqual(r["overall_status"], "partial")
        self.service.notifier = self.notifier
        self.service.render_digest(r["version_id"])
        retry = self.service.retry_digest_send(r["version_id"])
        self.assertEqual(retry["overall_status"], "success")
        self.assertEqual(len(self.notifier.sent), 1)

    def test_render_record_write_failure_keeps_atomic_recovery_work(self):
        with patch.object(self.db, "upsert_digest_artifact", side_effect=sqlite3.OperationalError("disk full")):
            r = self.service.publish_digest(DATE, channels=["email"])
        self.assertEqual(r["overall_status"], "partial")
        self.assertEqual(r["deliveries"][0]["status"], "pending")
        self.assertEqual(self.notifier.sent, [])
        self.service.render_digest(r["version_id"])
        self.assertEqual(self.service.retry_digest_send(r["version_id"])["overall_status"], "success")

    def test_retry_sent_is_still_persisted_success(self):
        r = self.service.publish_digest(DATE, channels=["email"])
        retry = self.service.retry_digest_send(r["version_id"], ["email"])
        self.assertEqual(retry["overall_status"], "success")
        self.assertEqual(len(self.notifier.sent), 1)

    def test_concurrent_same_key_different_requests_conflict(self):
        barrier = threading.Barrier(2)
        compute = self.service._compute_selection
        def synchronized(*args):
            result = compute(*args)
            barrier.wait(timeout=10)
            return result
        def publish(i):
            try:
                return self.service.publish_digest(DATE, request_key="race-key",
                                                   channels=[("email", "feishu")[i]])
            except DigestError as e:
                return e
        with patch.object(self.service, "_compute_selection", side_effect=synchronized):
            results = self.parallel(publish)
        self.assertEqual(sum(isinstance(r, DigestError) and r.code == "CONFLICT" for r in results), 1)
        self.assertEqual(len(self.db.list_digest_versions()), 1)
        self.assertEqual(len(self.notifier.sent), 1)

    def test_pending_record_insert_failure_rolls_back_snapshot(self):
        conn = self.db.get_connection()
        conn.execute("CREATE TRIGGER reject_send BEFORE INSERT ON digest_sends BEGIN SELECT RAISE(ABORT, 'injected'); END")
        with self.assertRaises(DigestError):
            self.service.publish_digest(DATE, channels=["email"])
        for table in ("digest_versions", "digest_items", "digest_artifacts", "digest_sends"):
            self.assertEqual(conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0], 0)
        self.assertEqual(self.notifier.render_calls, 0)

    def test_render_and_result_reload_persisted_rows(self):
        save = self.db.save_digest_version
        def persist(**kwargs):
            result = save(**kwargs)
            kwargs["items"][0]["snapshot_json"] = '{"title":"UNCOMMITTED"}'
            return result
        with patch.object(self.db, "save_digest_version", side_effect=persist):
            result = self.service.publish_digest(DATE)
        self.assertNotIn("UNCOMMITTED", self.notifier.last_content)
        self.assertEqual(result["items"], self.notifier.last_payload["items"])
        self.assertEqual(result["items"], self.service.get_digest(result["version_id"])["items"])

    def test_corrupt_json_is_explicit_read_failure(self):
        r = self.service.publish_digest(DATE)
        conn = self.db.get_connection()
        for table, column, value in (("digest_items", "snapshot_json", "{broken"),
                                     ("digest_items", "scores_json", "[]"),
                                     ("digest_versions", "stats_json", "null"),
                                     ("digest_versions", "config_json", "")):
            with self.subTest(column=column):
                old = conn.execute(f"SELECT {column} FROM {table} WHERE id=1").fetchone()[0]
                conn.execute(f"UPDATE {table} SET {column}=? WHERE id=1", (value,))
                conn.commit()
                with self.assertRaises(DigestError) as cm:
                    self.service.get_digest(r["version_id"])
                self.assertEqual(cm.exception.code, "HISTORY_UNAVAILABLE")
                conn.execute(f"UPDATE {table} SET {column}=? WHERE id=1", (old,))
                conn.commit()

    def test_hash_tamper_blocks_email_then_recovery_sends_snapshot(self):
        r = self.service.publish_digest(DATE)
        html = next(a for a in r["artifacts"] if a["format"] == "html")
        Path(html["path"]).write_text("tampered", encoding="utf-8")
        rejected = self.service.send_digest(r["version_id"], ["email"])
        self.assertEqual(self.notifier.sent, [])
        self.assertEqual(rejected["deliveries"][0]["status"], "pending")
        self.assertEqual(rejected["overall_status"], "partial")
        recovered = self.service.render_digest(r["version_id"])
        self.assertEqual(recovered["items"], r["items"])
        self.service.retry_digest_send(r["version_id"])
        self.assertEqual(len(self.notifier.sent), 1)

    def test_segmented_delivery_records_progress_and_blocks_blind_retry(self):
        from core.notifier import FeishuDigestDeliveryUnknown
        with patch.object(self.notifier, "send_digest_files",
                          side_effect=FeishuDigestDeliveryUnknown(1, 3)) as send:
            result = self.service.publish_digest(DATE, channels=["feishu"])
            channel = result["deliveries"][0]
            self.assertEqual(channel["status"], "unknown")
            self.assertIn("1/3", channel["last_error"])
            self.assertEqual(result["overall_status"], "partial")
            self.service.retry_digest_send(result["version_id"], ["feishu"])
            send.assert_called_once()

    def test_success_then_write_failure_is_unknown_and_not_retried(self):
        finish = self.db.finish_digest_send
        def failing_sent(*args, **kwargs):
            if args[3] == "sent":
                raise sqlite3.OperationalError("injected commit failure")
            return finish(*args, **kwargs)
        with patch.object(self.db, "finish_digest_send", side_effect=failing_sent):
            r = self.service.publish_digest(DATE, channels=["email"])
        self.assertEqual(r["deliveries"][0]["status"], "unknown")
        self.assertEqual(r["overall_status"], "partial")
        self.service.retry_digest_send(r["version_id"])
        self.assertEqual(len(self.notifier.sent), 1)
        resolved = self.service.resolve_digest_send(r["version_id"], "email", True)
        self.assertEqual(resolved["overall_status"], "success")

    def test_total_write_failure_retains_claim_until_explicit_recovery(self):
        with patch.object(self.db, "finish_digest_send", side_effect=sqlite3.OperationalError("unavailable")):
            r = self.service.publish_digest(DATE, channels=["email"])
        self.assertEqual(r["deliveries"][0]["status"], "sending")
        self.assertEqual(r["overall_status"], "partial")
        self.service.retry_digest_send(r["version_id"])
        self.assertEqual(len(self.notifier.sent), 1)
        token = r["deliveries"][0]["claim_token"]
        with self.assertRaises(DigestError):
            self.service.recover_digest_send(r["version_id"], "email", "wrong")
        recovered = self.service.recover_digest_send(r["version_id"], "email", token)
        self.assertEqual(recovered["deliveries"][0]["status"], "unknown")
        self.assertFalse(self.db.finish_digest_send(r["version_id"], "email", token, "sent"))
        resolved = self.service.resolve_digest_send(r["version_id"], "email", False)
        self.assertEqual(resolved["deliveries"][0]["status"], "pending")
        self.assertEqual(len(self.notifier.sent), 1)

    def test_concurrent_retry_has_one_transport_owner(self):
        self.service.notifier = None
        r = self.service.publish_digest(DATE, channels=["email"])
        self.service.notifier = self.notifier
        self.service.render_digest(r["version_id"])
        barrier = threading.Barrier(2)
        claim = self.db.claim_digest_send
        def synchronized(*args):
            barrier.wait(timeout=10)
            return claim(*args)
        with patch.object(self.db, "claim_digest_send", side_effect=synchronized):
            self.parallel(lambda _: self.service.retry_digest_send(r["version_id"]))
        self.assertEqual(len(self.notifier.sent), 1)
        self.assertEqual(self.db.list_digest_sends(r["version_id"])[0]["attempts"], 1)

    def test_empty_requested_channels_never_send_or_retry(self):
        self.db.delete_articles_by_ids([a["id"] for a in self.db.list_articles_created_between("2026-01-01", "2027-01-01")])
        r = self.service.publish_digest(DATE, channels=["email", "feishu"])
        self.assertEqual(r["selected_count"], 0)
        self.assertEqual({d["status"] for d in r["deliveries"]}, {"skipped"})
        self.service.send_digest(r["version_id"], ["email"])
        self.service.retry_digest_send(r["version_id"])
        self.assertEqual(self.notifier.sent, [])

    def test_persisted_failed_unknown_sending_pending_are_never_success(self):
        r = self.service.publish_digest(DATE)
        self.db.init_digest_sends(r["version_id"], ["email"])
        for state in ("failed", "unknown", "sending", "pending"):
            with self.subTest(state=state):
                conn = self.db.get_connection()
                conn.execute("UPDATE digest_sends SET status=?", (state,))
                conn.commit()
                self.assertEqual(self.service.get_digest(r["version_id"])["overall_status"], "partial")
                self.assertEqual(self.service.publish_digest(DATE)["overall_status"], "partial")

    def test_render_formats_reject_invalid_and_track_only_requested(self):
        self.service.notifier = None
        r = self.service.publish_digest(DATE)
        self.service.notifier = self.notifier
        for formats in ([], ["pdf"], None, 2):
            with self.subTest(formats=formats), self.assertRaises(DigestError):
                self.service.render_digest(r["version_id"], formats=formats)
        recovered = self.service.render_digest(r["version_id"], formats=["markdown"])
        self.assertEqual({a["format"]: a["status"] for a in recovered["artifacts"]},
                         {"markdown": "rendered", "html": "pending"})


class DigestMigrationBackupTests(unittest.TestCase):
    def test_wal_backup_precedes_new_version_tables_and_preserves_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "old.db"
            conn = sqlite3.connect(path)
            self.addCleanup(conn.close)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA wal_autocheckpoint=0")
            conn.execute("CREATE TABLE articles (id INTEGER PRIMARY KEY, doi TEXT, title TEXT, journal TEXT, authors TEXT, pub_date TEXT, url TEXT, abstract TEXT, relevance REAL, processed INTEGER DEFAULT 0)")
            conn.execute("INSERT INTO articles(id, title) VALUES(41, 'committed WAL article')")
            conn.commit()
            db = Database(str(path))
            db.close()
            backups = list(Path(tmp).glob("old.backup-*.db"))
            self.assertEqual(len(backups), 1)
            with sqlite3.connect(backups[0]) as backup:
                self.assertEqual(backup.execute("PRAGMA integrity_check").fetchone()[0], "ok")
                self.assertEqual(backup.execute("SELECT id,title FROM articles").fetchone(), (41, "committed WAL article"))
                self.assertIsNone(backup.execute("SELECT name FROM sqlite_master WHERE name='digest_versions'").fetchone())
            db = Database(str(path))
            db.close()
            self.assertEqual(len(list(Path(tmp).glob("old.backup-*.db"))), 1)

    def test_backup_failure_stops_migration_before_ddl(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "old.db"
            with sqlite3.connect(path) as conn:
                conn.execute("CREATE TABLE sentinel (id INTEGER)")
            with patch.object(Database, "_backup_before_migration", side_effect=OSError("no space")):
                with self.assertRaises(OSError):
                    Database(str(path))
            with sqlite3.connect(path) as conn:
                self.assertEqual(conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall(), [("sentinel",)])


if __name__ == "__main__":
    unittest.main()
