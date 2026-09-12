"""版本化日报闭环测试（WP B–E）：迁移、快照事务、幂等、渲染一致性、部分补发。

全部使用内存库 / 临时目录 / 模拟 Notifier；不联网、不发送、不触碰真实数据。
"""
import hashlib
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from core.db import Database  # noqa: E402
from digest.service import DigestError, DigestService  # noqa: E402


DATE = "2026-09-10"


class FakeNotifier:
    """模拟渲染与投递：渲染写临时文件；发送按预设失败/超时。"""

    def __init__(self, tmp: str, *, fail_render: bool = False,
                 fail_channels=(), timeout_channels=()):
        self.tmp = tmp
        self.fail_render = fail_render
        self.fail_channels = set(fail_channels)
        self.timeout_channels = set(timeout_channels)
        self.sent: list[dict] = []
        self.render_calls = 0

    def render_digest_version(self, payload):
        self.render_calls += 1
        if self.fail_render:
            raise RuntimeError("render boom")
        out = Path(self.tmp) / "daily" / payload["digest_date"] / f"v{payload['version']}"
        out.mkdir(parents=True, exist_ok=True)
        lines = [f"# 日报 {payload['digest_date']} v{payload['version']}"]
        for it in payload["items"]:
            lines.append(f"{it['rank']}. [id={it['article_id']}] {it['snapshot'].get('title')}")
        content = "\n".join(lines) + "\n"
        artifacts = []
        for fmt, name in (("markdown", "report.md"), ("html", "report.html")):
            p = out / name
            p.write_text(content, encoding="utf-8")
            artifacts.append({
                "format": fmt, "path": str(p),
                "content_hash": hashlib.sha256(content.encode("utf-8")).hexdigest(),
                "status": "rendered",
            })
        self.last_content = content
        self.last_payload = payload
        return artifacts

    def send_digest_files(self, *, md_path, html_path, date_str, channels):
        for ch in channels:
            if ch in self.fail_channels:
                raise RuntimeError(f"{ch} down")
            if ch in self.timeout_channels:
                raise TimeoutError(f"{ch} timeout")
            self.sent.append({"channel": ch, "md_path": md_path, "date": date_str})


def _make_config(limit=5, threshold=5.0):
    # 合成文章全部为 mlip 类别，放开该类上限以便断言 limit 生效
    return {"digest": {"daily": {"limit": limit,
                                 "category_limits": {"mlip": {"min": 0, "max": limit}}}},
            "relevance_threshold": threshold}


def _seed_articles(db, n=15, date=DATE, relevance_base=9.0):
    """n 篇 mlip 合成文章，相关性递减：Top-N 应只取前 limit 篇。

    created_at 显式固定为发表日当天——否则真实的入库时间（运行当天）会晚于
    重放报告日期，被入库时间独立上限排除（规范 §3.6）。
    """
    ids = db.save_articles_batch([
        {"doi": f"10.1/syn{i:02d}", "title": f"machine learning interatomic potential {i:02d}",
         "pub_date": date, "abstract": f"syn abstract {i}"}
        for i in range(1, n + 1)
    ])
    conn = db._memory_conn if db._memory_conn is not None else db._conn()
    created = f"{date} 08:00:00"
    for i, aid in enumerate(ids):
        conn.execute("UPDATE articles SET relevance = ?, created_at = ? WHERE id = ?",
                     (round(relevance_base - i * 0.1, 3), created, aid))
    conn.commit()
    return ids


class DigestServiceTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(self._cleanup)
        self.db = Database(":memory:")
        self.notifier = FakeNotifier(self.tmp)
        self.config = _make_config(limit=5)
        self.service = DigestService(self.db, self.config, notifier=self.notifier)

    def _cleanup(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)
        try:
            self.db.close()
        except Exception:
            pass


