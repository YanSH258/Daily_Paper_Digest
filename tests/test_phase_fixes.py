"""阶段1-4 回归测试：迁移、失败重试、证据分离、设置保存、引用导出、Web 鉴权。

全部使用临时数据库与模拟 LLM/抓取/推送，不依赖真实 API Key，不联网。
"""
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from core.db import Database  # noqa: E402
from utils.citation import format_article_citation  # noqa: E402
import web_server  # noqa: E402


LEGACY_SCHEMA = """
CREATE TABLE articles (id INTEGER PRIMARY KEY AUTOINCREMENT, doi TEXT UNIQUE, title TEXT,
 journal TEXT, authors TEXT, pub_date TEXT, url TEXT, abstract TEXT, relevance REAL,
 processed INTEGER DEFAULT 0, title_hash TEXT, analysis TEXT, created_at TEXT DEFAULT (datetime('now')));
CREATE TABLE daily_reports (id INTEGER PRIMARY KEY, report_date TEXT UNIQUE, file_path TEXT,
 total_found INTEGER, total_pushed INTEGER, created_at TEXT DEFAULT (datetime('now')));
INSERT INTO articles (doi, title, abstract, relevance) VALUES ('10.1000/legacy', 'Legacy paper', 'Old abstract', 7.5);
"""


class TestMigration(unittest.TestCase):
    def test_legacy_db_backup_and_columns(self):
        tmp = tempfile.mkdtemp()
        old_path = os.path.join(tmp, "legacy.db")
        c = sqlite3.connect(old_path)
        c.executescript(LEGACY_SCHEMA)
        c.commit()
        c.close()

        db = Database(old_path)
        cols = {r[1] for r in db.get_connection().execute("PRAGMA table_info(articles)").fetchall()}
        self.assertLessEqual({"score_status", "fulltext_text", "read_status", "analysis_status"}, cols)
        # 旧数据保留
        row = db.get_connection().execute(
            "SELECT title, score_status FROM articles WHERE doi='10.1000/legacy'").fetchone()
        self.assertEqual(row[0], "Legacy paper")
        self.assertEqual(row[1], "")
        # 自动备份恰好一份
        backups = [f for f in os.listdir(tmp) if "backup" in f]
        self.assertEqual(len(backups), 1)
        # 幂等：再次打开不再新增备份
        db.close()
        db2 = Database(old_path)
        db2.close()
        self.assertEqual(len([f for f in os.listdir(tmp) if "backup" in f]), 1)

    def test_fresh_db_no_backup(self):
        tmp = tempfile.mkdtemp()
        db = Database(os.path.join(tmp, "fresh.db"))
        db.close()
        self.assertEqual([f for f in os.listdir(tmp) if "backup" in f], [])

    def test_journal_id_stable_after_delete(self):
        db = Database(":memory:")
        i1 = db.add_journal(name="A", rss="https://a/rss")
        i2 = db.add_journal(name="B", rss="https://b/rss")
        i3 = db.add_journal(name="C", rss="https://c/rss")
        db.delete_journal(i2)
        ids = [j["id"] for j in db.list_journals()]
        self.assertEqual(ids, [i1, i3])  # 不重排
        j = db.get_journal_by_rss("https://c/rss")
        self.assertEqual(j["id"], i3)


class TestStageStatusAndRetry(unittest.TestCase):
    def test_retry_queue_split(self):
        db = Database(":memory:")
        ids = db.save_articles_batch([
            {"doi": "10.1/s", "title": "ScoreFailed", "journal": "J", "abstract": "x"},
            {"doi": "10.1/a", "title": "AnalysisFailed", "journal": "J", "abstract": "y"},
            {"doi": "10.1/o", "title": "OK", "journal": "J", "abstract": "z"},
        ])
        db.update_article_fields(ids[0], score_status="failed", score_error="timeout")
        db.update_article_fields(ids[1], score_status="ok", relevance=7.0, analysis_status="failed")
        db.update_article_fields(ids[2], score_status="ok", relevance=3.0, analysis_status="skipped")

        r = db.get_retry_articles(threshold=4, days=7)
        self.assertEqual([x["doi"] for x in r["score_failed"]], ["10.1/s"])
        self.assertEqual([x["doi"] for x in r["analysis_failed"]], ["10.1/a"])  # 低分不重试分析


class TestCitation(unittest.TestCase):
    def setUp(self):
        self.a = {
            "title": "Universal Machine Learning Potentials",
            "authors": "Han, Lee, Wang",
            "journal": "Nature",
            "pub_date": "2026-09-08",
            "doi": "10.1038/x",
            "url": "https://nature.com/x",
        }

    def test_bibtex(self):
        s = format_article_citation(self.a, "bibtex")
        self.assertIn("@article{", s)
        self.assertIn("title = {Universal Machine Learning Potentials}", s)
        self.assertIn("doi = {10.1038/x}", s)

    def test_ris(self):
        s = format_article_citation(self.a, "ris")
        self.assertTrue(s.startswith("TY  - JOUR"))
        self.assertIn("DO  - 10.1038/x", s)
        self.assertTrue(s.rstrip().endswith("ER  -"))

    def test_invalid_format(self):
        with self.assertRaises(ValueError):
            format_article_citation(self.a, "xml")


