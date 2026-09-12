"""CLI publication and preview against an isolated database, with no deliveries."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from core.db import Database

REPO = Path(__file__).resolve().parents[1]


class TestDigestCLI(unittest.TestCase):
    def test_fixed_version_publish_and_read_only_preview(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = root / "config.yaml"
            cfg.write_text(f"""
database:
  path: {root / 'test.db'}
llm:
  provider: fake
  fake:
    api_key: fake-key
    base_url: https://example.invalid
output:
  output_dir: {root / 'out'}
  email:
    enabled: false
tracking:
  enabled: false
relevance_threshold: 5
""", encoding="utf-8")
            db = Database(str(root / "test.db"))
            try:
                ids = db.save_articles_batch([{"title": "Machine learning interatomic potential", "doi": "10.test/cli",
                                               "pub_date": "2026-09-10", "abstract": "Fixed abstract"}])
                conn = db._conn()
                conn.execute("UPDATE articles SET relevance=9,created_at='2026-09-10 08:00:00'")
                conn.commit()
                env = dict(os.environ, DPD_DATA_ROOT=tmp, PYTHONDONTWRITEBYTECODE="1",
                           NO_PROXY="127.0.0.1,localhost", no_proxy="127.0.0.1,localhost")
                env["PYTHONPATH"] = os.pathsep.join(str(Path(p).resolve()) for p in sys.path if p)
                def run(flag):
                    return subprocess.run([sys.executable, str(REPO / "src/main.py"), "--config", str(cfg),
                                           "--date", "2026-09-10", flag], cwd=tmp, env=env,
                                          capture_output=True, text=True, check=True, timeout=40)
                def published():
                    output = run("--digest").stdout
                    start = output.index('{\n  "version_id"')
                    return json.JSONDecoder().raw_decode(output[start:])[0]
                first = published()
                second = published()
                self.assertEqual(first["version_id"], second["version_id"])
                self.assertFalse(second["created"])
                self.assertEqual([it["article_id"] for it in first["items"]], ids)
                self.assertEqual(db.list_digest_sends(first["version_id"]), [])
                before = conn.execute("SELECT COUNT(*) FROM digest_entries").fetchone()[0]
                before_dump = list(conn.iterdump())
                run("--digest-dry-run")
                self.assertEqual(list(conn.iterdump()), before_dump)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM digest_entries").fetchone()[0], before)
                self.assertEqual(len(db.list_digest_versions()), 1)
                self.assertTrue((root / "out/digest-dry-run-2026-09-10.txt").is_file())
            finally:
                db.close()