class TestMigrationAndSchema(DigestServiceTestBase):
    """WP B：迁移幂等、临时旧库兼容、关系保留。"""

    def test_new_tables_exist(self):
        names = {"digest_versions", "digest_items", "digest_artifacts", "digest_sends"}
        cur = self.db._memory_conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")
        present = {r[0] for r in cur.fetchall()}
        self.assertTrue(names <= present, msg=f"缺表: {names - present}")

    def test_legacy_db_migration_keeps_old_data(self):
        tmp = tempfile.mkdtemp()
        old_path = Path(tmp) / "legacy.db"
        conn = sqlite3.connect(old_path)
        conn.executescript("""
            CREATE TABLE articles (id INTEGER PRIMARY KEY AUTOINCREMENT, doi TEXT,
                title TEXT, journal TEXT, authors TEXT, pub_date TEXT, url TEXT,
                abstract TEXT, relevance REAL, processed INTEGER DEFAULT 0);
            CREATE TABLE digest_entries (id INTEGER PRIMARY KEY AUTOINCREMENT,
                digest_date TEXT NOT NULL, digest_type TEXT NOT NULL DEFAULT 'daily',
                article_id INTEGER NOT NULL, rank INTEGER, created_at TEXT DEFAULT (datetime('now')));
            INSERT INTO articles (doi, title) VALUES ('10.9/legacy', 'Legacy title');
            INSERT INTO digest_entries (digest_date, article_id, rank) VALUES ('2026-08-01', 1, 1);
        """)
        conn.commit()
        conn.close()
        db2 = Database(str(old_path))
        self.addCleanup(db2.close)
        cur = db2._conn().execute("SELECT title FROM articles WHERE doi='10.9/legacy'")
        self.assertEqual(cur.fetchone()[0], "Legacy title")  # 旧数据保留
        tables = {r[0] for r in db2._conn().execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        self.assertTrue({"digest_versions", "digest_items"} <= tables)  # 新表已建
        # 旧防重历史仍参与合并查询
        ids = db2.list_published_digest_article_ids_since("daily", "2026-07-01", "2026-09-01")
        self.assertEqual(ids, [1])

    def test_article_delete_preserves_published_snapshot(self):
        ids = _seed_articles(self.db, n=3)
        self.service.publish_digest(DATE, channels=[])
        self.db.delete_articles_by_ids(ids)
        result = self.service.get_digest(
            self.db.get_latest_published_digest_version(DATE)["id"])
        self.assertEqual(result["selected_count"], 3)
        titles = {it["snapshot"]["title"] for it in result["items"]}
        self.assertEqual(len(titles), 3)  # 快照完整保留，不随文章删除丢失


class TestVersionTransaction(DigestServiceTestBase):
    """WP B：快照事务、并发唯一约束、失败回滚。"""

    def test_duplicate_rank_rolls_back_whole_version(self):
        items = [
            {"article_id": 1, "rank": 1, "category": "mlip",
             "scores_json": "{}", "reason": "", "snapshot_json": "{}"},
            {"article_id": 2, "rank": 1, "category": "mlip",  # rank 冲突
             "scores_json": "{}", "reason": "", "snapshot_json": "{}"},
        ]
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.save_digest_version(digest_date=DATE, items=items)
        self.assertIsNone(self.db.get_latest_published_digest_version(DATE))  # 整笔回滚

    def test_request_key_unique_constraint(self):
        self.db.save_digest_version(digest_date=DATE, items=[],
                                    request_key="rk-1")
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.save_digest_version(digest_date="2026-09-11", items=[],
                                        request_key="rk-1")


class TestPublishIdempotency(DigestServiceTestBase):
    """WP C：发布幂等、预览零写入、请求键冲突。"""

    def test_publish_idempotent_same_day(self):
        _seed_articles(self.db, n=8)
        r1 = self.service.publish_digest(DATE, channels=["email"])
        sends_after_first = len(self.notifier.sent)
        r2 = self.service.publish_digest(DATE, channels=["email"])
        self.assertEqual(r1["version_id"], r2["version_id"])
        self.assertFalse(r2["created"])
        self.assertEqual(len(self.notifier.sent), sends_after_first)  # 不重复发送
        self.assertEqual(self.notifier.render_calls, 1)  # 不重复渲染

    def test_preview_has_no_side_effects(self):
        _seed_articles(self.db, n=8)
        preview = self.service.preview_digest(DATE)
        self.assertEqual(preview["selected_count"], 5)  # limit=5
        cur = self.db._memory_conn.execute("SELECT COUNT(*) FROM digest_versions")
        self.assertEqual(cur.fetchone()[0], 0)  # 预览零业务写入
        cur = self.db._memory_conn.execute("SELECT COUNT(*) FROM digest_items")
        self.assertEqual(cur.fetchone()[0], 0)

    def test_request_key_conflict_across_dates(self):
        self.service.publish_digest("2026-09-09", request_key="rk-x")
        with self.assertRaises(DigestError) as cm:
            self.service.regenerate_digest(DATE, request_key="rk-x")
        self.assertEqual(cm.exception.code, "CONFLICT")

    def test_history_read_failure_stops_publish(self):
        _seed_articles(self.db, n=3)
        import unittest.mock
        with unittest.mock.patch.object(
                self.db, "list_published_digest_article_ids_since",
                side_effect=sqlite3.OperationalError("db corrupted")):
            with self.assertRaises(DigestError) as cm:
                self.service.publish_digest(DATE)
        self.assertEqual(cm.exception.code, "HISTORY_UNAVAILABLE")
        self.assertIsNone(self.db.get_latest_published_digest_version(DATE))


class TestVersionLifecycle(DigestServiceTestBase):
    """WP C：regenerate 新版本、旧版保留、渲染恢复、空候选。"""

    def test_regenerate_creates_new_version_keeps_old(self):
        _seed_articles(self.db, n=8)
        r1 = self.service.publish_digest(DATE)
        r2 = self.service.regenerate_digest(DATE, request_key="regen-1")
        self.assertEqual(r1["version"], 1)
        self.assertEqual(r2["version"], 2)
        old = self.service.get_digest(r1["version_id"])
        self.assertEqual(old["status"], "published")  # 旧版保留
        # 同 key 重试 → 幂等返回 v2，不创建 v3
        r2_retry = self.service.regenerate_digest(DATE, request_key="regen-1")
        self.assertEqual(r2_retry["version_id"], r2["version_id"])
        self.assertFalse(r2_retry["created"])

    def test_render_recovery_does_not_reselect(self):
        _seed_articles(self.db, n=6)
        broken = FakeNotifier(self.tmp, fail_render=True)
        self.service.notifier = broken
        # 渲染失败不抛异常：保留版本，返回 partial 结果与恢复指引（§4.2）
        result = self.service.publish_digest(DATE)
        self.assertTrue(any(e["code"] == "RENDER_FAILED" for e in result["errors"]))
        self.assertEqual(result["overall_status"], "partial")
        version_id = result["version_id"]
        self.assertIsNotNone(version_id)  # 版本保留
        # 恢复渲染：不重新选文（文章不变，条目一致）
        self.service.notifier = FakeNotifier(self.tmp)
        result = self.service.render_digest(version_id)
        self.assertTrue(all(a["status"] == "rendered" for a in result["artifacts"]))
        items = self.db.get_digest_items(version_id)
        self.assertEqual([it["rank"] for it in items], [1, 2, 3, 4, 5])

    def test_empty_pool_publishes_empty_version(self):
        result = self.service.publish_digest(DATE)  # 库中无文章
        self.assertEqual(result["selected_count"], 0)
        self.assertIsNotNone(result["version_id"])
        self.assertIn("空候选", result.get("note", ""))
        self.assertEqual(self.notifier.sent, [])  # 空报告不发送

    def test_config_hash_changes_with_config(self):
        _seed_articles(self.db, n=6)
        r1 = self.service.publish_digest(DATE)
        v1 = self.db.get_digest_version_by_id(r1["version_id"])
        self.service.regenerate_digest(
            DATE, request_key="cfg-change",
            config=_make_config(limit=3))
        r2 = self.db.get_latest_published_digest_version(DATE)
        v2 = self.db.get_digest_version_by_id(r2["id"])
        self.assertNotEqual(v1["config_hash"], v2["config_hash"])
        self.assertEqual(len(self.db.get_digest_items(r2["id"])), 3)


class TestReportConsistency(DigestServiceTestBase):
    """WP D 验收：15 篇合成数据，实际报告是 Top-N 且各入口 ID/排名一致。"""

    def test_report_is_top_n_and_consistent_across_entries(self):
        ids = _seed_articles(self.db, n=15)  # 相关性 9.0 递减到 7.6
        result = self.service.publish_digest(DATE, channels=["email"])
        self.assertEqual(result["selected_count"], 5)  # limit=5，不因 15 篇凑数
        # 入口一：服务读取（固定快照）
        via_service = [(it["rank"], it["article_id"]) for it in result["items"]]
        # 入口二：DB 直读
        via_db = [(it["rank"], it["article_id"]) for it in self.db.get_digest_items(result["version_id"])]
        self.assertEqual(via_service, via_db)
        # 应为相关性最高的 5 篇，rank 递增
        expected_ids = [ids[i] for i in range(5)]  # relevance_base - i*0.1 递减
        self.assertEqual([aid for _, aid in via_service], expected_ids)
        # 入口三：Markdown 文件内容按 rank 列出同样的文章
        md_art = next(a for a in result["artifacts"] if a["format"] == "markdown")
        md_text = Path(md_art["path"]).read_text(encoding="utf-8")
        for rank, aid in via_service:
            self.assertIn(f"{rank}. [id={aid}]", md_text)
        # 文件哈希与产物记录一致
        self.assertEqual(
            hashlib.sha256(md_text.encode("utf-8")).hexdigest(),
            md_art["content_hash"])
        # 入口四：渲染快照与选择历史一致（渲染不换文）
        payload_items = self.notifier.last_payload["items"]
        self.assertEqual([(it["rank"], it["article_id"]) for it in payload_items],
                         via_service)


class TestDeliveryAndRetry(DigestServiceTestBase):
    """WP E：部分失败补发、超时 unknown、并发领取、sent 不重发。"""

    def test_partial_failure_then_targeted_retry(self):
        _seed_articles(self.db, n=4)
        self.notifier.fail_channels = {"feishu"}
        result = self.service.publish_digest(DATE, channels=["email", "feishu"])
        self.assertEqual(result["overall_status"], "partial")
        statuses = {d["channel"]: d["status"] for d in result["deliveries"]}
        self.assertEqual(statuses["email"], "sent")
        self.assertEqual(statuses["feishu"], "failed")
        sent_before = list(self.notifier.sent)
        # 注入解除后只补发失败渠道：email 不重发
        self.notifier.fail_channels.clear()
        retry = self.service.retry_digest_send(result["version_id"], channels=["feishu"])
        self.assertEqual({d["channel"]: d["status"] for d in retry["deliveries"]}["feishu"], "sent")
        self.assertEqual([s["channel"] for s in self.notifier.sent], ["email", "feishu"])

    def test_timeout_marks_unknown_and_retry_skips_it(self):
        _seed_articles(self.db, n=4)
        self.notifier.timeout_channels = {"email"}
        result = self.service.publish_digest(DATE, channels=["email", "feishu"])
        statuses = {d["channel"]: d["status"] for d in result["deliveries"]}
        self.assertEqual(statuses["email"], "unknown")
        retry = self.service.retry_digest_send(result["version_id"])  # 普通补发
        skipped_text = " ".join(e.get("message", "") for e in retry["errors"])
        self.assertIn("unknown", skipped_text)
        self.assertEqual([s["channel"] for s in self.notifier.sent if s["channel"] == "email"], [])

    def test_concurrent_claim_only_one_owner(self):
        _seed_articles(self.db, n=3)
        self.service.publish_digest(DATE)  # 无渠道：只建版本
        self.db.init_digest_sends(self.db.get_latest_published_digest_version(DATE)["id"], ["email"])
        version_id = self.db.get_latest_published_digest_version(DATE)["id"]
        first = self.db.claim_digest_send(version_id, "email", "token-a")
        self.assertEqual(first["claim_token"], "token-a")
        self.assertEqual(first["status"], "sending")
        second = self.db.claim_digest_send(version_id, "email", "token-b")
        self.assertEqual(second["status"], "sending")
        self.assertNotEqual(second["claim_token"], "token-b")  # 领取失败，令牌未覆盖
        # 陈旧令牌无法回写结果
        ok = self.db.finish_digest_send(version_id, "email", "token-b", "sent")
        self.assertFalse(ok)
        ok = self.db.finish_digest_send(version_id, "email", "token-a", "sent")
        self.assertTrue(ok)
        row = self.db.list_digest_sends(version_id)[0]
        self.assertEqual(row["status"], "sent")

    def test_send_without_rendered_artifact_rejected(self):
        _seed_articles(self.db, n=3)
        self.service.notifier = FakeNotifier(self.tmp, fail_render=True)
        result = self.service.publish_digest(DATE, channels=["email"])
        self.assertTrue(any(e["code"] == "RENDER_FAILED" for e in result["errors"]))
        version_id = result["version_id"]
        # 渲染失败后，产物状态为 failed，投递被拒绝
        result = self.service.send_digest(version_id, channels=["email"])
        self.assertTrue(any(e.get("code") == "RENDER_FAILED" for e in result["errors"]))
        self.assertEqual({d["channel"]: d["status"] for d in result["deliveries"]},
                         {"email": "pending"})
        self.assertEqual(result["overall_status"], "partial")


class TestDigestHTTP(unittest.TestCase):
    """WP F：/api/digests* 端点——鉴权、幂等、404、历史只读、legacy 弃用标注。"""

    @classmethod
    def setUpClass(cls):
        import threading
        import urllib.request
        import web_server
        cls.web_server = web_server
        cls.urllib = urllib.request
        tmp = tempfile.mkdtemp()
        cls.tmp = tmp
        cfg_path = Path(tmp) / "config.yaml"
        cfg_path.write_text(f"""
database:
  path: {Path(tmp) / 'http.db'}
llm:
  provider: deepseek
  deepseek:
    api_key: sk-test
    base_url: https://example.invalid
digest:
  daily:
    limit: 5
    category_limits:
      mlip: {{min: 0, max: 10}}
relevance_threshold: 5.0
output:
  output_dir: {Path(tmp) / 'out'}
web:
  api_token: test-token
""", encoding="utf-8")
        ctx = web_server.WebContext(config_path=str(cfg_path))
        ids = ctx.db.save_articles_batch([
            {"doi": f"10.1/h{i:02d}", "title": f"machine learning interatomic potential {i}",
             "pub_date": "2026-09-10", "abstract": "x"} for i in range(1, 9)
        ])
        conn = ctx.db._conn()
        for i, aid in enumerate(ids):
            conn.execute("UPDATE articles SET relevance = ?, created_at = ? WHERE id = ?",
                         (round(9.0 - i * 0.1, 3), "2026-09-10 08:00:00", aid))
        conn.commit()
        server = web_server.ThreadingHTTPServer(("127.0.0.1", 0), web_server.Handler)
        server.context = ctx
        cls.port = server.server_address[1]
        cls.server = server
        cls.article_ids = ids
        t = threading.Thread(target=server.serve_forever, daemon=True)
        t.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        import shutil
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _req(self, path, method="GET", body=None, token=None):
        headers = {}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        if token is not None:
            headers["X-API-Token"] = token
        req = self.urllib.Request(f"http://127.0.0.1:{self.port}{path}",
                                  data=data, headers=headers, method=method)
        try:
            with self.urllib.urlopen(req, timeout=10) as resp:
                return resp.status, json.loads(resp.read())
        except self.urllib.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_publish_requires_token(self):
        code, body = self._req("/api/digests/publish", method="POST",
                               body={"date": DATE})
        self.assertEqual(code, 401)

    def test_publish_and_idempotent_rerun(self):
        # 独立日期 09-12；此前用例已发布 09-10（版本 + legacy 历史），
        # 30 天防重窗口内已发布的文章不再入选——期望值按防重集合动态计算
        other = "2026-09-12"
        excluded = set(self.server.context.db.list_published_digest_article_ids_since(
            "daily", "2026-08-13", other))
        expected = len([aid for aid in self.article_ids if aid not in excluded])
        code, body = self._req("/api/digests/publish", method="POST",
                               body={"date": other}, token="test-token")
        self.assertEqual(code, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(body["selected_count"], min(5, expected))
        self.assertTrue(body["created"])
        code, body2 = self._req("/api/digests/publish", method="POST",
                                body={"date": other}, token="test-token")
        self.assertEqual(code, 200)
        self.assertEqual(body2["version_id"], body["version_id"])  # 同日复用
        self.assertFalse(body2["created"])

    def test_history_and_detail(self):
        code, body = self._req("/api/digests/publish", method="POST",
                               body={"date": DATE}, token="test-token")
        vid = body["version_id"]
        code, hist = self._req("/api/digests?limit=10")
        self.assertEqual(code, 200)
        self.assertTrue(any(it["version_id"] == vid for it in hist["items"]))
        code, detail = self._req(f"/api/digests/{vid}")
        self.assertEqual(code, 200)
        self.assertEqual([it["rank"] for it in detail["items"]], [1, 2, 3, 4, 5])

    def test_detail_not_found(self):
        code, body = self._req("/api/digests/999999")
        self.assertEqual(code, 404)
        self.assertEqual(body["error"]["code"], "NOT_FOUND")

    def test_preview_invalid_date(self):
        code, body = self._req("/api/digests/preview?date=2026-13-40")
        self.assertEqual(code, 400)
        self.assertEqual(body["error"]["code"], "INVALID_DATE")

    def test_legacy_digest_post_marked_deprecated(self):
        code, body = self._req("/api/digest", method="POST",
                               body={"date": DATE}, token="test-token")
        self.assertEqual(code, 200)
        self.assertIn("deprecated", body)
        self.assertNotIn("version_id", body)  # legacy 不创建版本

    def test_reports_route_blocks_traversal(self):
        code, _ = self._req("/reports/daily/../secret.md")
        self.assertEqual(code, 400)

    def test_versioned_report_file_served(self):
        code, body = self._req("/api/digests/publish", method="POST",
                               body={"date": DATE}, token="test-token")
        vid = body["version_id"]
        detail = self._req(f"/api/digests/{vid}")[1]
        md_path = next(a["path"] for a in detail["artifacts"] if a["format"] == "markdown")
        rel = Path(md_path).relative_to(Path(self.tmp) / "out").as_posix()
        req = self.urllib.Request(f"http://127.0.0.1:{self.port}/reports/{rel}")
        with self.urllib.urlopen(req, timeout=10) as resp:
            self.assertEqual(resp.status, 200)
            content = resp.read()
        self.assertIn("日报".encode("utf-8"), content)  # 版本化文件可经 /reports 读取


if __name__ == "__main__":
    unittest.main()
