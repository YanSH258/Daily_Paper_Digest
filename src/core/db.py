"""
db.py - SQLite 数据库模块，用于文章去重和历史记录
"""
import sqlite3
import hashlib
import difflib
import logging
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

_TITLE_SIMILARITY_THRESHOLD = 0.80
_DEDUP_WINDOW_DAYS = 7

_DEFAULT_CONFIG: dict[str, Any] = {
    "title_similarity_threshold": _TITLE_SIMILARITY_THRESHOLD,
    "dedup_window_days": _DEDUP_WINDOW_DAYS,
}


def _compute_title_hash(title: str) -> Optional[str]:
    if not title or not title.strip():
        return None
    normalized = " ".join(title.lower().split())
    return hashlib.md5(normalized.encode("utf-8")).hexdigest()


class Database:
    def __init__(self, db_path: str = "chem_daily.db", config: Optional[dict[str, Any]] = None):
        self.db_path = db_path
        self._config: dict[str, Any] = {**_DEFAULT_CONFIG, **(config or {})}

        # :memory: 模式：共享单连接 + 锁
        self._memory_conn: Optional[sqlite3.Connection] = None
        self._memory_lock: threading.Lock = threading.Lock()

        # 文件模式：线程本地连接
        self._local: threading.local = threading.local()

        if db_path == ":memory:":
            self._memory_conn = sqlite3.connect(
                ":memory:",
                check_same_thread=False,
            )
            self._memory_conn.execute("PRAGMA busy_timeout=5000")

        self._init_db()

    def get_connection(self) -> sqlite3.Connection:
        """返回当前线程的数据库连接（供外部统计/导出工具使用）。"""
        return self._conn()

    def _conn(self) -> sqlite3.Connection:
        if self._memory_conn is not None:
            return self._memory_conn

        conn: Optional[sqlite3.Connection] = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(
                self.db_path,
                check_same_thread=False,
            )
            conn.execute("PRAGMA busy_timeout=5000")
            self._local.conn = conn
        return conn

    def _init_db(self) -> None:
        if self._memory_conn is not None:
            with self._memory_lock:
                self.__run_init(self._memory_conn)
        else:
            conn = self._conn()
            self.__run_init(conn)

    def __run_init(self, conn: sqlite3.Connection) -> None:
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
                title_hash  TEXT,
                analysis    TEXT,
                created_at  TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS daily_reports (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                report_date  TEXT UNIQUE,
                file_path    TEXT,
                total_found  INTEGER,
                total_pushed INTEGER,
                created_at   TEXT DEFAULT (datetime('now'))
            );
        """)

        # 启用 WAL 模式（仅文件数据库）
        if self._memory_conn is None:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")

        # 迁移：兼容旧数据库
        for col, definition in [
            ("title_hash", "TEXT"),
            ("analysis", "TEXT"),
        ]:
            try:
                conn.execute(f"ALTER TABLE articles ADD COLUMN {col} {definition}")
                conn.commit()
            except sqlite3.OperationalError as e:
                logger.debug("列 %s 已存在，跳过迁移: %s", col, e)

        # 创建索引
        index_statements = [
            (
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_articles_url "
                "ON articles (url) WHERE url IS NOT NULL AND url != ''",
                "URL 唯一索引",
            ),
            (
                "CREATE INDEX IF NOT EXISTS idx_articles_title_hash ON articles(title_hash)",
                "title_hash 索引",
            ),
            (
                "CREATE INDEX IF NOT EXISTS idx_articles_created_at ON articles(created_at)",
                "created_at 索引",
            ),
            (
                "CREATE INDEX IF NOT EXISTS idx_articles_relevance_created "
                "ON articles(relevance DESC, created_at DESC)",
                "relevance+created_at 复合索引",
            ),
        ]
        for stmt, desc in index_statements:
            try:
                conn.execute(stmt)
            except sqlite3.OperationalError as e:
                logger.warning("无法创建 %s: %s", desc, e)

        conn.commit()
        logger.info("数据库初始化完成: %s", self.db_path)

    def close(self) -> None:
        """关闭所有数据库连接。"""
        if self._memory_conn is not None:
            with self._memory_lock:
                try:
                    self._memory_conn.close()
                except Exception as e:
                    logger.error("关闭内存连接失败: %s", e)
                self._memory_conn = None
        else:
            conn: Optional[sqlite3.Connection] = getattr(self._local, "conn", None)
            if conn is not None:
                try:
                    conn.close()
                except Exception as e:
                    logger.error("关闭线程连接失败: %s", e)
                self._local.conn = None

    def __enter__(self) -> "Database":
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.close()

    def is_processed(self, doi: str = "", url: str = "", title: str = "") -> bool:
        try:
            if self._memory_conn is not None:
                with self._memory_lock:
                    return self._is_processed_impl(self._memory_conn, doi, url, title)
            else:
                return self._is_processed_impl(self._conn(), doi, url, title)
        except sqlite3.Error as e:
            logger.error("is_processed 查询失败: %s", e)
            return False

    def _is_processed_impl(self, conn: sqlite3.Connection, doi: str, url: str, title: str) -> bool:
        if doi:
            if conn.execute("SELECT id FROM articles WHERE doi = ?", (doi,)).fetchone():
                return True
        if url:
            if conn.execute("SELECT id FROM articles WHERE url = ?", (url,)).fetchone():
                return True
        if title:
            h = _compute_title_hash(title)
            if h and conn.execute("SELECT id FROM articles WHERE title_hash = ?", (h,)).fetchone():
                return True
        return False

    def check_duplicate(self, article: dict[str, Any]) -> tuple[bool, str]:
        try:
            doi = (article.get("doi") or "").strip()
            url = (article.get("url") or "").strip()
            title = (article.get("title") or "").strip()

            if self._memory_conn is not None:
                with self._memory_lock:
                    return self._check_duplicate_impl(self._memory_conn, doi, url, title)
            else:
                return self._check_duplicate_impl(self._conn(), doi, url, title)
        except sqlite3.Error as e:
            logger.error("check_duplicate 查询失败: %s", e)
            return False, ""

    def _check_duplicate_impl(
        self, conn: sqlite3.Connection, doi: str, url: str, title: str
    ) -> tuple[bool, str]:
        if doi:
            if conn.execute("SELECT id FROM articles WHERE doi = ?", (doi,)).fetchone():
                return True, "DOI重复: %s" % doi
        if url:
            if conn.execute("SELECT id FROM articles WHERE url = ?", (url,)).fetchone():
                return True, "URL重复: %s" % url
        if title:
            h = _compute_title_hash(title)
            if h and conn.execute("SELECT id FROM articles WHERE title_hash = ?", (h,)).fetchone():
                return True, "标题精确重复: %s" % title[:60]
            similar = self._find_similar_impl(conn, title)
            if similar:
                return True, "标题相似重复: %s" % title[:60]
        return False, ""

    def find_similar_articles(
        self,
        title: str,
        threshold: Optional[float] = None,
        days: Optional[int] = None,
    ) -> list[dict[str, Any]]:
        try:
            if self._memory_conn is not None:
                with self._memory_lock:
                    return self._find_similar_impl(self._memory_conn, title, threshold, days)
            else:
                return self._find_similar_impl(self._conn(), title, threshold, days)
        except sqlite3.Error as e:
            logger.error("find_similar_articles 查询失败: %s", e)
            return []

    def _find_similar_impl(
        self,
        conn: sqlite3.Connection,
        title: str,
        threshold: Optional[float] = None,
        days: Optional[int] = None,
    ) -> list[dict[str, Any]]:
        if not title:
            return []

        _threshold: float = threshold if threshold is not None else self._config["title_similarity_threshold"]
        _days: int = days if days is not None else self._config["dedup_window_days"]

        norm_target = " ".join(title.lower().split())

        cursor = conn.execute(
            "SELECT id, title, doi, url FROM articles WHERE created_at >= datetime('now', ?)",
            (f"-{_days} days",),
        )
        rows = cursor.fetchall()
        # 获取列名
        col_names = [description[0] for description in cursor.description]
        id_idx = col_names.index("id")
        title_idx = col_names.index("title")
        doi_idx = col_names.index("doi")
        url_idx = col_names.index("url")

        results: list[dict[str, Any]] = []
        for row in rows:
            candidate = (row[title_idx] or "").strip()
            if not candidate:
                continue
            ratio = difflib.SequenceMatcher(
                None, norm_target, " ".join(candidate.lower().split())
            ).ratio()
            if ratio >= _threshold:
                results.append({
                    "id": row[id_idx],
                    "title": row[title_idx],
                    "doi": row[doi_idx],
                    "url": row[url_idx],
                    "similarity": round(ratio, 4),
                })
        results.sort(key=lambda x: x["similarity"], reverse=True)
        return results

    def save_article(self, article: dict[str, Any]) -> bool:
        is_dup, reason = self.check_duplicate(article)
        if is_dup:
            logger.debug("跳过重复文章: %s", reason)
            return False

        title = (article.get("title") or "").strip()
        analysis = article.get("analysis")

        try:
            params = {
                "doi": article.get("doi") or None,
                "title": title,
                "journal": article.get("journal", ""),
                "authors": ", ".join(article.get("authors", [])),
                "pub_date": article.get("pub_date", ""),
                "url": article.get("url", ""),
                "abstract": article.get("abstract", ""),
                "relevance": article.get("relevance", 0),
                "title_hash": _compute_title_hash(title) if title else None,
                "analysis": analysis,
            }
            if self._memory_conn is not None:
                with self._memory_lock:
                    self._memory_conn.execute(
                        """
                        INSERT OR IGNORE INTO articles
                            (doi, title, journal, authors, pub_date, url, abstract,
                             relevance, processed, title_hash, analysis)
                        VALUES
                            (:doi, :title, :journal, :authors, :pub_date, :url,
                             :abstract, :relevance, 1, :title_hash, :analysis)
                        """,
                        params,
                    )
                    self._memory_conn.commit()
            else:
                conn = self._conn()
                conn.execute(
                    """
                    INSERT OR IGNORE INTO articles
                        (doi, title, journal, authors, pub_date, url, abstract,
                         relevance, processed, title_hash, analysis)
                    VALUES
                        (:doi, :title, :journal, :authors, :pub_date, :url,
                         :abstract, :relevance, 1, :title_hash, :analysis)
                    """,
                    params,
                )
                conn.commit()
            return True
        except sqlite3.Error as e:
            logger.error("保存文章失败: %s", e)
            return False

    def get_duplicate_count(self) -> int:
        try:
            if self._memory_conn is not None:
                with self._memory_lock:
                    return self._get_duplicate_count_impl(self._memory_conn)
            else:
                return self._get_duplicate_count_impl(self._conn())
        except sqlite3.Error as e:
            logger.error("get_duplicate_count 查询失败: %s", e)
            return 0

    def _get_duplicate_count_impl(self, conn: sqlite3.Connection) -> int:
        row = conn.execute(
            """SELECT COUNT(*) FROM articles WHERE title_hash IN (
                SELECT title_hash FROM articles
                WHERE title_hash IS NOT NULL
                GROUP BY title_hash HAVING COUNT(*) > 1)"""
        ).fetchone()
        return row[0] if row else 0

    def get_dedup_stats(self) -> dict[str, int]:
        try:
            if self._memory_conn is not None:
                with self._memory_lock:
                    return self._get_dedup_stats_impl(self._memory_conn)
            else:
                return self._get_dedup_stats_impl(self._conn())
        except sqlite3.Error as e:
            logger.error("get_dedup_stats 查询失败: %s", e)
            return {}

    def _get_dedup_stats_impl(self, conn: sqlite3.Connection) -> dict[str, int]:
        total = conn.execute("SELECT COUNT(*) FROM articles").fetchone()[0]
        with_doi = conn.execute(
            "SELECT COUNT(*) FROM articles WHERE doi IS NOT NULL AND doi != ''"
        ).fetchone()[0]
        with_url = conn.execute(
            "SELECT COUNT(*) FROM articles WHERE url IS NOT NULL AND url != ''"
        ).fetchone()[0]
        return {
            "total_articles": total,
            "with_doi": with_doi,
            "with_url": with_url,
            "duplicate_titles": self._get_duplicate_count_impl(conn),
        }

    def save_report(
        self,
        report_date: str,
        file_path: str,
        total_found: int,
        total_pushed: int,
    ) -> None:
        try:
            params = (report_date, file_path, total_found, total_pushed)
            if self._memory_conn is not None:
                with self._memory_lock:
                    self._memory_conn.execute(
                        "INSERT OR REPLACE INTO daily_reports "
                        "(report_date, file_path, total_found, total_pushed) VALUES (?,?,?,?)",
                        params,
                    )
                    self._memory_conn.commit()
            else:
                conn = self._conn()
                conn.execute(
                    "INSERT OR REPLACE INTO daily_reports "
                    "(report_date, file_path, total_found, total_pushed) VALUES (?,?,?,?)",
                    params,
                )
                conn.commit()
        except sqlite3.Error as e:
            logger.error("save_report 失败: %s", e)

    def get_recent_articles(self, days: int = 7) -> list[dict[str, Any]]:
        try:
            if self._memory_conn is not None:
                with self._memory_lock:
                    return self._get_recent_articles_impl(self._memory_conn, days)
            else:
                return self._get_recent_articles_impl(self._conn(), days)
        except sqlite3.Error as e:
            logger.error("get_recent_articles 查询失败: %s", e)
            return []

    def _get_recent_articles_impl(
        self, conn: sqlite3.Connection, days: int
    ) -> list[dict[str, Any]]:
        cursor = conn.execute(
            "SELECT * FROM articles WHERE created_at >= datetime('now', ?) "
            "ORDER BY relevance DESC, created_at DESC",
            (f"-{days} days",),
        )
        col_names = [description[0] for description in cursor.description]
        rows = cursor.fetchall()
        return [dict(zip(col_names, row)) for row in rows]