def _make_tmp_config(tmp: str, extra: str = "") -> str:
    cfg = os.path.join(tmp, "config.yaml")
    with open(cfg, "w", encoding="utf-8") as f:
        f.write(f"""
database:
  path: {os.path.join(tmp, 't.db')}
llm:
  provider: deepseek
  deepseek:
    api_key: sk-test
    base_url: https://api.deepseek.com
relevance_threshold: 4
{extra}
""")
    return cfg


class TestSettingsSave(unittest.TestCase):
    """设置保存：Token 留空不改 / 显式清除 / 非法配置不落盘 / 原子写入。"""

    def _ctx(self, cfg_path):
        ctx = web_server.WebContext(config_path=cfg_path)
        return ctx

    def test_token_preserved_when_empty(self):
        tmp = tempfile.mkdtemp()
        cfg = _make_tmp_config(tmp, "web:\n  api_token: secret-123")
        ctx = self._ctx(cfg)
        payload, code = web_server._save_settings(ctx, {"relevance_threshold": 6})
        self.assertEqual(code, 200)
        with open(cfg, encoding="utf-8") as f:
            content = f.read()
        self.assertIn("secret-123", content)  # 保存其他设置不清掉 Token
        self.assertEqual(ctx.api_token, "secret-123")

    def test_token_replace_and_clear(self):
        tmp = tempfile.mkdtemp()
        cfg = _make_tmp_config(tmp)
        ctx = self._ctx(cfg)
        payload, code = web_server._save_settings(ctx, {"web.api_token": "new-token"})
        self.assertEqual(code, 200)
        self.assertEqual(ctx.api_token, "new-token")
        payload, code = web_server._save_settings(ctx, {"web.api_token_clear": True})
        self.assertEqual(code, 200)
        self.assertEqual(ctx.api_token, "")

    def test_invalid_config_not_written(self):
        tmp = tempfile.mkdtemp()
        cfg = _make_tmp_config(tmp)
        ctx = self._ctx(cfg)
        before = open(cfg, encoding="utf-8").read()
        # 把 provider 切到一个没有 api_key 的新提供方 → 校验失败 → 不落盘
        payload, code = web_server._save_settings(ctx, {"llm.provider": "broken"})
        self.assertEqual(code, 400)
        self.assertIn("未保存", payload["error"])
        self.assertEqual(before, open(cfg, encoding="utf-8").read())

    def test_no_tmp_leftover(self):
        tmp = tempfile.mkdtemp()
        cfg = _make_tmp_config(tmp)
        ctx = self._ctx(cfg)
        web_server._save_settings(ctx, {"relevance_threshold": 5})
        self.assertEqual([f for f in os.listdir(tmp) if f.endswith(".tmp")], [])


class TestWebServerAuth(unittest.TestCase):
    """端到端鉴权：Token 开启后写操作必须携带 X-API-Token。"""

    @classmethod
    def setUpClass(cls):
        import threading
        import urllib.request
        import web_server
        cls.web_server = web_server
        cls.urllib = urllib.request
        tmp = tempfile.mkdtemp()
        cls.tmp = tmp
        cfg_path = _make_tmp_config(tmp, "web:\n  api_token: test-token")
        ctx = web_server.WebContext(config_path=cfg_path)
        # 预置一篇文章供写操作测试
        ctx.db.save_articles_batch([
            {"doi": "10.1/x", "title": "Test Article", "journal": "J",
             "abstract": "abs", "relevance": 7.0},
        ])
        server = web_server.ThreadingHTTPServer(("127.0.0.1", 0), web_server.Handler)
        server.context = ctx
        cls.port = server.server_address[1]
        cls.ctx = ctx
        t = threading.Thread(target=server.serve_forever, daemon=True)
        t.start()
        cls.server = server

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

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
                return resp.status, resp.read()
        except self.urllib.HTTPError as e:
            return e.code, e.read()

    def test_get_endpoints_open(self):
        code, _ = self._req("/api/articles")
        self.assertEqual(code, 200)
        code, _ = self._req("/api/today")
        self.assertEqual(code, 200)
        code, _ = self._req("/healthz")
        self.assertEqual(code, 200)

    def test_write_requires_token(self):
        code, _ = self._req("/api/run", method="POST", body={"mode": "default"})
        self.assertEqual(code, 401)
        # journals/test 是写语义接口，也必须受保护
        code, _ = self._req("/api/journals/test", method="POST", body={"rss": "https://x/rss"})
        self.assertEqual(code, 401)
        code, _ = self._req("/api/articles/1/status", method="POST", body={"read_status": "read"})
        self.assertEqual(code, 401)

    def test_write_with_token(self):
        code, body = self._req("/api/articles/1/status", method="POST",
                               body={"read_status": "queued"}, token="test-token")
        self.assertEqual(code, 200)
        self.assertTrue(json.loads(body)["ok"])

    def test_chat_validation(self):
        code, _ = self._req("/api/articles/1/chat", method="POST", body={"question": ""}, token="test-token")
        self.assertEqual(code, 400)


if __name__ == "__main__":
    unittest.main()
