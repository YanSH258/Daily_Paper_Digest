"""
db.py - SQLite 数据库模块，用于文章去重和历史记录
"""
import sqlite3
import hashlib
import difflib
import logging
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

# 标题相似度阈值：超过此值则视为重复文章
_TITLE_SIMILARITY_THRESHOLD = 0.80
# 标题相似度匹配的时间窗口（天）
_DEDUP_WINDOW_DAYS = 7


def _compute_title_hash(title: str) -> str:
    """计算标题的归一化哈希值（小写、合并空白后的 MD5）"""
    normalized = " ".join(title.lower().split())
    return hashlib.md5(normalized.encode("utf-8")).hexdigest()


class Database:
    def __init__(self, db_path: str = "chem_daily.db"):
        self.db_path = db_path
        # In-memory databases require a single persistent connection;
        # each new sqlite3.connect(":memory:") call creates a fresh empty DB.
        self._persistent_conn = (
            sqlite3.connect(":memory:") if db_path == ":memory:" else None
        )
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
                    title_hash  TEXT,
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
            # 迁移：为已存在的数据库添加 title_hash 列
            try:
                conn.execute("ALTER TABLE articles ADD COLUMN title_hash TEXT")
            except sqlite3.OperationalError:
                pass  # 列已存在，忽略
            # 迁移：在 url 上创建部分唯一索引（仅对非空 URL 生效）
            try:
                conn.execute(
                    "CREATE UNIQUE INDEX IF NOT EXISTS idx_articles_url "
                    "ON articles (url) WHERE url IS NOT NULL AND url != ''"
                )
            except sqlite3.OperationalError as e:
                logger.warning(f"无法创建 URL 唯一索引（可能存在重复数据）: {e}")
        logger.info(f"数据库初始化完成: {self.db_path}")

    def _conn(self):
        # sqlite3.Connection is a context manager (commits/rolls back on exit,
        # but does not close), so both branches are safe with 'with ... as conn:'.
        if self._persistent_conn is not None:
            return self._persistent_conn
        return sqlite3.connect(self.db_path)

    def is_processed(self, doi: str = "", url: str = "", title: str = "") -> bool:
        """
        多维度检查文章是否已处理过。

        按优先级依次检查：DOI → URL → 标题哈希。
        保持对旧调用方式（仅传入 doi）的向后兼容。
        """
        with self._conn() as conn:
            if doi:
                row = conn.execute(
                    "SELECT id FROM articles WHERE doi = ?", (doi,)
                ).fetchone()
                if row:
                    return True
            if url:
                row = conn.execute(
                    "SELECT id FROM articles WHERE url = ?", (url,)
                ).fetchone()
                if row:
                    return True
            if title:
                h = _compute_title_hash(title)
                row = conn.execute(
                    "SELECT id FROM articles WHERE title_hash = ?", (h,)
                ).fetchone()
                if row:
                    return True
        return False

    def check_duplicate(self, article: dict) -> tuple:
        """
        使用多维度策略检查文章是否为重复。

        检查顺序：
          1. DOI 精确匹配
          2. URL 精确匹配
          3. 标题哈希匹配（归一化后的精确去重）
          4. 标题相似度模糊匹配（difflib，近期文章窗口内）

        返回:
            (is_duplicate: bool, reason: str)
        """
        doi   = (article.get("doi")   or "").strip()
        url   = (article.get("url")   or "").strip()
        title = (article.get("title") or "").strip()

        with self._conn() as conn:
            # 1. DOI 精确匹配
            if doi:
                row = conn.execute(
                    "SELECT id FROM articles WHERE doi = ?", (doi,)
                ).fetchone()
                if row:
                    logger.debug(f"重复检测 [DOI]: {doi}")
                    return True, f"DOI重复: {doi}"

            # 2. URL 精确匹配
            if url:
                row = conn.execute(
                    "SELECT id FROM articles WHERE url = ?", (url,)
                ).fetchone()
                if row:
                    logger.debug(f"重复检测 [URL]: {url}")
                    return True, f"URL重复: {url}"

            # 3. 标题哈希匹配
            if title:
                h = _compute_title_hash(title)
                row = conn.execute(
                    "SELECT id FROM articles WHERE title_hash = ?", (h,)
                ).fetchone()
                if row:
                    logger.debug(f"重复检测 [标题哈希]: {title[:60]}")
                    return True, f"标题精确重复: {title[:60]}"

                # 4. 标题相似度模糊匹配（在近期文章中）
                similar = self.find_similar_articles(
                    title,
                    threshold=_TITLE_SIMILARITY_THRESHOLD,
                    days=_DEDUP_WINDOW_DAYS,
                )
                if similar:
                    matched_title = similar[0]["title"]
                    logger.debug(
                        f"重复检测 [标题相似度]: {title[:60]!r} ≈ {matched_title[:60]!r}"
                    )
                    return True, f"标题相似重复: {title[:60]}"

        return False, ""

    def find_similar_articles(
        self,
        title: str,
        threshold: float = _TITLE_SIMILARITY_THRESHOLD,
        days: int = _DEDUP_WINDOW_DAYS,
    ) -> list:
        """
        在近期文章中查找与给定标题相似的文章。

        Args:
            title:     目标文章标题
            threshold: 相似度阈值（0.0-1.0），默认 0.80
            days:      向前检索的天数，默认 7 天

        Returns:
            匹配的文章列表，每项包含 id/title/doi/url 和 similarity 字段，
            按相似度从高到低排序。
        """
        if not title:
            return []

        norm_target = " ".join(title.lower().split())

        with self._conn() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT id, title, doi, url FROM articles
                WHERE created_at >= datetime('now', ?)
                """,
                (f"-{days} days",),
            ).fetchall()

        results = []
        for row in rows:
            candidate = (row["title"] or "").strip()
            if not candidate:
                continue
            norm_candidate = " ".join(candidate.lower().split())
            ratio = difflib.SequenceMatcher(None, norm_target, norm_candidate).ratio()
            if ratio >= threshold:
                results.append(
                    {
                        "id":         row["id"],
                        "title":      row["title"],
                        "doi":        row["doi"],
                        "url":        row["url"],
                        "similarity": round(ratio, 4),
                    }
                )

        results.sort(key=lambda x: x["similarity"], reverse=True)
        return results

    def save_article(self, article: dict) -> bool:
        """保存文章记录，返回是否为新文章"""
        is_dup, reason = self.check_duplicate(article)
        if is_dup:
            logger.debug(f"跳过重复文章: {reason}")
            return False

        title = (article.get("title") or "").strip()
        try:
            with self._conn() as conn:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO articles
                        (doi, title, journal, authors, pub_date, url, abstract,
                         relevance, processed, title_hash)
                    VALUES
                        (:doi, :title, :journal, :authors, :pub_date, :url,
                         :abstract, :relevance, 1, :title_hash)
                    """,
                    {
                        "doi":        article.get("doi") or None,
                        "title":      title,
                        "journal":    article.get("journal", ""),
                        "authors":    ", ".join(article.get("authors", [])),
                        "pub_date":   article.get("pub_date", ""),
                        "url":        article.get("url", ""),
                        "abstract":   article.get("abstract", ""),
                        "relevance":  article.get("relevance", 0),
                        "title_hash": _compute_title_hash(title) if title else None,
                    },
                )
            return True
        except Exception as e:
            logger.error(f"保存文章失败: {e}")
            return False

    def get_duplicate_count(self) -> int:
        """统计数据库中通过标题哈希检测到的重复文章数量"""
        with self._conn() as conn:
            row = conn.execute(
                """
                SELECT COUNT(*) FROM articles
                WHERE title_hash IN (
                    SELECT title_hash FROM articles
                    WHERE title_hash IS NOT NULL
                    GROUP BY title_hash HAVING COUNT(*) > 1
                )
                """
            ).fetchone()
            return row[0] if row else 0

    def get_dedup_stats(self) -> dict:
        """获取去重统计信息"""
        with self._conn() as conn:
            total = conn.execute(
                "SELECT COUNT(*) FROM articles"
            ).fetchone()[0]
            with_doi = conn.execute(
                "SELECT COUNT(*) FROM articles WHERE doi IS NOT NULL AND doi != ''"
            ).fetchone()[0]
            with_url = conn.execute(
                "SELECT COUNT(*) FROM articles WHERE url IS NOT NULL AND url != ''"
            ).fetchone()[0]
        dup_titles = self.get_duplicate_count()
        return {
            "total_articles":   total,
            "with_doi":         with_doi,
            "with_url":         with_url,
            "duplicate_titles": dup_titles,
        }

    def save_report(self, report_date: str, file_path: str,
                    total_found: int, total_pushed: int):
        """保存每日报告记录"""
        with self._conn() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO daily_reports
                    (report_date, file_path, total_found, total_pushed)
                VALUES (?, ?, ?, ?)
                """,
                (report_date, file_path, total_found, total_pushed),
            )

    def get_recent_articles(self, days: int = 7) -> list:
        """获取最近N天的文章列表"""
        with self._conn() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT * FROM articles
                WHERE created_at >= datetime('now', ?)
                ORDER BY relevance DESC, created_at DESC
                """,
                (f"-{days} days",),
            ).fetchall()
            return [dict(r) for r in rows]
