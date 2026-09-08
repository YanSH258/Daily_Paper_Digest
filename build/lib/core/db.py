"""
db.py - SQLite 数据库模块，用于文章去重和历史记录
"""
import json
import shutil
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
                created_at  TEXT DEFAULT (datetime('now')),
                topic       TEXT,
                starred     INTEGER DEFAULT 0,
                note        TEXT,
                tags        TEXT,
                score_status  TEXT DEFAULT '',
                score_error   TEXT,
                score_model   TEXT,
                score_basis   TEXT,
                relevance_reason TEXT,
                fetch_status  TEXT,
                evidence_level TEXT,
                fetch_source  TEXT,
                network_mode  TEXT,
                access_path   TEXT,
                fulltext_url  TEXT,
                fulltext_text TEXT,
                content_hash  TEXT,
                analysis_status TEXT DEFAULT '',
                analysis_error  TEXT,
                analysis_model  TEXT,
                analysis_prompt_version TEXT,
                analysis_input_hash TEXT,
                analyzed_at   TEXT,
                read_status   TEXT DEFAULT '',
                relevance_feedback TEXT,
                updated_at    TEXT
            );

            CREATE TABLE IF NOT EXISTS daily_reports (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                report_date  TEXT UNIQUE,
                file_path    TEXT,
                total_found  INTEGER,
                total_pushed INTEGER,
                created_at   TEXT DEFAULT (datetime('now')),
                push_results TEXT
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

            CREATE TABLE IF NOT EXISTS task_runs (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id     TEXT UNIQUE,
                trigger     TEXT,
                mode        TEXT,
                date_str    TEXT,
                status      TEXT DEFAULT 'running',
                stage       TEXT,
                stats       TEXT,
                error       TEXT,
                started_at  TEXT DEFAULT (datetime('now')),
                ended_at    TEXT
            );
        """)

        # 启用 WAL 模式（仅文件数据库）
        if self._memory_conn is None:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")

        # 迁移：兼容旧数据库（幂等，可重复执行）
        legacy_columns = [
            ("title_hash", "TEXT"),
            ("analysis", "TEXT"),
            ("topic", "TEXT"),
            ("starred", "INTEGER DEFAULT 0"),
            ("note", "TEXT"),
            ("tags", "TEXT"),
        ]
        stage_columns = [
            # 评分阶段
            ("score_status", "TEXT DEFAULT ''"),
            ("score_error", "TEXT"),
            ("score_model", "TEXT"),
            ("score_basis", "TEXT"),
            ("relevance_reason", "TEXT"),
            # 全文与证据
            ("fetch_status", "TEXT"),
            ("evidence_level", "TEXT"),
            ("fetch_source", "TEXT"),
            ("network_mode", "TEXT"),
            ("access_path", "TEXT"),
            ("fulltext_url", "TEXT"),
            ("fulltext_text", "TEXT"),
            ("content_hash", "TEXT"),
            # 分析阶段
            ("analysis_status", "TEXT DEFAULT ''"),
            ("analysis_error", "TEXT"),
            ("analysis_model", "TEXT"),
            ("analysis_prompt_version", "TEXT"),
            ("analysis_input_hash", "TEXT"),
            ("analyzed_at", "TEXT"),
            # 阅读闭环
            ("read_status", "TEXT DEFAULT ''"),
            ("relevance_feedback", "TEXT"),
            ("updated_at", "TEXT"),
        ]
        self._migrate_columns(conn, "articles", legacy_columns + stage_columns, backup_before=True)
        self._migrate_columns(conn, "daily_reports", [("push_results", "TEXT")], backup_before=False)

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
                "CREATE INDEX IF NOT EXISTS idx_articles_score_status ON articles(score_status)",
                "score_status 索引（失败重试查询）",
            ),
            (
                "CREATE INDEX IF NOT EXISTS idx_articles_read_status ON articles(read_status)",
                "read_status 索引（阅读队列）",
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

    def _migrate_columns(
        self, conn: sqlite3.Connection, table: str, columns: list[tuple[str, str]],
        backup_before: bool = False,
    ) -> None:
        """幂等加列迁移。文件库且确实需要加列时，先自动备份整个数据库。"""
        try:
            existing = {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        except sqlite3.Error as e:
            logger.warning("读取表 %s 结构失败: %s", table, e)
            return
        if not existing:
            return  # 新建表，CREATE TABLE 已包含全部列
        missing = [(c, d) for c, d in columns if c not in existing]
        if not missing:
            return
        if backup_before and self._memory_conn is None:
            try:
                src = Path(self.db_path)
                backup = src.with_name(f"{src.stem}.backup-{datetime.now():%Y%m%d-%H%M%S}{src.suffix}")
                shutil.copy2(src, backup)
                logger.info("检测到旧表结构，迁移前已自动备份数据库: %s", backup)
            except Exception as e:
                logger.error("数据库自动备份失败（继续迁移）: %s", e)
        for col, definition in missing:
            try:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {definition}")
                conn.commit()
                logger.info("表 %s 迁移：新增列 %s", table, col)
            except sqlite3.OperationalError as e:
                logger.debug("列 %s.%s 已存在，跳过迁移: %s", table, col, e)

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

    def save_articles_batch(self, articles: list[dict[str, Any]]) -> list[Optional[int]]:
        """批量保存文章基础记录（单次事务，processed=0 表示尚未完成处理）。

        返回与输入对齐的 id 列表；被唯一约束忽略的重复项对应 id 为 None。
        """
        if not articles:
            return []

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
                "relevance": article.get("relevance"),
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
                 :abstract, :relevance, 0, :title_hash, :analysis, :topic)
        """

        def _run(conn: sqlite3.Connection) -> list[Optional[int]]:
            ids: list[Optional[int]] = []
            for params in params_list:
                cur = conn.execute(sql, params)
                ids.append(cur.lastrowid if cur.rowcount > 0 else None)
            return ids

        try:
            if self._memory_conn is not None:
                with self._memory_lock:
                    ids = _run(self._memory_conn)
                    self._memory_conn.commit()
                    return ids
            conn = self._conn()
            with conn:
                return _run(conn)
        except sqlite3.Error as e:
            logger.error("批量保存文章失败: %s", e)
            return [None] * len(articles)

    # ── 分阶段状态更新（评分 / 全文 / 分析 / 阅读闭环）──────────

    _ARTICLE_FIELD_WHITELIST = {
        "relevance", "relevance_reason", "score_status", "score_error",
        "score_model", "score_basis",
        "fetch_status", "evidence_level", "fetch_source", "network_mode",
        "access_path", "fulltext_url", "fulltext_text", "content_hash",
        "abstract", "analysis", "analysis_status", "analysis_error", "analysis_model",
        "analysis_prompt_version", "analysis_input_hash", "analyzed_at",
        "read_status", "relevance_feedback", "processed", "topic",
    }

    def update_article_fields(self, article_id: int, **fields: Any) -> bool:
        """按白名单更新 articles 的阶段状态字段，并刷新 updated_at。"""
        updates = {k: v for k, v in fields.items() if k in self._ARTICLE_FIELD_WHITELIST}
        if not updates:
            return False
        updates["updated_at"] = datetime.now().isoformat(timespec="seconds")
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        values = list(updates.values()) + [article_id]
        try:
            if self._memory_conn is not None:
                with self._memory_lock:
                    self._memory_conn.execute(
                        f"UPDATE articles SET {set_clause} WHERE id = ?", values)
                    self._memory_conn.commit()
            else:
                conn = self._conn()
                conn.execute(f"UPDATE articles SET {set_clause} WHERE id = ?", values)
                conn.commit()
            return True
        except sqlite3.Error as e:
            logger.error("更新文章字段失败 (id=%s): %s", article_id, e)
            return False

    def get_retry_articles(self, threshold: float, days: int = 7) -> dict[str, list[dict[str, Any]]]:
        """查询需要重试失败阶段的历史文章（评分失败 / 相关但分析失败）。"""
        sql_base = (
            "SELECT * FROM articles "
            "WHERE COALESCE(created_at, datetime('now')) >= datetime('now', ?) AND "
        )
        params = (f"-{int(days)} days",)
        try:
            if self._memory_conn is not None:
                with self._memory_lock:
                    cur_s = self._memory_conn.execute(
                        sql_base + "score_status = 'failed'", params)
                    score_rows = cur_s.fetchall()
                    score_cols = [d[0] for d in cur_s.description]
                    cur_a = self._memory_conn.execute(
                        sql_base + "analysis_status = 'failed' AND COALESCE(relevance, 0) >= ?",
                        params + (threshold,))
                    analysis_rows = cur_a.fetchall()
                    analysis_cols = [d[0] for d in cur_a.description]
                return {
                    "score_failed": [dict(zip(score_cols, r)) for r in score_rows],
                    "analysis_failed": [dict(zip(analysis_cols, r)) for r in analysis_rows],
                }
            conn = self._conn()
            cur_s = conn.execute(sql_base + "score_status = 'failed'", params)
            score_rows = cur_s.fetchall()
            score_cols = [d[0] for d in cur_s.description]
            cur_a = conn.execute(
                sql_base + "analysis_status = 'failed' AND COALESCE(relevance, 0) >= ?",
                params + (threshold,))
            analysis_rows = cur_a.fetchall()
            analysis_cols = [d[0] for d in cur_a.description]
            return {
                "score_failed": [dict(zip(score_cols, r)) for r in score_rows],
                "analysis_failed": [dict(zip(analysis_cols, r)) for r in analysis_rows],
            }
        except sqlite3.Error as e:
            logger.error("查询重试文章失败: %s", e)
            return {"score_failed": [], "analysis_failed": []}

    def get_articles_by_ids(self, ids: list[int]) -> list[dict[str, Any]]:
        if not ids:
            return []
        placeholders = ",".join("?" for _ in ids)
        try:
            if self._memory_conn is not None:
                with self._memory_lock:
                    cur = self._memory_conn.execute(
                        f"SELECT * FROM articles WHERE id IN ({placeholders})", list(ids))
                    cols = [d[0] for d in cur.description]
                    return [dict(zip(cols, r)) for r in cur.fetchall()]
            cur = self._conn().execute(
                f"SELECT * FROM articles WHERE id IN ({placeholders})", list(ids))
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, r)) for r in cur.fetchall()]
        except sqlite3.Error as e:
            logger.error("按 id 查询文章失败: %s", e)
            return []

    def list_articles_by_created_date(self, date_str: str, limit: int = 500) -> list[dict[str, Any]]:
        """按入库日期（created_at 转本地时区的日期部分）列出文章，供今日精选使用。

        created_at 默认值为 UTC 的 datetime('now')，必须转 localtime 再比较，
        否则跨午夜时段的运行会落到"昨天"。
        """
        try:
            sql = (
                "SELECT * FROM articles WHERE date(created_at, 'localtime') = ? "
                "ORDER BY COALESCE(relevance, 0) DESC, id DESC LIMIT ?"
            )
            if self._memory_conn is not None:
                with self._memory_lock:
                    cur = self._memory_conn.execute(sql, (date_str, limit))
                    cols = [d[0] for d in cur.description]
                    return [dict(zip(cols, r)) for r in cur.fetchall()]
            cur = self._conn().execute(sql, (date_str, limit))
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, r)) for r in cur.fetchall()]
        except sqlite3.Error as e:
            logger.error("按日期查询文章失败: %s", e)
            return []

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
        push_results: Optional[dict] = None,
    ) -> None:
        try:
            params = (
                report_date, file_path, total_found, total_pushed,
                json.dumps(push_results, ensure_ascii=False) if push_results is not None else None,
            )
            sql = (
                "INSERT OR REPLACE INTO daily_reports "
                "(report_date, file_path, total_found, total_pushed, push_results) VALUES (?,?,?,?,?)"
            )
            if self._memory_conn is not None:
                with self._memory_lock:
                    self._memory_conn.execute(sql, params)
                    self._memory_conn.commit()
            else:
                conn = self._conn()
                conn.execute(sql, params)
                conn.commit()
        except sqlite3.Error as e:
            logger.error("save_report 失败: %s", e)

    def get_report(self, report_date: str) -> Optional[dict[str, Any]]:
        try:
            if self._memory_conn is not None:
                with self._memory_lock:
                    cur = self._memory_conn.execute(
                        "SELECT * FROM daily_reports WHERE report_date = ?", (report_date,))
                    row = cur.fetchone()
                    cols = [d[0] for d in cur.description] if row else []
            else:
                cur = self._conn().execute(
                    "SELECT * FROM daily_reports WHERE report_date = ?", (report_date,))
                row = cur.fetchone()
                cols = [d[0] for d in cur.description] if row else []
            return dict(zip(cols, row)) if row else None
        except sqlite3.Error as e:
            logger.error("get_report 失败: %s", e)
            return None

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
        """删除订阅源。

        订阅 ID 是永久 ID（页面端可能持有旧列表），删除后不做重排，
        避免旧页面按位置误操作到其他订阅。
        """
        try:
            if self._memory_conn is not None:
                with self._memory_lock:
                    cur = self._memory_conn.execute(
                        "DELETE FROM journals WHERE id = ?", (journal_id,)
                    )
                    self._memory_conn.commit()
                    return cur.rowcount > 0
            conn = self._conn()
            cur = conn.execute("DELETE FROM journals WHERE id = ?", (journal_id,))
            conn.commit()
            return cur.rowcount > 0
        except sqlite3.Error as e:
            logger.error("delete_journal 失败: %s", e)
            return False

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

    # ── 任务运行记录（跨进程持久化）────────────────────────────

    def task_start(self, task_id: str, trigger: str, mode: str, date_str: Optional[str] = None) -> None:
        try:
            sql = ("INSERT OR REPLACE INTO task_runs "
                   "(task_id, trigger, mode, date_str, status, started_at) VALUES (?,?,?,?, 'running', ?)")
            params = (task_id, trigger, mode, date_str, datetime.now().isoformat(timespec="seconds"))
            if self._memory_conn is not None:
                with self._memory_lock:
                    self._memory_conn.execute(sql, params)
                    self._memory_conn.commit()
            else:
                conn = self._conn()
                conn.execute(sql, params)
                conn.commit()
        except sqlite3.Error as e:
            logger.error("task_start 失败: %s", e)

    def task_finish(
        self,
        task_id: str,
        status: str,
        stats: Optional[dict] = None,
        error: Optional[str] = None,
        stage: Optional[str] = None,
    ) -> None:
        try:
            sql = ("UPDATE task_runs SET status = ?, stats = ?, error = ?, stage = ?, "
                   "ended_at = ? WHERE task_id = ?")
            params = (
                status,
                json.dumps(stats, ensure_ascii=False) if stats is not None else None,
                error,
                stage,
                datetime.now().isoformat(timespec="seconds"),
                task_id,
            )
            if self._memory_conn is not None:
                with self._memory_lock:
                    self._memory_conn.execute(sql, params)
                    self._memory_conn.commit()
            else:
                conn = self._conn()
                conn.execute(sql, params)
                conn.commit()
        except sqlite3.Error as e:
            logger.error("task_finish 失败: %s", e)

    def mark_interrupted_tasks(self) -> int:
        """启动时把上次遗留的 running 状态任务标记为 interrupted，返回条数。"""
        try:
            sql = ("UPDATE task_runs SET status = 'interrupted', ended_at = ? "
                   "WHERE status = 'running'")
            params = (datetime.now().isoformat(timespec="seconds"),)
            if self._memory_conn is not None:
                with self._memory_lock:
                    cur = self._memory_conn.execute(sql, params)
                    self._memory_conn.commit()
                    return cur.rowcount
            conn = self._conn()
            cur = conn.execute(sql, params)
            conn.commit()
            return cur.rowcount
        except sqlite3.Error as e:
            logger.error("mark_interrupted_tasks 失败: %s", e)
            return 0

    def list_task_runs(self, limit: int = 20) -> list[dict[str, Any]]:
        try:
            sql = ("SELECT task_id, trigger, mode, date_str, status, stage, stats, error, "
                   "started_at, ended_at FROM task_runs ORDER BY id DESC LIMIT ?")
            if self._memory_conn is not None:
                with self._memory_lock:
                    cur = self._memory_conn.execute(sql, (limit,))
                    cols = [d[0] for d in cur.description]
                    rows = cur.fetchall()
            else:
                cur = self._conn().execute(sql, (limit,))
                cols = [d[0] for d in cur.description]
                rows = cur.fetchall()
            items = []
            for r in rows:
                item = dict(zip(cols, r))
                if item.get("stats"):
                    try:
                        item["stats"] = json.loads(item["stats"])
                    except (TypeError, ValueError):
                        pass
                items.append(item)
            return items
        except sqlite3.Error as e:
            logger.error("list_task_runs 失败: %s", e)
            return []