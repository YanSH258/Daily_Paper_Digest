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

            CREATE TABLE IF NOT EXISTS journals (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                name        TEXT NOT NULL,
                rss         TEXT NOT NULL UNIQUE,
                publisher   TEXT,
                max_articles INTEGER NOT NULL DEFAULT 100,
                enabled     INTEGER NOT NULL DEFAULT 1,
                source      TEXT NOT NULL DEFAULT 'web',
                created_at  TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS chat_messages (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                article_id  INTEGER NOT NULL,
                role        TEXT NOT NULL,
                content     TEXT NOT NULL,
                created_at  TEXT DEFAULT (datetime('now'))
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
            ("topic", "TEXT"),
            ("starred", "INTEGER DEFAULT 0"),
            ("note", "TEXT"),
            ("tags", "TEXT"),
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
                "ON articles (relevance DESC, created_at DESC)",
                "relevance+created_at 复合索引",
            ),
            (
                "CREATE INDEX IF NOT EXISTS idx_articles_starred ON articles(starred)",
                "starred 索引",
            ),
            (
                "CREATE INDEX IF NOT EXISTS idx_articles_topic ON articles(topic)",
                "topic 索引",
            ),
            (
                "CREATE INDEX IF NOT EXISTS idx_chat_messages_article "
                "ON chat_messages(article_id, id)",
                "chat_messages 文章索引",
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
        len_target = len(norm_target)
        if len_target == 0:
            return []

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
            norm_cand = " ".join(candidate.lower().split())
            len_cand = len(norm_cand)
            # 长度过滤：如果长度比例上限都不可能达到 _threshold，直接跳过 difflib
            if min(len_target, len_cand) / max(len_target, len_cand) < _threshold:
                continue

            ratio = difflib.SequenceMatcher(
                None, norm_target, norm_cand
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

    def save_article(self, article: dict[str, Any], check_dup: bool = True) -> bool:
        if check_dup:
            is_dup, reason = self.check_duplicate(article)
            if is_dup:
                logger.debug("跳过重复文章: %s", reason)
                return False

        title = (article.get("title") or "").strip()
        analysis = article.get("analysis")
        topic = article.get("topic")

        try:
            params = {
                "doi": article.get("doi") or None,
                "title": title,
                "journal": article.get("journal", ""),
                "authors": ", ".join(article.get("authors", [])) if isinstance(article.get("authors"), list) else (article.get("authors") or ""),
                "pub_date": article.get("pub_date", ""),
                "url": article.get("url", ""),
                "abstract": article.get("abstract", ""),
                "relevance": article.get("relevance", 0),
                "title_hash": _compute_title_hash(title) if title else None,
                "analysis": analysis,
                "topic": topic,
            }
            sql = """
                INSERT OR IGNORE INTO articles
                    (doi, title, journal, authors, pub_date, url, abstract,
                     relevance, processed, title_hash, analysis, topic)
                VALUES
                    (:doi, :title, :journal, :authors, :pub_date, :url,
                     :abstract, :relevance, 1, :title_hash, :analysis, :topic)
            """
            if self._memory_conn is not None:
                with self._memory_lock:
                    self._memory_conn.execute(sql, params)
                    self._memory_conn.commit()
            else:
                conn = self._conn()
                conn.execute(sql, params)
                conn.commit()
            return True
        except sqlite3.Error as e:
            logger.error("保存文章失败: %s", e)
            return False

    def save_articles_batch(self, articles: list[dict[str, Any]]) -> int:
        """批量保存文章（单次事务提交，极大减少磁盘 I/O）"""
        if not articles:
            return 0

        params_list = []
        for article in articles:
            title = (article.get("title") or "").strip()
            authors = article.get("authors", [])
            authors_str = ", ".join(authors) if isinstance(authors, list) else (authors or "")
            params_list.append({
                "doi": article.get("doi") or None,
                "title": title,
                "journal": article.get("journal", ""),
                "authors": authors_str,
                "pub_date": article.get("pub_date", ""),
                "url": article.get("url", ""),
                "abstract": article.get("abstract", ""),
                "relevance": article.get("relevance", 0),
                "title_hash": _compute_title_hash(title) if title else None,
                "analysis": article.get("analysis"),
                "topic": article.get("topic"),
            })

        sql = """
            INSERT OR IGNORE INTO articles
                (doi, title, journal, authors, pub_date, url, abstract,
                 relevance, processed, title_hash, analysis, topic)
            VALUES
                (:doi, :title, :journal, :authors, :pub_date, :url,
                 :abstract, :relevance, 1, :title_hash, :analysis, :topic)
        """
        try:
            if self._memory_conn is not None:
                with self._memory_lock:
                    cur = self._memory_conn.executemany(sql, params_list)
                    self._memory_conn.commit()
                    return cur.rowcount
            else:
                conn = self._conn()
                with conn:
                    cur = conn.executemany(sql, params_list)
                return cur.rowcount
        except sqlite3.Error as e:
            logger.error("批量保存文章失败: %s", e)
            return 0

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

    # ── 期刊订阅管理 ──────────────────────────────────────────

    def list_journals(self, enabled_only: bool = False) -> list[dict[str, Any]]:
        """列出所有订阅源（Web 端导入的）。"""
        try:
            if self._memory_conn is not None:
                with self._memory_lock:
                    return self._list_journals_impl(self._memory_conn, enabled_only)
            return self._list_journals_impl(self._conn(), enabled_only)
        except sqlite3.Error as e:
            logger.error("list_journals 查询失败: %s", e)
            return []

    def _list_journals_impl(self, conn: sqlite3.Connection, enabled_only: bool) -> list[dict[str, Any]]:
        sql = (
            "SELECT id, name, rss, publisher, max_articles, enabled, source, created_at "
            "FROM journals"
        )
        if enabled_only:
            sql += " WHERE enabled = 1"
        sql += " ORDER BY id"
        cursor = conn.execute(sql)
        cols = [d[0] for d in cursor.description]
        return [dict(zip(cols, row)) for row in cursor.fetchall()]

    def get_journal_by_rss(self, rss: str) -> Optional[dict[str, Any]]:
        try:
            if self._memory_conn is not None:
                with self._memory_lock:
                    return self._get_journal_by_rss_impl(self._memory_conn, rss)
            return self._get_journal_by_rss_impl(self._conn(), rss)
        except sqlite3.Error as e:
            logger.error("get_journal_by_rss 查询失败: %s", e)
            return None

    def _get_journal_by_rss_impl(self, conn: sqlite3.Connection, rss: str) -> Optional[dict[str, Any]]:
        cursor = conn.execute(
            "SELECT id, name, rss, publisher, max_articles, enabled, source, created_at "
            "FROM journals WHERE rss = ?",
            (rss,),
        )
        row = cursor.fetchone()
        if not row:
            return None
        cols = [d[0] for d in cursor.description]
        return dict(zip(cols, row))

    def add_journal(
        self,
        name: str,
        rss: str,
        publisher: Optional[str] = None,
        max_articles: int = 100,
        enabled: bool = True,
        source: str = "web",
    ) -> Optional[int]:
        """新增订阅源，返回新记录 id；若 RSS 已存在或出错返回 None。"""
        try:
            if self._memory_conn is not None:
                with self._memory_lock:
                    return self._add_journal_impl(
                        self._memory_conn, name, rss, publisher, max_articles, enabled, source
                    )
            return self._add_journal_impl(
                self._conn(), name, rss, publisher, max_articles, enabled, source
            )
        except sqlite3.Error as e:
            logger.error("add_journal 失败: %s", e)
            return None

    def _add_journal_impl(
        self,
        conn: sqlite3.Connection,
        name: str,
        rss: str,
        publisher: Optional[str],
        max_articles: int,
        enabled: bool,
        source: str,
    ) -> Optional[int]:
        conn.execute(
            "INSERT OR IGNORE INTO journals "
            "(name, rss, publisher, max_articles, enabled, source) VALUES (?,?,?,?,?,?)",
            (name, rss, publisher, int(max_articles or 100), 1 if enabled else 0, source),
        )
        conn.commit()
        row = conn.execute("SELECT id FROM journals WHERE rss = ?", (rss,)).fetchone()
        return row[0] if row else None

    def update_journal(self, journal_id: int, **fields: Any) -> bool:
        allowed = {"name", "rss", "publisher", "max_articles", "enabled"}
        updates = {k: v for k, v in fields.items() if k in allowed}
        if not updates:
            return False
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        values = list(updates.values())
        values.append(journal_id)
        try:
            if self._memory_conn is not None:
                with self._memory_lock:
                    self._memory_conn.execute(
                        f"UPDATE journals SET {set_clause} WHERE id = ?", values
                    )
                    self._memory_conn.commit()
            else:
                conn = self._conn()
                conn.execute(f"UPDATE journals SET {set_clause} WHERE id = ?", values)
                conn.commit()
            return True
        except sqlite3.Error as e:
            logger.error("update_journal 失败: %s", e)
            return False

    def set_journal_enabled(self, journal_id: int, enabled: bool) -> bool:
        return self.update_journal(journal_id, enabled=1 if enabled else 0)

    def migrate_config_journals(self, journals: list[dict[str, Any]]) -> int:
        """把旧版 config.yaml 里的 journals 列表迁移入库（按 RSS 去重），返回新增数。"""
        added = 0
        for j in journals or []:
            rss = (j.get("rss") or "").strip()
            if not rss or self.get_journal_by_rss(rss) is not None:
                continue
            self.add_journal(
                name=(j.get("name") or "未命名").strip(),
                rss=rss,
                publisher=(j.get("publisher") or "").strip() or "DEFAULT",
                max_articles=int(j.get("max_articles") or 100),
                enabled=True,
                source="config",
            )
            added += 1
        return added

    def delete_journal(self, journal_id: int) -> bool:
        """删除订阅源；确有删除时把剩余订阅重排为 1..N（订阅 id 无外部引用，重排安全）。"""
        try:
            if self._memory_conn is not None:
                with self._memory_lock:
                    cur = self._memory_conn.execute(
                        "DELETE FROM journals WHERE id = ?", (journal_id,)
                    )
                    if cur.rowcount > 0:
                        self._renumber_journals_impl(self._memory_conn)
                    self._memory_conn.commit()
            else:
                conn = self._conn()
                cur = conn.execute("DELETE FROM journals WHERE id = ?", (journal_id,))
                if cur.rowcount > 0:
                    self._renumber_journals_impl(conn)
                conn.commit()
            return True
        except sqlite3.Error as e:
            logger.error("delete_journal 失败: %s", e)
            return False

    @staticmethod
    def _renumber_journals_impl(conn: sqlite3.Connection) -> None:
        """按现有顺序把 journals 重排为 1..N，并同步自增计数器（两步法避免主键冲突）。"""
        row = conn.execute("SELECT COALESCE(MAX(id), 0) FROM journals").fetchone()
        offset = (row[0] if row else 0) + 100000
        conn.execute("UPDATE journals SET id = id + ?", (offset,))
        for new_id, (old_id,) in enumerate(
            conn.execute("SELECT id FROM journals ORDER BY id").fetchall(), start=1
        ):
            conn.execute("UPDATE journals SET id = ? WHERE id = ?", (new_id, old_id))
        count = conn.execute("SELECT COUNT(*) FROM journals").fetchone()[0]
        seq_row = conn.execute(
            "SELECT 1 FROM sqlite_sequence WHERE name = 'journals'"
        ).fetchone()
        if seq_row:
            conn.execute(
                "UPDATE sqlite_sequence SET seq = ? WHERE name = 'journals'", (count,)
            )
        else:
            conn.execute(
                "INSERT INTO sqlite_sequence (name, seq) VALUES ('journals', ?)", (count,)
            )

    # ── 个人文献库：星标 / 笔记 / 标签 ────────────────────────

    @staticmethod
    def _normalize_tags(tags: Any) -> str:
        """把标签列表规范化为去重后的逗号分隔字符串。"""
        if isinstance(tags, str):
            items = tags.split(",")
        else:
            items = list(tags or [])
        seen: list[str] = []
        for t in items:
            t = str(t).strip().lstrip("#")
            if t and t not in seen:
                seen.append(t)
        return ",".join(seen)

    def _update_article_field(self, article_id: int, field: str, value: Any) -> bool:
        try:
            if self._memory_conn is not None:
                with self._memory_lock:
                    self._memory_conn.execute(
                        f"UPDATE articles SET {field} = ? WHERE id = ?", (value, article_id)
                    )
                    self._memory_conn.commit()
            else:
                conn = self._conn()
                conn.execute(f"UPDATE articles SET {field} = ? WHERE id = ?", (value, article_id))
                conn.commit()
            return True
        except sqlite3.Error as e:
            logger.error("更新 articles.%s 失败: %s", field, e)
            return False

    def set_article_star(self, article_id: int, starred: bool) -> bool:
        return self._update_article_field(article_id, "starred", 1 if starred else 0)

    def update_article_note(self, article_id: int, note: str) -> bool:
        return self._update_article_field(article_id, "note", (note or "").strip() or None)

    def update_article_tags(self, article_id: int, tags: Any) -> bool:
        return self._update_article_field(article_id, "tags", self._normalize_tags(tags))

    def list_all_tags(self) -> list[dict[str, Any]]:
        """返回所有自定义标签及其使用次数。"""
        try:
            if self._memory_conn is not None:
                with self._memory_lock:
                    rows = self._memory_conn.execute(
                        "SELECT tags FROM articles WHERE tags IS NOT NULL AND tags != ''"
                    ).fetchall()
            else:
                rows = self._conn().execute(
                    "SELECT tags FROM articles WHERE tags IS NOT NULL AND tags != ''"
                ).fetchall()
            counter: dict[str, int] = {}
            for (raw,) in rows:
                for t in str(raw or "").split(","):
                    t = t.strip()
                    if t:
                        counter[t] = counter.get(t, 0) + 1
            return [{"tag": k, "count": v} for k, v in sorted(counter.items(), key=lambda x: -x[1])]
        except sqlite3.Error as e:
            logger.error("list_all_tags 查询失败: %s", e)
            return []

    # ── 文献对话记录 ─────────────────────────────────────────

    def add_chat_message(self, article_id: int, role: str, content: str) -> Optional[int]:
        try:
            if self._memory_conn is not None:
                with self._memory_lock:
                    cur = self._memory_conn.execute(
                        "INSERT INTO chat_messages (article_id, role, content) VALUES (?,?,?)",
                        (article_id, role, content),
                    )
                    self._memory_conn.commit()
                    return cur.lastrowid
            cur = self._conn().execute(
                "INSERT INTO chat_messages (article_id, role, content) VALUES (?,?,?)",
                (article_id, role, content),
            )
            self._conn().commit()
            return cur.lastrowid
        except sqlite3.Error as e:
            logger.error("add_chat_message 失败: %s", e)
            return None

    def get_chat_messages(self, article_id: int, limit: int = 200) -> list[dict[str, Any]]:
        try:
            if self._memory_conn is not None:
                with self._memory_lock:
                    cursor = self._memory_conn.execute(
                        "SELECT id, role, content, created_at FROM chat_messages "
                        "WHERE article_id = ? ORDER BY id DESC LIMIT ?",
                        (article_id, limit),
                    )
                    cols = [d[0] for d in cursor.description]
                    rows = cursor.fetchall()
            else:
                cursor = self._conn().execute(
                    "SELECT id, role, content, created_at FROM chat_messages "
                    "WHERE article_id = ? ORDER BY id DESC LIMIT ?",
                    (article_id, limit),
                )
                cols = [d[0] for d in cursor.description]
                rows = cursor.fetchall()
            items = [dict(zip(cols, row)) for row in rows]
            items.reverse()
            return items
        except sqlite3.Error as e:
            logger.error("get_chat_messages 查询失败: %s", e)
            return []

    def clear_chat_messages(self, article_id: int) -> bool:
        try:
            if self._memory_conn is not None:
                with self._memory_lock:
                    self._memory_conn.execute(
                        "DELETE FROM chat_messages WHERE article_id = ?", (article_id,)
                    )
                    self._memory_conn.commit()
            else:
                conn = self._conn()
                conn.execute("DELETE FROM chat_messages WHERE article_id = ?", (article_id,))
                conn.commit()
            return True
        except sqlite3.Error as e:
            logger.error("clear_chat_messages 失败: %s", e)
            return False

    def get_article_chat_count(self, article_id: int) -> int:
        try:
            if self._memory_conn is not None:
                with self._memory_lock:
                    row = self._memory_conn.execute(
                        "SELECT COUNT(*) FROM chat_messages WHERE article_id = ?", (article_id,)
                    ).fetchone()
            else:
                row = self._conn().execute(
                    "SELECT COUNT(*) FROM chat_messages WHERE article_id = ?", (article_id,)
                ).fetchone()
            return row[0] if row else 0
        except sqlite3.Error as e:
            logger.error("get_article_chat_count 查询失败: %s", e)
            return 0