import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from utils import backlog


class BacklogTests(unittest.TestCase):
    def test_old_database_preview_apply_restore_preserves_relations(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'legacy.db'
            with sqlite3.connect(path) as conn:
                conn.executescript('''
                    CREATE TABLE articles(id INTEGER PRIMARY KEY, pub_date TEXT, score_status TEXT,
                        processed INTEGER DEFAULT 0, starred INTEGER DEFAULT 0, note TEXT);
                    CREATE TABLE chat_messages(article_id INTEGER, content TEXT);
                    CREATE TABLE topic_papers(article_id INTEGER, topic_id INTEGER);
                    CREATE TABLE digest_items(article_id INTEGER, snapshot_json TEXT);
                    INSERT INTO articles VALUES(1,'2020-01-01','',0,1,'PRIVATE NOTE');
                    INSERT INTO articles VALUES(2,'','',0,0,NULL);
                    INSERT INTO articles VALUES(3,'2026-09-12','',0,0,NULL);
                    INSERT INTO articles VALUES(4,'2020-01-01','ok',1,0,NULL);
                    INSERT INTO chat_messages VALUES(2,'PRIVATE CHAT');
                    INSERT INTO topic_papers VALUES(1,7);
                    INSERT INTO digest_items VALUES(1,'PRIVATE SNAPSHOT');
                ''')
            before = path.read_bytes()
            preview = backlog.preview(path, '2026-09-13')
            self.assertEqual(path.read_bytes(), before)
            self.assertNotIn('PRIVATE', json.dumps(preview))
            self.assertEqual(preview['change_count'], 3)
            self.assertEqual(preview['counts']['preserve_scored'], 1)
            self.assertTrue(preview['decisions'][1]['relations']['chat_messages'])
            applied = backlog.apply(path, '2026-09-13')
            self.assertEqual(applied['changed'], 3)
            self.assertTrue(Path(applied['backup_path']).exists())
            with sqlite3.connect(applied['backup_path']) as conn:
                self.assertNotIn('processing_status', {r[1] for r in conn.execute('PRAGMA table_info(articles)')})
            with sqlite3.connect(path) as conn:
                self.assertEqual(conn.execute('SELECT processing_status FROM articles WHERE id=1').fetchone()[0], 'outside_window')
                conn.execute("UPDATE articles SET score_status='ok' WHERE id=3")
            self.assertEqual(backlog.restore(path, applied['batch_id']), 2)
            with sqlite3.connect(path) as conn:
                self.assertEqual(conn.execute('SELECT processing_status FROM articles WHERE id=3').fetchone()[0], 'eligible')
                self.assertEqual(conn.execute('SELECT note FROM articles WHERE id=1').fetchone()[0], 'PRIVATE NOTE')
                for table in ('chat_messages', 'topic_papers', 'digest_items'):
                    self.assertEqual(conn.execute(f'SELECT count(*) FROM {table}').fetchone()[0], 1)
            self.assertEqual(backlog.restore(path, applied['batch_id']), 0)
