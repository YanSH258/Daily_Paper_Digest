"""
db.py - SQLite 数据库模块，用于文章去重和历史记录
"""
import sqlite3
import logging
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)


class Database:
    def __init__(self, db_path: str = "chem_daily.db"):
        self.db_path = db_path
        self._init_db()

    def _init_db(self):
        """初始化数据库表结构"""
        with self._conn() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS articles (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    doi         TEXT UNIQUE,
                    title       TEXT,
                    journal     TEXT,
                    authors     TEXT,
                    pub_date    TEXT,
                    url         TEXT,
                    abstract    TEXT,
                    relevance   REAL,
                    processed   INTEGER DEFAULT 0,
                    created_at  TEXT DEFAULT (datetime('now'))
                );

                CREATE TABLE IF NOT EXISTS daily_reports (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    report_date TEXT UNIQUE,
                    file_path   TEXT,
                    total_found INTEGER,
                    total_pushed INTEGER,
                    created_at  TEXT DEFAULT (datetime('now'))
                );
            """)
        logger.info(f"数据库初始化完成: {self.db_path}")

    def _conn(self):
        return sqlite3.connect(self.db_path)

    def is_processed(self, doi: str) -> bool:
        """检查文章是否已处理过"""
        if not doi:
            return False
        with self._conn() as conn:
            row = conn.execute(
                "SELECT id FROM articles WHERE doi = ?", (doi,)
            ).fetchone()
            return row is not None

    def save_article(self, article: dict) -> bool:
        """保存文章记录，返回是否为新文章"""
        doi = article.get("doi", "")
        if doi and self.is_processed(doi):
            return False
        try:
            with self._conn() as conn:
                conn.execute("""
                    INSERT OR IGNORE INTO articles
                        (doi, title, journal, authors, pub_date, url, abstract, relevance, processed)
                    VALUES
                        (:doi, :title, :journal, :authors, :pub_date, :url, :abstract, :relevance, 1)
                """, {
                    "doi":       article.get("doi", ""),
                    "title":     article.get("title", ""),
                    "journal":   article.get("journal", ""),
                    "authors":   ", ".join(article.get("authors", [])),
                    "pub_date":  article.get("pub_date", ""),
                    "url":       article.get("url", ""),
                    "abstract":  article.get("abstract", ""),
                    "relevance": article.get("relevance", 0),
                })
            return True
        except Exception as e:
            logger.error(f"保存文章失败: {e}")
            return False

    def save_report(self, report_date: str, file_path: str,
                    total_found: int, total_pushed: int):
        """保存每日报告记录"""
        with self._conn() as conn:
            conn.execute("""
                INSERT OR REPLACE INTO daily_reports
                    (report_date, file_path, total_found, total_pushed)
                VALUES (?, ?, ?, ?)
            """, (report_date, file_path, total_found, total_pushed))

    def get_recent_articles(self, days: int = 7) -> list:
        """获取最近N天的文章列表"""
        with self._conn() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute("""
                SELECT * FROM articles
                WHERE created_at >= datetime('now', ?)
                ORDER BY relevance DESC, created_at DESC
            """, (f"-{days} days",)).fetchall()
            return [dict(r) for r in rows]
