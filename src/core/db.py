"""
db.py - SQLite 数据库模块，用于文章去重和历史记录
"""
import json
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
            self._memory_conn.execute("PRAGMA foreign_keys=ON")

        try:
            self._init_db()
        except Exception:
            self.close()
            raise

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
            conn.execute("PRAGMA foreign_keys=ON")
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
        schema = """
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
                score_prompt_version TEXT,
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
                analysis_evidence_level TEXT,
                analysis_model  TEXT,
                analysis_prompt_version TEXT,
                analysis_input_hash TEXT,
                analyzed_at   TEXT,
                read_status   TEXT DEFAULT '',
                relevance_feedback TEXT,
                updated_at    TEXT,
                zotero_key    TEXT,
                discovered_via TEXT DEFAULT 'rss',
                sim_prior     REAL,
                cited_count   INTEGER,
                title_zh      TEXT,
                processing_status TEXT DEFAULT 'unreviewed',
                processing_reason TEXT,
                admitted_at TEXT,
                queued_at TEXT,
                date_source TEXT
            );

            CREATE TABLE IF NOT EXISTS daily_reports (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                report_date  TEXT UNIQUE,
                file_path    TEXT,
                total_found  INTEGER,
                total_pushed INTEGER,
                created_at   TEXT DEFAULT (datetime('now')),
                push_results TEXT,
                kind         TEXT DEFAULT 'daily'
            );

            CREATE TABLE IF NOT EXISTS journals (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                name        TEXT NOT NULL,
                rss         TEXT NOT NULL UNIQUE,
                publisher   TEXT,
                max_articles INTEGER NOT NULL DEFAULT 100,
                enabled     INTEGER NOT NULL DEFAULT 1,
                source      TEXT NOT NULL DEFAULT 'web',
                created_at  TEXT DEFAULT (datetime('now')),
                source_type TEXT DEFAULT 'rss',
                query       TEXT,
                last_run    TEXT,
                consecutive_failures INTEGER DEFAULT 0,
                last_error  TEXT
            );

            CREATE TABLE IF NOT EXISTS article_sources (
                article_id INTEGER NOT NULL,
                source_key TEXT NOT NULL,
                seen_at TEXT NOT NULL DEFAULT (datetime('now')),
                PRIMARY KEY(article_id, source_key)
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

            CREATE TABLE IF NOT EXISTS topics (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                name        TEXT NOT NULL,
                research_question TEXT,
                notes       TEXT,
                created_at  TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS topic_papers (
                topic_id    INTEGER NOT NULL,
                article_id  INTEGER NOT NULL,
                note        TEXT,
                added_at    TEXT DEFAULT (datetime('now')),
                PRIMARY KEY (topic_id, article_id)
            );

            CREATE TABLE IF NOT EXISTS citation_edges (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                seed_id     INTEGER NOT NULL,
                citing_id   INTEGER NOT NULL UNIQUE,
                found_at    TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS watched_seeds (
                article_id  INTEGER PRIMARY KEY,
                active      INTEGER NOT NULL DEFAULT 1,
                last_checked_at TEXT
            );

            CREATE TABLE IF NOT EXISTS watch_authors (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                name        TEXT NOT NULL,
                openalex_id TEXT,
                enabled     INTEGER NOT NULL DEFAULT 1,
                last_run    TEXT,
                created_at  TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS journal_metrics (
                name       TEXT PRIMARY KEY,
                full_name  TEXT,
                if_value   REAL,
                cas_zone   INTEGER,
                issn       TEXT,
                updated_at TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS digest_entries (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                digest_date     TEXT NOT NULL,
                digest_type     TEXT NOT NULL DEFAULT 'daily',
                article_id      INTEGER NOT NULL,
                rank            INTEGER,
                category        TEXT,
                relevance_score REAL,
                final_score     REAL,
                selected_reason TEXT,
                created_at      TEXT NOT NULL DEFAULT (datetime('now')),
                UNIQUE(digest_date, digest_type, article_id)
            );

            CREATE TABLE IF NOT EXISTS digest_versions (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                digest_type  TEXT NOT NULL DEFAULT 'daily',
                digest_date  TEXT NOT NULL,
                version      INTEGER NOT NULL,
                status       TEXT NOT NULL DEFAULT 'published',
                request_key  TEXT,
                request_hash TEXT,
                config_json  TEXT NOT NULL DEFAULT '{}',
                config_hash  TEXT NOT NULL DEFAULT '',
                content_hash TEXT NOT NULL DEFAULT '',
                stats_json   TEXT NOT NULL DEFAULT '{}',
                created_at   TEXT NOT NULL DEFAULT (datetime('now')),
                UNIQUE(digest_type, digest_date, version)
            );

            CREATE TABLE IF NOT EXISTS digest_items (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                version_id    INTEGER NOT NULL,
                article_id    INTEGER,
                rank          INTEGER NOT NULL,
                category      TEXT,
                scores_json   TEXT,
                reason        TEXT,
                snapshot_json TEXT NOT NULL DEFAULT '{}',
                UNIQUE(version_id, rank)
            );

            CREATE TABLE IF NOT EXISTS digest_artifacts (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                version_id   INTEGER NOT NULL,
                format       TEXT NOT NULL,
                path         TEXT,
                content_hash TEXT,
                status       TEXT NOT NULL DEFAULT 'pending',
                error        TEXT,
                updated_at   TEXT NOT NULL DEFAULT (datetime('now')),
                UNIQUE(version_id, format)
            );

            CREATE TABLE IF NOT EXISTS digest_sends (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                version_id   INTEGER NOT NULL,
                channel      TEXT NOT NULL,
                status       TEXT NOT NULL DEFAULT 'pending',
                attempts     INTEGER NOT NULL DEFAULT 0,
                request_key  TEXT,
                claim_token  TEXT,
                claimed_at   TEXT,
                last_error   TEXT,
                finished_at  TEXT,
                UNIQUE(version_id, channel)
            );

            CREATE TABLE IF NOT EXISTS highlights (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                article_id  INTEGER NOT NULL,
                text        TEXT NOT NULL,
                note        TEXT,
                created_at  TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS blocked_articles (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                doi         TEXT,
                url         TEXT,
                title_hash  TEXT,
                title       TEXT,
                reason      TEXT DEFAULT 'manual',
                blocked_at  TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS compare_results (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                kind        TEXT NOT NULL DEFAULT 'compare',
                article_ids TEXT,
                content     TEXT NOT NULL,
                model       TEXT,
                created_at  TEXT DEFAULT (datetime('now'))
            );
        """
        # Compare with the target schema before executing *any* migration DDL.
        # The backup API includes committed WAL pages; a filesystem copy does not.
        self._migration_backed_up = False
        if self._memory_conn is None:
            existing_tables = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            if existing_tables:
                with sqlite3.connect(":memory:") as target:
                    target.executescript(schema)
                    target_tables = {r[0] for r in target.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'")}
                    needs_migration = any(
                        not {r[1] for r in target.execute(f'PRAGMA table_info({table})')}
                        <= {r[1] for r in conn.execute(f'PRAGMA table_info({table})')}
                        for table in target_tables)
                if needs_migration:
                    self._backup_before_migration(conn)
        conn.executescript(schema)

        # 启用 WAL 模式（仅文件数据库）
        if self._memory_conn is None:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")

        # 迁移：兼容旧数据库（幂等，可重复执行）
        legacy_columns = [
            ("created_at", "TEXT"),
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
            ("score_prompt_version", "TEXT"),
            ("relevance_reason", "TEXT"),
            ("processing_status", "TEXT DEFAULT 'unreviewed'"),
            ("processing_reason", "TEXT"),
            ("admitted_at", "TEXT"),
            ("queued_at", "TEXT"),
            ("date_source", "TEXT"),
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
            ("analysis_evidence_level", "TEXT"),
            ("analysis_model", "TEXT"),
            ("analysis_prompt_version", "TEXT"),
            ("analysis_input_hash", "TEXT"),
            ("analyzed_at", "TEXT"),
            # 阅读闭环
            ("read_status", "TEXT DEFAULT ''"),
            ("relevance_feedback", "TEXT"),
            ("updated_at", "TEXT"),
            # 研究工作台（Zotero / 追踪 / 推荐）
            ("zotero_key", "TEXT"),
            ("discovered_via", "TEXT DEFAULT 'rss'"),
            ("sim_prior", "REAL"),
            ("cited_count", "INTEGER"),
            ("title_zh", "TEXT"),
        ]
        self._migrate_columns(conn, "digest_versions", [("request_hash", "TEXT")], backup_before=True)
        self._migrate_columns(conn, "articles", legacy_columns + stage_columns, backup_before=True)
        self._migrate_columns(conn, "daily_reports", [
            ("push_results", "TEXT"),
            ("kind", "TEXT DEFAULT 'daily'"),
        ], backup_before=False)
        self._migrate_columns(conn, "journals", [
            ("source_type", "TEXT DEFAULT 'rss'"),
            ("query", "TEXT"),
            ("last_run", "TEXT"),
            ("consecutive_failures", "INTEGER DEFAULT 0"),
            ("last_error", "TEXT"),
        ], backup_before=False)

        # 初始化补缺失期刊，并只升级仍保持上一版内置值的记录；用户修改过的
        # 任一字段都不会匹配，仍由手工编辑或显式重置控制。
        try:
            from utils.journal_metrics import PREVIOUS_SEED_METRICS, SEED_METRICS
            metric_fields = ("full_name", "if_value", "cas_zone", "issn")
            upgrades = []
            additions = []
            for m in SEED_METRICS:
                previous = PREVIOUS_SEED_METRICS.get(m["name"])
                row = conn.execute(
                    "SELECT full_name, if_value, cas_zone, issn FROM journal_metrics WHERE name=?",
                    (m["name"],),
                ).fetchone()
                previous_values = tuple(previous.get(k) for k in metric_fields) if previous else None
                current_values = tuple(m.get(k) for k in metric_fields)
                if row is not None and tuple(row) == previous_values and tuple(row) != current_values:
                    upgrades.append((m["name"], current_values))
                elif row is None:
                    additions.append((m["name"], current_values))
        except Exception as e:  # noqa: BLE001 - 只读扫描失败不阻断其他数据库功能
            logger.warning("读取 journal_metrics 种子失败: %s", e)
        else:
            if upgrades:
                # 备份失败必须阻断升级；_backup_before_migration 会清理残缺备份并抛错。
                self._backup_before_migration(conn)
            try:
                with conn:
                    conn.executemany(
                        "UPDATE journal_metrics SET full_name=?, if_value=?, cas_zone=?, "
                        "issn=?, updated_at=datetime('now') WHERE name=?",
                        [(*values, name) for name, values in upgrades],
                    )
                    conn.executemany(
                        "INSERT INTO journal_metrics (name, full_name, if_value, cas_zone, issn) "
                        "VALUES (?, ?, ?, ?, ?)",
                        [(name, *values) for name, values in additions],
                    )
            except sqlite3.Error as e:
                logger.warning("写入 journal_metrics 种子失败: %s", e)

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
                "CREATE INDEX IF NOT EXISTS idx_digest_entries_date "
                "ON digest_entries(digest_type, digest_date)",
                "digest_entries 日期索引（防重查询）",
            ),
            (
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_digest_versions_request_key "
                "ON digest_versions(request_key) WHERE request_key IS NOT NULL",
                "digest_versions 幂等键唯一索引",
            ),
            (
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_digest_items_version_article "
                "ON digest_items(version_id, article_id) WHERE article_id IS NOT NULL",
                "digest_items 版本内文章唯一",
            ),
            (
                "CREATE INDEX IF NOT EXISTS idx_digest_versions_date "
                "ON digest_versions(digest_type, digest_date)",
                "digest_versions 日期索引",
            ),
            (
                "CREATE INDEX IF NOT EXISTS idx_digest_sends_status "
                "ON digest_sends(status)",
                "digest_sends 状态索引（补发查询）",
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

        integrity_triggers = (
            "CREATE TRIGGER IF NOT EXISTS trg_watch_seed_article_exists "
            "BEFORE INSERT ON watched_seeds WHEN NOT EXISTS "
            "(SELECT 1 FROM articles WHERE id = NEW.article_id) "
            "BEGIN SELECT RAISE(ABORT, 'article does not exist'); END",
            "CREATE TRIGGER IF NOT EXISTS trg_topic_paper_topic_exists "
            "BEFORE INSERT ON topic_papers WHEN NOT EXISTS "
            "(SELECT 1 FROM topics WHERE id = NEW.topic_id) "
            "BEGIN SELECT RAISE(ABORT, 'topic does not exist'); END",
            "CREATE TRIGGER IF NOT EXISTS trg_topic_paper_article_exists "
            "BEFORE INSERT ON topic_papers WHEN NOT EXISTS "
            "(SELECT 1 FROM articles WHERE id = NEW.article_id) "
            "BEGIN SELECT RAISE(ABORT, 'article does not exist'); END",
            "CREATE TRIGGER IF NOT EXISTS trg_highlight_article_exists "
            "BEFORE INSERT ON highlights WHEN NOT EXISTS "
            "(SELECT 1 FROM articles WHERE id = NEW.article_id) "
            "BEGIN SELECT RAISE(ABORT, 'article does not exist'); END",
            "CREATE TRIGGER IF NOT EXISTS trg_chat_article_exists "
            "BEFORE INSERT ON chat_messages WHEN NOT EXISTS "
            "(SELECT 1 FROM articles WHERE id = NEW.article_id) "
            "BEGIN SELECT RAISE(ABORT, 'article does not exist'); END",
            "CREATE TRIGGER IF NOT EXISTS trg_citation_seed_exists "
            "BEFORE INSERT ON citation_edges WHEN NOT EXISTS "
            "(SELECT 1 FROM articles WHERE id = NEW.seed_id) "
            "BEGIN SELECT RAISE(ABORT, 'article does not exist'); END",
            "CREATE TRIGGER IF NOT EXISTS trg_citation_citing_exists "
            "BEFORE INSERT ON citation_edges WHEN NOT EXISTS "
            "(SELECT 1 FROM articles WHERE id = NEW.citing_id) "
            "BEGIN SELECT RAISE(ABORT, 'article does not exist'); END",
            "CREATE TRIGGER IF NOT EXISTS trg_digest_item_version_exists "
            "BEFORE INSERT ON digest_items WHEN NOT EXISTS "
            "(SELECT 1 FROM digest_versions WHERE id = NEW.version_id) "
            "BEGIN SELECT RAISE(ABORT, 'digest version does not exist'); END",
            "CREATE TRIGGER IF NOT EXISTS trg_digest_artifact_version_exists "
            "BEFORE INSERT ON digest_artifacts WHEN NOT EXISTS "
            "(SELECT 1 FROM digest_versions WHERE id = NEW.version_id) "
            "BEGIN SELECT RAISE(ABORT, 'digest version does not exist'); END",
            "CREATE TRIGGER IF NOT EXISTS trg_digest_send_version_exists "
            "BEFORE INSERT ON digest_sends WHEN NOT EXISTS "
            "(SELECT 1 FROM digest_versions WHERE id = NEW.version_id) "
            "BEGIN SELECT RAISE(ABORT, 'digest version does not exist'); END",
            "CREATE TRIGGER IF NOT EXISTS trg_article_delete_dependents "
            "AFTER DELETE ON articles BEGIN "
            "DELETE FROM watched_seeds WHERE article_id = OLD.id; "
            "DELETE FROM chat_messages WHERE article_id = OLD.id; "
            "DELETE FROM highlights WHERE article_id = OLD.id; "
            "DELETE FROM topic_papers WHERE article_id = OLD.id; "
            "DELETE FROM citation_edges WHERE seed_id = OLD.id OR citing_id = OLD.id; "
            "END",
            "CREATE TRIGGER IF NOT EXISTS trg_topic_delete_papers "
            "AFTER DELETE ON topics BEGIN DELETE FROM topic_papers WHERE topic_id = OLD.id; END",
        )
        for trigger in integrity_triggers:
            try:
                conn.execute(trigger)
            except sqlite3.OperationalError as e:
                logger.warning("无法创建完整性触发器: %s", e)
        conn.commit()
        logger.info("数据库初始化完成: %s", self.db_path)

    def _backup_before_migration(self, conn: sqlite3.Connection) -> None:
        if self._memory_conn is not None or getattr(self, "_migration_backed_up", False):
            return
        src = Path(self.db_path)
        backup = src.with_name(f"{src.stem}.backup-{datetime.now():%Y%m%d-%H%M%S-%f}{src.suffix}")
        try:
            with sqlite3.connect(str(backup)) as destination:
                conn.backup(destination)
        except Exception:
            backup.unlink(missing_ok=True)
            raise  # A failed backup must stop migration.
        self._migration_backed_up = True
        logger.info("迁移前已使用 SQLite backup API 备份数据库: %s", backup)

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
        if backup_before:
            self._backup_before_migration(conn)
        for col, definition in missing:
            try:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {definition}")
                conn.commit()
                logger.info("表 %s 迁移：新增列 %s", table, col)
            except sqlite3.OperationalError as e:
                logger.debug("列 %s.%s 已存在，跳过迁移: %s", table, col, e)

    def persist_admission(self, article_ids: list[int] | list[tuple[int, dict[str, Any]]], decisions: Optional[dict[int, dict[str, Any]]] = None, queued_ids: Optional[list[int]] = None, clock: Optional[str] = None) -> int:
        """Persist auditable admission decisions and queue timestamps atomically."""
        now = clock or (datetime.utcnow().isoformat(timespec="seconds") + "Z")
        pairs = article_ids if article_ids and isinstance(article_ids[0], tuple) else [(i, (decisions or {}).get(i, {})) for i in article_ids]
        qset = set(queued_ids or [])
        conn = self._conn()
        try:
            with conn:
                for aid, d in pairs:
                    status = d.get("decision") or d.get("processing_status") or "unreviewed"
                    reason = d.get("reason")
                    source = d.get("date_source")
                    conn.execute("UPDATE articles SET processing_status=?, processing_reason=?, date_source=?, admitted_at=?, queued_at=CASE WHEN ? THEN COALESCE(queued_at, ?) ELSE queued_at END, updated_at=? WHERE id=?", (status, reason, source, now, aid in qset, now, now, aid))
            return len(pairs)
        except sqlite3.Error as e:
            logger.error("保存准入决策失败: %s", e)
            raise

    def get_processing_queue(self, config: Optional[dict[str, Any]] = None, date_str: Optional[str] = None, trial: bool = False) -> dict[str, Any]:
        from processing import build_queue, needs_abstract_rescore, scoring_candidates, validate_budget
        budget = validate_budget(config or {}, trial=trial)
        conn = self._conn()
        cur = conn.execute("""
            SELECT * FROM articles WHERE COALESCE(NULLIF(processing_status,''),'unreviewed')
                IN ('unreviewed','eligible') AND (
                ((score_status IS NULL OR score_status NOT IN ('ok','success')) AND processed=0)
                OR (score_status IN ('ok','success') AND score_basis='title'
                    AND trim(COALESCE(abstract,'')) != '')
            ) ORDER BY COALESCE(queued_at, created_at), id
        """)
        rows = [dict(zip([d[0] for d in cur.description], r)) for r in cur.fetchall()]
        eligible = scoring_candidates(rows, date_str or datetime.now().date().isoformat(), config or {})
        selected = build_queue(eligible, config, budget)
        ids = [r["id"] for r in selected]
        now = datetime.utcnow().isoformat(timespec="seconds") + "Z"
        deferred = set(r["id"] for r in eligible) - set(ids)
        with conn:
            for row in eligible:
                conn.execute("UPDATE articles SET processing_status='eligible', admitted_at=COALESCE(admitted_at, ?), queued_at=COALESCE(queued_at, ?), date_source=COALESCE(date_source, ?) WHERE id=?", (now, now, row.get("date_source"), row["id"]))
                if row["id"] in deferred:
                    conn.execute("UPDATE articles SET processing_reason='budget_deferred', queued_at=COALESCE(queued_at, ?), updated_at=? WHERE id=? AND processing_status='eligible'", (now, now, row["id"]))
            conn.executemany("UPDATE articles SET processing_status='eligible', queued_at=COALESCE(queued_at, ?), updated_at=? WHERE id=? AND processing_status='eligible'", [(now, now, i) for i in ids])
        return {"selected": selected, "score_queue_total": len(eligible),
                "score_deferred": len(deferred), "score_attempted": 0, "budget": budget,
                "rescore_queued": sum(needs_abstract_rescore(row) for row in eligible),
                "rescore_selected": sum(needs_abstract_rescore(row) for row in selected)}

    def get_analysis_queue(self, config: Optional[dict[str, Any]] = None, date_str: Optional[str] = None, trial: bool = False) -> dict[str, Any]:
        from processing import admission_decision, is_score_retry, needs_abstract_rescore, validate_budget
        cfg = config or {}
        budget = validate_budget(cfg, trial)
        conn = self._conn()
        cur = conn.execute("""
            SELECT * FROM articles
            WHERE processing_status='eligible'
              AND score_status IN ('ok','success')
              AND COALESCE(relevance, 0) >= ?
              AND (analysis_status IS NULL OR analysis_status IN ('','failed'))
              AND processed=0
            ORDER BY COALESCE(queued_at, created_at), id
        """, (float(cfg.get("relevance_threshold", 0)),))
        rows = [dict(zip([d[0] for d in cur.description], r)) for r in cur.fetchall()]
        run_date = date_str or datetime.now().date().isoformat()
        rows = [
            row for row in rows
            if not (
                needs_abstract_rescore(row)
                and not is_score_retry(row)
                and admission_decision(row, run_date, cfg)["decision"] == "eligible"
            )
        ]
        return {"selected": rows[:budget], "analysis_queue_total": len(rows), "analysis_deferred": max(0, len(rows)-budget), "budget": budget}

    def count_score_failed(self) -> int:
        """评分失败且仍排队待重试的文章数（跨运行存量，不随单轮预算清空）。"""
        try:
            sql = ("SELECT COUNT(*) FROM articles WHERE score_status = 'failed' "
                   "AND COALESCE(NULLIF(processing_status,''),'unreviewed') IN ('unreviewed','eligible') "
                   "AND processed = 0")
            if self._memory_conn is not None:
                with self._memory_lock:
                    return int(self._memory_conn.execute(sql).fetchone()[0])
            return int(self._conn().execute(sql).fetchone()[0])
        except sqlite3.Error as e:
            logger.error("count_score_failed 失败: %s", e)
            return 0

    def get_admission_reason_counts(self) -> dict[str, int]:
        cur = self._conn().execute("SELECT COALESCE(processing_reason, 'unknown'), COUNT(*) FROM articles GROUP BY COALESCE(processing_reason, 'unknown')")
        return {str(k): int(v) for k, v in cur.fetchall()}

    def record_source_result(self, result: dict[str, Any], persisted: bool) -> Optional[int]:
        conn = self._conn()
        conn.execute("CREATE TABLE IF NOT EXISTS source_sync (id INTEGER PRIMARY KEY AUTOINCREMENT, source_id TEXT, started_at TEXT, window_end TEXT, persisted INTEGER NOT NULL, metadata_json TEXT, created_at TEXT DEFAULT (datetime('now')))")
        success = bool(result.get("success"))
        complete = bool(result.get("complete"))
        truncated = bool(result.get("truncated"))
        eligible_cursor = bool(persisted and success and complete and not truncated)
        cur = conn.execute("INSERT INTO source_sync(source_id,started_at,window_end,persisted,metadata_json) VALUES(?,?,?,?,?)", (result.get("source_id") or result.get("id"), result.get("started_at"), result.get("window_end"), int(eligible_cursor), json.dumps(result, ensure_ascii=False, default=str)))
        # Cursor update and source result metadata share one transaction.
        if eligible_cursor and result.get("source_id") and result.get("window_end"):
            conn.execute("UPDATE journals SET last_run=?, consecutive_failures=0, last_error=NULL WHERE id=?", (result["window_end"], result["source_id"]))
        elif result.get("source_id"):
            err = result.get("error") or ("source fetch incomplete" if success else "source fetch failed")
            conn.execute("UPDATE journals SET consecutive_failures=COALESCE(consecutive_failures,0)+1, last_error=? WHERE id=?", (str(err), result["source_id"]))
        conn.commit()
        return cur.lastrowid

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

    def complete_discovered_metadata(self, article: dict[str, Any]) -> dict[str, Any]:
        """补充再次发现的文章元数据；只允许 DOI 或 URL 精确匹配。"""
        from processing import discovered_metadata_updates
        doi = str(article.get("doi") or "").strip().lower()
        url = str(article.get("url") or "").strip()

        def complete(conn):
            cur = None
            if doi:
                cur = conn.execute("SELECT * FROM articles WHERE lower(trim(doi))=?", (doi,))
                row = cur.fetchone()
            else:
                row = None
            if row is None and url:
                cur = conn.execute("SELECT * FROM articles WHERE url=?", (url,))
                row = cur.fetchone()
            if row is None:
                return {}
            existing = dict(zip([d[0] for d in cur.description], row))
            existing_doi = str(existing.get("doi") or "").strip().lower()
            if doi and existing_doi and existing_doi != doi:
                return {}
            updates = discovered_metadata_updates(existing, article)
            if updates:
                columns = ", ".join(f"{key}=?" for key in updates)
                conn.execute(f"UPDATE articles SET {columns}, updated_at=datetime('now') WHERE id=?",
                             (*updates.values(), existing["id"]))
            return updates

        if self._memory_conn is not None:
            with self._memory_lock, self._memory_conn:
                return complete(self._memory_conn)
        conn = self._conn()
        with conn:
            return complete(conn)

    def record_article_sources(self, article):
        conn = self._conn()
        row = None
        if article.get("doi"):
            row = conn.execute("SELECT id FROM articles WHERE lower(doi)=lower(?)", (article["doi"],)).fetchone()
        if row is None and article.get("url"):
            row = conn.execute("SELECT id FROM articles WHERE url=?", (article["url"],)).fetchone()
        if row is None and article.get("title"):
            row = conn.execute("SELECT id FROM articles WHERE title_hash=?", (_compute_title_hash(article["title"]),)).fetchone()
        if row is None:
            return None
        sources = article.get("_sources") or [article.get("discovered_via") or "rss"]
        with conn:
            conn.executemany("INSERT OR IGNORE INTO article_sources(article_id,source_key) VALUES(?,?)",
                             [(row[0], str(key)) for key in sources])
        return row[0]

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
        # 屏蔽名单优先：被用户清理过的文章不再回流（也不重复消耗 LLM 评分）
        try:
            if doi:
                row = conn.execute(
                    "SELECT 1 FROM blocked_articles WHERE doi IS NOT NULL AND lower(trim(doi)) = ?",
                    (doi.lower(),)).fetchone()
                if row:
                    return True, "屏蔽名单命中 (DOI)"
            if url:
                row = conn.execute(
                    "SELECT 1 FROM blocked_articles WHERE url IS NOT NULL AND url = ?",
                    (url,)).fetchone()
                if row:
                    return True, "屏蔽名单命中 (URL)"
            if title:
                h = _compute_title_hash(title)
                if h and conn.execute(
                    "SELECT 1 FROM blocked_articles WHERE title_hash IS NOT NULL AND title_hash = ?",
                    (h,)).fetchone():
                    return True, "屏蔽名单命中 (标题)"
        except sqlite3.Error as e:
            logger.warning("屏蔽名单查询失败（忽略屏蔽继续）: %s", e)
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

    def save_manual_article(self, article: dict[str, Any]) -> Optional[int]:
        """手动添加单篇：默认高分+收藏，避免被低分清理误删；返回新 id。"""
        title = (article.get("title") or "").strip()
        if not title and not (article.get("doi") or "").strip():
            return None
        authors = article.get("authors")
        if isinstance(authors, list):
            authors_str = ", ".join(a for a in authors if a)
        else:
            authors_str = (authors or "").strip()
        doi = (article.get("doi") or "").strip() or None
        if doi:
            doi = doi.lower().removeprefix("https://doi.org/").removeprefix("http://dx.doi.org/")
        url = (article.get("url") or "").strip()
        if not url and doi:
            url = f"https://doi.org/{doi}"
        params = {
            "doi": doi,
            "title": title,
            "journal": (article.get("journal") or "").strip(),
            "authors": authors_str,
            "pub_date": (article.get("pub_date") or "").strip(),
            "url": url,
            "abstract": (article.get("abstract") or "").strip(),
            "relevance": float(article.get("relevance") if article.get("relevance") is not None else 8.0),
            "title_hash": _compute_title_hash(title) if title else None,
            "topic": article.get("topic"),
            "tags": (article.get("tags") or "").strip(),
            "note": (article.get("note") or "").strip(),
        }
        sql = """
            INSERT INTO articles
                (doi, title, journal, authors, pub_date, url, abstract,
                 relevance, processed, title_hash, topic, tags, note,
                 starred, discovered_via, score_status, score_model, score_basis,
                 relevance_reason, created_at, updated_at)
            VALUES
                (:doi, :title, :journal, :authors, :pub_date, :url, :abstract,
                 :relevance, 1, :title_hash, :topic, :tags, :note,
                 1, 'manual', 'ok', 'manual', 'manual',
                 '手动添加评分（非模型评分）', datetime('now'), datetime('now'))
        """
        try:
            if self._memory_conn is not None:
                with self._memory_lock:
                    cur = self._memory_conn.execute(sql, params)
                    self._memory_conn.commit()
                    return cur.lastrowid
            conn = self._conn()
            cur = conn.execute(sql, params)
            conn.commit()
            return cur.lastrowid
        except sqlite3.IntegrityError as e:
            logger.info("手动添加重复文章: %s", e)
            return None
        except sqlite3.Error as e:
            logger.error("手动添加文章失败: %s", e)
            raise

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
                "processing_status": article.get("processing_status", "unreviewed"),
                "processing_reason": article.get("processing_reason"),
                "admitted_at": article.get("admitted_at"),
                "queued_at": article.get("queued_at"),
                "date_source": article.get("date_source") or article.get("pub_date_source"),
            })

        sql = """
            INSERT OR IGNORE INTO articles
                (doi, title, journal, authors, pub_date, url, abstract,
                 relevance, processed, title_hash, analysis, topic,
                 processing_status, processing_reason, admitted_at, queued_at, date_source)
            VALUES
                (:doi, :title, :journal, :authors, :pub_date, :url,
                 :abstract, :relevance, 0, :title_hash, :analysis, :topic,
                 :processing_status, :processing_reason, :admitted_at, :queued_at, :date_source)
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
        "relevance", "relevance_reason", "score_status", "score_error", "processing_status", "processing_reason", "admitted_at", "queued_at", "date_source",
        "score_model", "score_basis", "score_prompt_version",
        "fetch_status", "evidence_level", "fetch_source", "network_mode",
        "access_path", "fulltext_url", "fulltext_text", "content_hash",
        "abstract", "analysis", "analysis_status", "analysis_error", "analysis_evidence_level", "analysis_model",
        "analysis_prompt_version", "analysis_input_hash", "analyzed_at",
        "read_status", "relevance_feedback", "processed", "topic",
        "zotero_key", "discovered_via", "sim_prior", "cited_count", "title_zh",
        "admitted_at", "queued_at", "date_source",
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

    def get_journal_metric(self, name: str) -> Optional[dict[str, Any]]:
        if not name:
            return None
        try:
            if self._memory_conn is not None:
                with self._memory_lock:
                    row = self._memory_conn.execute(
                        "SELECT name, full_name, if_value, cas_zone FROM journal_metrics WHERE name = ?",
                        (name,),
                    ).fetchone()
            else:
                conn = self._conn()
                row = conn.execute(
                    "SELECT name, full_name, if_value, cas_zone FROM journal_metrics WHERE name = ?",
                    (name,),
                ).fetchone()
        except sqlite3.Error as e:
            logger.warning("查询期刊指标失败 (%s): %s", name, e)
            return None
        if not row:
            return None
        keys = ("name", "full_name", "if_value", "cas_zone")
        return dict(zip(keys, row))

    def list_journal_metrics(self) -> list[dict[str, Any]]:
        try:
            if self._memory_conn is not None:
                with self._memory_lock:
                    rows = self._memory_conn.execute(
                        "SELECT name, full_name, if_value, cas_zone, issn FROM journal_metrics "
                        "ORDER BY (if_value IS NULL), if_value DESC, name"
                    ).fetchall()
            else:
                conn = self._conn()
                rows = conn.execute(
                    "SELECT name, full_name, if_value, cas_zone, issn FROM journal_metrics "
                    "ORDER BY (if_value IS NULL), if_value DESC, name"
                ).fetchall()
        except sqlite3.Error as e:
            logger.warning("列出期刊指标失败: %s", e)
            return []
        keys = ("name", "full_name", "if_value", "cas_zone", "issn")
        return [dict(zip(keys, r)) for r in rows]

    def upsert_journal_metric(
        self,
        name: str,
        full_name: str = "",
        if_value: Optional[float] = None,
        cas_zone: Optional[int] = None,
        issn: str = "",
    ) -> bool:
        name = (name or "").strip()
        if not name:
            return False
        sql = (
            "INSERT INTO journal_metrics (name, full_name, if_value, cas_zone, issn, updated_at) "
            "VALUES (?, ?, ?, ?, ?, datetime('now')) "
            "ON CONFLICT(name) DO UPDATE SET "
            "full_name=excluded.full_name, if_value=excluded.if_value, "
            "cas_zone=excluded.cas_zone, issn=excluded.issn, updated_at=datetime('now')"
        )
        try:
            if self._memory_conn is not None:
                with self._memory_lock:
                    self._memory_conn.execute(
                        sql, (name, full_name or None, if_value, cas_zone, issn or None)
                    )
                    self._memory_conn.commit()
            else:
                conn = self._conn()
                conn.execute(sql, (name, full_name or None, if_value, cas_zone, issn or None))
                conn.commit()
            return True
        except sqlite3.Error as e:
            logger.error("写入期刊指标失败 (%s): %s", name, e)
            return False

    def reseed_journal_metrics(self) -> int:
        """用内置种子表刷新期刊指标，返回写入条数。手工改过的同名刊会被覆盖。"""
        try:
            from utils.journal_metrics import SEED_METRICS
        except Exception as e:  # noqa: BLE001
            logger.error("加载期刊指标种子失败: %s", e)
            return 0
        n = 0
        for m in SEED_METRICS:
            if self.upsert_journal_metric(
                m.get("name", ""),
                m.get("full_name") or "",
                m.get("if_value"),
                m.get("cas_zone"),
                m.get("issn") or "",
            ):
                n += 1
        return n

    def _cleanup_where(self, min_score: float) -> tuple[str, list[Any]]:
        """低分清理条件：默认保护收藏/笔记/标签/Zotero/阅读状态/反馈。"""
        where = [
            # 未评分（relevance IS NULL）的文章不是"低分"，是待评分——绝不清理
            "relevance IS NOT NULL",
            "COALESCE(relevance, 0) < ?",
            "COALESCE(starred, 0) = 0",
            "(note IS NULL OR trim(note) = '')",
            "(tags IS NULL OR trim(tags) = '')",
            "(zotero_key IS NULL OR trim(zotero_key) = '')",
            "(read_status IS NULL OR trim(read_status) = '')",
            "(relevance_feedback IS NULL OR trim(relevance_feedback) = '')",
        ]
        return " AND ".join(where), [float(min_score)]

    def count_low_relevance(self, min_score: float) -> int:
        clause, params = self._cleanup_where(min_score)
        try:
            if self._memory_conn is not None:
                with self._memory_lock:
                    return self._memory_conn.execute(
                        f"SELECT COUNT(*) FROM articles WHERE {clause}", params
                    ).fetchone()[0]
            conn = self._conn()
            return conn.execute(
                f"SELECT COUNT(*) FROM articles WHERE {clause}", params
            ).fetchone()[0]
        except sqlite3.Error as e:
            logger.error("统计低分文献失败: %s", e)
            return 0

    def sample_low_relevance(self, min_score: float, limit: int = 8) -> list[dict[str, Any]]:
        clause, params = self._cleanup_where(min_score)
        sql = (
            f"SELECT id, title, journal, relevance FROM articles WHERE {clause} "
            "ORDER BY COALESCE(relevance, 0) ASC, id ASC LIMIT ?"
        )
        try:
            if self._memory_conn is not None:
                with self._memory_lock:
                    rows = self._memory_conn.execute(sql, params + [limit]).fetchall()
            else:
                conn = self._conn()
                rows = conn.execute(sql, params + [limit]).fetchall()
        except sqlite3.Error as e:
            logger.error("抽样低分文献失败: %s", e)
            return []
        keys = ("id", "title", "journal", "relevance")
        return [dict(zip(keys, r)) for r in rows]

    def delete_low_relevance(self, min_score: float) -> int:
        """删除低分且无用户痕迹的文章；删除前把标识写入屏蔽名单，防止每日抓取回流。"""
        clause, params = self._cleanup_where(min_score)
        try:
            self.block_articles_by_clause(clause, params, reason="low_score_cleanup")
        except sqlite3.Error as e:
            logger.warning("清理前记录屏蔽名单失败（继续删除）: %s", e)
        sql = f"DELETE FROM articles WHERE {clause}"
        try:
            if self._memory_conn is not None:
                with self._memory_lock:
                    cur = self._memory_conn.execute(sql, params)
                    self._memory_conn.commit()
                    return cur.rowcount or 0
            conn = self._conn()
            cur = conn.execute(sql, params)
            conn.commit()
            return cur.rowcount or 0
        except sqlite3.Error as e:
            logger.error("删除低分文献失败: %s", e)
            raise

    def delete_articles_by_ids(self, ids: list[int]) -> int:
        """按 id 手动删除文章，并清理关联表。返回实际删除条数。"""
        ids = [int(i) for i in ids or [] if str(i).isdigit()]
        if not ids:
            return 0
        ph = ",".join("?" for _ in ids)
        related = (
            "chat_messages", "highlights", "topic_papers", "digest_entries",
            "watched_seeds", "citation_edges",
        )
        try:
            if self._memory_conn is not None:
                with self._memory_lock:
                    conn = self._memory_conn
                    try:
                        for table in related:
                            if table == "citation_edges":
                                conn.execute(
                                    f"DELETE FROM citation_edges WHERE seed_id IN ({ph}) OR citing_id IN ({ph})",
                                    ids + ids,
                                )
                            else:
                                conn.execute(f"DELETE FROM {table} WHERE article_id IN ({ph})", ids)
                        cur = conn.execute(f"DELETE FROM articles WHERE id IN ({ph})", ids)
                        deleted = cur.rowcount or 0
                        conn.commit()
                        return deleted
                    except Exception:
                        # 关联清理与文章删除必须同成功或同回滚，否则后续提交
                        # 会把已删除的关联数据固化成永久丢失
                        conn.rollback()
                        raise
            conn = self._conn()
            try:
                for table in related:
                    if table == "citation_edges":
                        conn.execute(
                            f"DELETE FROM citation_edges WHERE seed_id IN ({ph}) OR citing_id IN ({ph})",
                            ids + ids,
                        )
                    else:
                        conn.execute(f"DELETE FROM {table} WHERE article_id IN ({ph})", ids)
                cur = conn.execute(f"DELETE FROM articles WHERE id IN ({ph})", ids)
                deleted = cur.rowcount or 0
                conn.commit()
                return deleted
            except Exception:
                conn.rollback()
                raise
        except sqlite3.Error as e:
            logger.error("按 id 删除文章失败: %s", e)
            raise

    def list_low_relevance_rows(self, min_score: float) -> list[dict[str, Any]]:
        """导出用：返回将被清理的文章完整关键字段。"""
        clause, params = self._cleanup_where(min_score)
        sql = (
            "SELECT id, doi, title, journal, authors, pub_date, url, abstract, "
            "relevance, topic, tags, note, created_at "
            f"FROM articles WHERE {clause} ORDER BY COALESCE(relevance, 0) ASC, id ASC"
        )
        try:
            if self._memory_conn is not None:
                with self._memory_lock:
                    rows = self._memory_conn.execute(sql, params).fetchall()
            else:
                conn = self._conn()
                rows = conn.execute(sql, params).fetchall()
        except sqlite3.Error as e:
            logger.error("导出低分文献失败: %s", e)
            return []
        keys = (
            "id", "doi", "title", "journal", "authors", "pub_date", "url",
            "abstract", "relevance", "topic", "tags", "note", "created_at",
        )
        return [dict(zip(keys, r)) for r in rows]

    def get_retry_articles(self, threshold: float, days: int = 7) -> dict[str, list[dict[str, Any]]]:
        """查询需要重试/补齐的历史文章。

        评分重试：'failed'（模型失败）**或** "空且 processed=0"（任务被打断、
        入库后从未评分的僵尸文章，如服务重启杀死运行中的任务）。
        分析重试：'failed' 或 "空且 processed=0"，且相关性已达阈值。
        """
        sql_base = (
            "SELECT * FROM articles "
            "WHERE COALESCE(created_at, datetime('now')) >= datetime('now', ?) AND "
        )
        params = (f"-{int(days)} days",)
        score_cond = ("(COALESCE(processing_status, 'eligible') = 'eligible' AND "
                      "(score_status = 'failed' OR (COALESCE(score_status, '') = '' AND processed = 0)))")
        analysis_cond = ("(COALESCE(processing_status, 'eligible') = 'eligible' AND "
                         "(analysis_status = 'failed' OR COALESCE(analysis_status, '') = '') "
                         "AND processed = 0 AND COALESCE(relevance, 0) >= ?)")
        try:
            if self._memory_conn is not None:
                with self._memory_lock:
                    cur_s = self._memory_conn.execute(
                        sql_base + score_cond, params)
                    score_rows = cur_s.fetchall()
                    score_cols = [d[0] for d in cur_s.description]
                    cur_a = self._memory_conn.execute(
                        sql_base + analysis_cond, params + (threshold,))
                    analysis_rows = cur_a.fetchall()
                    analysis_cols = [d[0] for d in cur_a.description]
                return {
                    "score_failed": [dict(zip(score_cols, r)) for r in score_rows],
                    "analysis_failed": [dict(zip(analysis_cols, r)) for r in analysis_rows],
                }
            conn = self._conn()
            cur_s = conn.execute(sql_base + score_cond, params)
            score_rows = cur_s.fetchall()
            score_cols = [d[0] for d in cur_s.description]
            cur_a = conn.execute(
                sql_base + analysis_cond, params + (threshold,))
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
        kind: str = "daily",
    ) -> None:
        try:
            params = (
                report_date, file_path, total_found, total_pushed,
                json.dumps(push_results, ensure_ascii=False) if push_results is not None else None,
                kind,
            )
            sql = (
                "INSERT OR REPLACE INTO daily_reports "
                "(report_date, file_path, total_found, total_pushed, push_results, kind) VALUES (?,?,?,?,?,?)"
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
            "SELECT id, name, rss, publisher, max_articles, enabled, source, "
            "source_type, query, last_run, last_error, created_at "
            "FROM journals"
        )
        if enabled_only:
            sql += " WHERE enabled = 1"
        sql += " ORDER BY id"
        cursor = conn.execute(sql)
        cols = [d[0] for d in cursor.description]
        journals = [dict(zip(cols, row)) for row in cursor.fetchall()]

        # 聚合每个期刊的入库文章总数和最近一次抓取数
        for j in journals:
            jid = j["id"]
            jname = j.get("name") or ""
            try:
                # 统计入库文章数（优先通过 article_sources 关联，兼容 journal 字段名称）
                row = conn.execute(
                    "SELECT COUNT(DISTINCT a.id) FROM articles a "
                    "LEFT JOIN article_sources s ON a.id = s.article_id "
                    "WHERE s.source_key IN (?, ?) OR a.journal = ?",
                    (f"journal:{jid}", f"journal:{jname}", jname)
                ).fetchone()
                j["article_count"] = row[0] if row else 0
            except Exception:
                j["article_count"] = 0

            try:
                # 查询 source_sync 最新抓取记录中的篇数
                sync_row = conn.execute(
                    "SELECT metadata_json FROM source_sync "
                    "WHERE source_id IN (?, ?) ORDER BY id DESC LIMIT 1",
                    (str(jid), jname)
                ).fetchone()
                if sync_row and sync_row[0]:
                    meta = json.loads(sync_row[0])
                    j["last_fetch_count"] = meta.get("returned_count", meta.get("raw_count", 0))
                else:
                    j["last_fetch_count"] = 0
            except Exception:
                j["last_fetch_count"] = 0

        return journals


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
        source_type: str = "rss",
        query: str = "",
    ) -> Optional[int]:
        """新增订阅源，返回新记录 id；若 RSS 已存在或出错返回 None。

        source_type: rss / arxiv / openalex。rss 列统一存规范化的真实
        抓取 URL 作为去重键；arxiv/openalex 的检索式存 query。
        """
        try:
            if self._memory_conn is not None:
                with self._memory_lock:
                    return self._add_journal_impl(
                        self._memory_conn, name, rss, publisher, max_articles, enabled, source,
                        source_type=source_type, query=query,
                    )
            return self._add_journal_impl(
                self._conn(), name, rss, publisher, max_articles, enabled, source,
                source_type=source_type, query=query,
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
        source_type: str = "rss",
        query: str = "",
    ) -> Optional[int]:
        conn.execute(
            "INSERT OR IGNORE INTO journals "
            "(name, rss, publisher, max_articles, enabled, source, source_type, query) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (name, rss, publisher, int(max_articles or 100), 1 if enabled else 0, source,
             (source_type or "rss").strip() or "rss", (query or "").strip()),
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
            if cur.rowcount > 0:
                self.renumber_journals()
            return cur.rowcount > 0
        except sqlite3.Error as e:
            logger.error("delete_journal 失败: %s", e)
            return False

    def renumber_journals(self) -> None:
        """把订阅 id 压缩重排为 1..N（按当前顺序），并同步自增计数器。"""
        conn = self._conn()
        try:
            (max_id,) = conn.execute("SELECT COALESCE(MAX(id), 0) FROM journals").fetchone()
            offset = max_id + 1
            conn.execute("UPDATE journals SET id = id + ?", (offset,))
            for new_id, (old_id,) in enumerate(
                conn.execute("SELECT id FROM journals ORDER BY id").fetchall(), start=1
            ):
                conn.execute("UPDATE journals SET id = ? WHERE id = ?", (new_id, old_id))
            (count,) = conn.execute("SELECT COUNT(*) FROM journals").fetchone()
            if conn.execute(
                "SELECT 1 FROM sqlite_master WHERE name = 'sqlite_sequence'"
            ).fetchone():
                conn.execute(
                    "UPDATE sqlite_sequence SET seq = ? WHERE name = 'journals'", (count,))
            conn.commit()
        except sqlite3.Error as e:
            conn.rollback()
            logger.error("renumber_journals 失败: %s", e)

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
            if not self._exists("articles", "id", article_id):
                return None
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
    # ── 推荐质量：反馈样例 ─────────────────────────────────────

    def get_feedback_examples(self, n: int = 3) -> dict[str, list[dict[str, str]]]:
        """取最近的喜欢/不喜欢样例，注入评分 prompt 作 few-shot。"""
        out = {"liked": [], "disliked": []}
        try:
            sql = ("SELECT title, topic, relevance_feedback, COALESCE(starred,0) AS starred "
                   "FROM articles WHERE relevance_feedback = ? ORDER BY updated_at DESC LIMIT ?")
            if self._memory_conn is not None:
                with self._memory_lock:
                    d_rows = self._memory_conn.execute(sql, ("irrelevant", n)).fetchall()
                    l_rows = self._memory_conn.execute(
                        "SELECT title, topic, relevance_feedback, COALESCE(starred,0) AS starred FROM articles "
                        "WHERE relevance_feedback = 'relevant' OR starred = 1 "
                        "ORDER BY updated_at DESC LIMIT ?", (n,)).fetchall()
            else:
                conn = self._conn()
                d_rows = conn.execute(sql, ("irrelevant", n)).fetchall()
                l_rows = conn.execute(
                    "SELECT title, topic, relevance_feedback, COALESCE(starred,0) AS starred FROM articles "
                    "WHERE relevance_feedback = 'relevant' OR starred = 1 "
                    "ORDER BY updated_at DESC LIMIT ?", (n,)).fetchall()
            for r in d_rows:
                out["disliked"].append({"title": r[0], "topic": r[1] or ""})
            for r in l_rows:
                out["liked"].append({"title": r[0], "topic": r[1] or ""})
            return out
        except sqlite3.Error as e:
            logger.error("get_feedback_examples 失败: %s", e)
            return out

    # ── Zotero 关联 ─────────────────────────────────────────────

    def get_article_by_zotero_key(self, key: str) -> Optional[int]:
        try:
            sql = "SELECT id FROM articles WHERE zotero_key = ?"
            if self._memory_conn is not None:
                with self._memory_lock:
                    row = self._memory_conn.execute(sql, (key,)).fetchone()
            else:
                row = self._conn().execute(sql, (key,)).fetchone()
            return row[0] if row else None
        except sqlite3.Error as e:
            logger.error("get_article_by_zotero_key 失败: %s", e)
            return None

    # ── 引文追踪 ────────────────────────────────────────────────

    def _exists(self, table: str, key: str, value: Any) -> bool:
        if table not in {"articles", "topics"} or key != "id":
            raise ValueError("unsupported existence check")
        conn = self._memory_conn if self._memory_conn is not None else self._conn()
        return conn.execute(f"SELECT 1 FROM {table} WHERE id = ?", (value,)).fetchone() is not None

    def integrity_report(self) -> dict[str, int]:
        """Report legacy orphan rows without mutating user data."""
        conn = self._memory_conn if self._memory_conn is not None else self._conn()
        checks = {
            "watched_seeds": "SELECT COUNT(*) FROM watched_seeds s LEFT JOIN articles a ON a.id=s.article_id WHERE a.id IS NULL",
            "topic_papers_topic": "SELECT COUNT(*) FROM topic_papers p LEFT JOIN topics t ON t.id=p.topic_id WHERE t.id IS NULL",
            "topic_papers_article": "SELECT COUNT(*) FROM topic_papers p LEFT JOIN articles a ON a.id=p.article_id WHERE a.id IS NULL",
            "highlights": "SELECT COUNT(*) FROM highlights h LEFT JOIN articles a ON a.id=h.article_id WHERE a.id IS NULL",
            "chat_messages": "SELECT COUNT(*) FROM chat_messages m LEFT JOIN articles a ON a.id=m.article_id WHERE a.id IS NULL",
            "citation_seed": "SELECT COUNT(*) FROM citation_edges e LEFT JOIN articles a ON a.id=e.seed_id WHERE a.id IS NULL",
            "citation_citing": "SELECT COUNT(*) FROM citation_edges e LEFT JOIN articles a ON a.id=e.citing_id WHERE a.id IS NULL",
        }
        return {name: int(conn.execute(sql).fetchone()[0]) for name, sql in checks.items()}

    def set_watch_seed(self, article_id: int, active: bool) -> bool:
        try:
            if not self._exists("articles", "id", article_id):
                return False
            sql = ("INSERT INTO watched_seeds (article_id, active) VALUES (?, ?) "
                   "ON CONFLICT(article_id) DO UPDATE SET active = excluded.active")
            if self._memory_conn is not None:
                with self._memory_lock:
                    self._memory_conn.execute(sql, (article_id, 1 if active else 0))
                    self._memory_conn.commit()
            else:
                conn = self._conn()
                conn.execute(sql, (article_id, 1 if active else 0))
                conn.commit()
            return True
        except sqlite3.Error as e:
            logger.error("set_watch_seed 失败: %s", e)
            return False

    def get_watched_seeds(self) -> list[dict[str, Any]]:
        try:
            sql = ("SELECT a.id, a.doi, a.title, a.journal, a.created_at, s.active, s.last_checked_at "
                   "FROM watched_seeds s JOIN articles a ON a.id = s.article_id "
                   "WHERE s.active = 1 ORDER BY s.article_id")
            if self._memory_conn is not None:
                with self._memory_lock:
                    cur = self._memory_conn.execute(sql)
                    cols = [d[0] for d in cur.description]
                    return [dict(zip(cols, r)) for r in cur.fetchall()]
            cur = self._conn().execute(sql)
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, r)) for r in cur.fetchall()]
        except sqlite3.Error as e:
            logger.error("get_watched_seeds 失败: %s", e)
            return []

    def is_seed_watched(self, article_id: int) -> bool:
        try:
            sql = "SELECT active FROM watched_seeds WHERE article_id = ?"
            if self._memory_conn is not None:
                with self._memory_lock:
                    row = self._memory_conn.execute(sql, (article_id,)).fetchone()
            else:
                row = self._conn().execute(sql, (article_id,)).fetchone()
            return bool(row and row[0])
        except sqlite3.Error as e:
            logger.error("is_seed_watched 失败: %s", e)
            return False

    def mark_seed_checked(self, article_id: int) -> None:
        try:
            sql = ("INSERT INTO watched_seeds (article_id, active, last_checked_at) "
                   "VALUES (?, 1, ?) ON CONFLICT(article_id) DO UPDATE SET last_checked_at = excluded.last_checked_at")
            now = datetime.now().isoformat(timespec="seconds")
            if self._memory_conn is not None:
                with self._memory_lock:
                    self._memory_conn.execute(sql, (article_id, now))
                    self._memory_conn.commit()
            else:
                conn = self._conn()
                conn.execute(sql, (article_id, now))
                conn.commit()
        except sqlite3.Error as e:
            logger.error("mark_seed_checked 失败: %s", e)

    def add_citation_edge(self, seed_id: int, citing_id: int) -> bool:
        try:
            if not self._exists("articles", "id", seed_id) or not self._exists("articles", "id", citing_id):
                return False
            sql = "INSERT OR IGNORE INTO citation_edges (seed_id, citing_id) VALUES (?, ?)"
            if self._memory_conn is not None:
                with self._memory_lock:
                    self._memory_conn.execute(sql, (seed_id, citing_id))
                    self._memory_conn.commit()
            else:
                conn = self._conn()
                conn.execute(sql, (seed_id, citing_id))
                conn.commit()
            return True
        except sqlite3.Error as e:
            logger.error("add_citation_edge 失败: %s", e)
            return False

    def get_citation_edges_since(self, days: int = 7) -> list[dict[str, Any]]:
        try:
            sql = ("SELECT e.seed_id, e.citing_id, e.found_at, sa.title AS seed_title, "
                   "ca.title AS citing_title, ca.doi AS citing_doi, ca.url AS citing_url, "
                   "ca.relevance AS citing_relevance "
                   "FROM citation_edges e "
                   "JOIN articles sa ON sa.id = e.seed_id "
                   "JOIN articles ca ON ca.id = e.citing_id "
                   "WHERE e.found_at >= datetime('now', ?) ORDER BY e.found_at DESC")
            if self._memory_conn is not None:
                with self._memory_lock:
                    cur = self._memory_conn.execute(sql, (f"-{int(days)} days",))
                    cols = [d[0] for d in cur.description]
                    return [dict(zip(cols, r)) for r in cur.fetchall()]
            cur = self._conn().execute(sql, (f"-{int(days)} days",))
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, r)) for r in cur.fetchall()]
        except sqlite3.Error as e:
            logger.error("get_citation_edges_since 失败: %s", e)
            return []

    # ── 作者追踪 ────────────────────────────────────────────────

    def add_watch_author(self, name: str, openalex_id: Optional[str] = None) -> Optional[int]:
        try:
            sql = "INSERT INTO watch_authors (name, openalex_id) VALUES (?, ?)"
            if self._memory_conn is not None:
                with self._memory_lock:
                    cur = self._memory_conn.execute(sql, (name.strip(), openalex_id))
                    self._memory_conn.commit()
                    return cur.lastrowid
            conn = self._conn()
            cur = conn.execute(sql, (name.strip(), openalex_id))
            conn.commit()
            return cur.lastrowid
        except sqlite3.Error as e:
            logger.error("add_watch_author 失败: %s", e)
            return None

    def list_watch_authors(self, enabled_only: bool = True) -> list[dict[str, Any]]:
        try:
            sql = ("SELECT id, name, openalex_id, enabled, last_run, created_at FROM watch_authors "
                   + ("WHERE enabled = 1 " if enabled_only else "") + "ORDER BY id")
            if self._memory_conn is not None:
                with self._memory_lock:
                    cur = self._memory_conn.execute(sql)
                    cols = [d[0] for d in cur.description]
                    return [dict(zip(cols, r)) for r in cur.fetchall()]
            cur = self._conn().execute(sql)
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, r)) for r in cur.fetchall()]
        except sqlite3.Error as e:
            logger.error("list_watch_authors 失败: %s", e)
            return []

    def delete_watch_author(self, author_id: int) -> bool:
        try:
            if self._memory_conn is not None:
                with self._memory_lock:
                    cur = self._memory_conn.execute("DELETE FROM watch_authors WHERE id = ?", (author_id,))
                    self._memory_conn.commit()
                    return cur.rowcount > 0
            conn = self._conn()
            cur = conn.execute("DELETE FROM watch_authors WHERE id = ?", (author_id,))
            conn.commit()
            return cur.rowcount > 0
        except sqlite3.Error as e:
            logger.error("delete_watch_author 失败: %s", e)
            return False

    def mark_author_run(self, author_id: int) -> None:
        try:
            if self._memory_conn is not None:
                with self._memory_lock:
                    self._memory_conn.execute(
                        "UPDATE watch_authors SET last_run = ? WHERE id = ?",
                        (datetime.now().isoformat(timespec="seconds"), author_id))
                    self._memory_conn.commit()
            else:
                conn = self._conn()
                conn.execute("UPDATE watch_authors SET last_run = ? WHERE id = ?",
                             (datetime.now().isoformat(timespec="seconds"), author_id))
                conn.commit()
        except sqlite3.Error as e:
            logger.error("mark_author_run 失败: %s", e)

    # ── 研究专题 ────────────────────────────────────────────────

    def create_topic(self, name: str, research_question: str = "", notes: str = "") -> Optional[int]:
        try:
            if self._memory_conn is not None:
                with self._memory_lock:
                    cur = self._memory_conn.execute(
                        "INSERT INTO topics (name, research_question, notes) VALUES (?,?,?)",
                        (name.strip(), research_question, notes))
                    self._memory_conn.commit()
                    return cur.lastrowid
            conn = self._conn()
            cur = conn.execute("INSERT INTO topics (name, research_question, notes) VALUES (?,?,?)",
                               (name.strip(), research_question, notes))
            conn.commit()
            return cur.lastrowid
        except sqlite3.Error as e:
            logger.error("create_topic 失败: %s", e)
            return None

    def list_topics(self) -> list[dict[str, Any]]:
        try:
            sql = ("SELECT t.id, t.name, t.research_question, t.notes, t.created_at, "
                   "COUNT(p.article_id) AS paper_count "
                   "FROM topics t LEFT JOIN topic_papers p ON p.topic_id = t.id "
                   "GROUP BY t.id ORDER BY t.id DESC")
            if self._memory_conn is not None:
                with self._memory_lock:
                    cur = self._memory_conn.execute(sql)
                    cols = [d[0] for d in cur.description]
                    return [dict(zip(cols, r)) for r in cur.fetchall()]
            cur = self._conn().execute(sql)
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, r)) for r in cur.fetchall()]
        except sqlite3.Error as e:
            logger.error("list_topics 失败: %s", e)
            return []

    def get_topic(self, topic_id: int) -> Optional[dict[str, Any]]:
        try:
            sql = "SELECT id, name, research_question, notes, created_at FROM topics WHERE id = ?"
            papers_sql = ("SELECT a.id, a.title, a.journal, a.topic, a.relevance, a.read_status, "
                          "a.evidence_level FROM topic_papers p "
                          "JOIN articles a ON a.id = p.article_id WHERE p.topic_id = ? "
                          "ORDER BY COALESCE(a.relevance, 0) DESC")
            if self._memory_conn is not None:
                with self._memory_lock:
                    cur = self._memory_conn.execute(sql, (topic_id,))
                    row = cur.fetchone()
                    if not row:
                        return None
                    cols = [d[0] for d in cur.description]
                    pcur = self._memory_conn.execute(papers_sql, (topic_id,))
                    pcols = [d[0] for d in pcur.description]
                    papers = [dict(zip(pcols, r)) for r in pcur.fetchall()]
            else:
                conn = self._conn()
                cur = conn.execute(sql, (topic_id,))
                row = cur.fetchone()
                if not row:
                    return None
                cols = [d[0] for d in cur.description]
                pcur = conn.execute(papers_sql, (topic_id,))
                pcols = [d[0] for d in pcur.description]
                papers = [dict(zip(pcols, r)) for r in pcur.fetchall()]
            topic = dict(zip(cols, row))
            topic["papers"] = papers
            return topic
        except sqlite3.Error as e:
            logger.error("get_topic 失败: %s", e)
            return None

    def update_topic(self, topic_id: int, name: str, research_question: str, notes: str) -> bool:
        try:
            if self._memory_conn is not None:
                with self._memory_lock:
                    self._memory_conn.execute(
                        "UPDATE topics SET name = ?, research_question = ?, notes = ? WHERE id = ?",
                        (name, research_question, notes, topic_id))
                    self._memory_conn.commit()
            else:
                conn = self._conn()
                conn.execute("UPDATE topics SET name = ?, research_question = ?, notes = ? WHERE id = ?",
                             (name, research_question, notes, topic_id))
                conn.commit()
            return True
        except sqlite3.Error as e:
            logger.error("update_topic 失败: %s", e)
            return False

    def delete_topic(self, topic_id: int) -> bool:
        try:
            if self._memory_conn is not None:
                with self._memory_lock:
                    self._memory_conn.execute("DELETE FROM topic_papers WHERE topic_id = ?", (topic_id,))
                    self._memory_conn.execute("DELETE FROM topics WHERE id = ?", (topic_id,))
                    self._memory_conn.commit()
            else:
                conn = self._conn()
                conn.execute("DELETE FROM topic_papers WHERE topic_id = ?", (topic_id,))
                conn.execute("DELETE FROM topics WHERE id = ?", (topic_id,))
                conn.commit()
            return True
        except sqlite3.Error as e:
            logger.error("delete_topic 失败: %s", e)
            return False

    def add_topic_papers(self, topic_id: int, article_ids: list[int]) -> int:
        added = 0
        try:
            if not self._exists("topics", "id", topic_id):
                return 0
            article_ids = [aid for aid in article_ids if self._exists("articles", "id", aid)]
            if self._memory_conn is not None:
                with self._memory_lock:
                    for aid in article_ids:
                        cur = self._memory_conn.execute(
                            "INSERT OR IGNORE INTO topic_papers (topic_id, article_id) VALUES (?, ?)",
                            (topic_id, aid))
                        added += cur.rowcount
                    self._memory_conn.commit()
            else:
                conn = self._conn()
                for aid in article_ids:
                    cur = conn.execute(
                        "INSERT OR IGNORE INTO topic_papers (topic_id, article_id) VALUES (?, ?)",
                        (topic_id, aid))
                    added += cur.rowcount
                conn.commit()
            return added
        except sqlite3.Error as e:
            logger.error("add_topic_papers 失败: %s", e)
            return added

    def remove_topic_paper(self, topic_id: int, article_id: int) -> bool:
        try:
            if self._memory_conn is not None:
                with self._memory_lock:
                    cur = self._memory_conn.execute(
                        "DELETE FROM topic_papers WHERE topic_id = ? AND article_id = ?",
                        (topic_id, article_id))
                    self._memory_conn.commit()
                    return cur.rowcount > 0
            conn = self._conn()
            cur = conn.execute("DELETE FROM topic_papers WHERE topic_id = ? AND article_id = ?",
                               (topic_id, article_id))
            conn.commit()
            return cur.rowcount > 0
        except sqlite3.Error as e:
            logger.error("remove_topic_paper 失败: %s", e)
            return False

    # ── 划线摘录 ────────────────────────────────────────────────

    def add_highlight(self, article_id: int, text: str, note: str = "") -> Optional[int]:
        try:
            if not self._exists("articles", "id", article_id):
                return None
            if self._memory_conn is not None:
                with self._memory_lock:
                    cur = self._memory_conn.execute(
                        "INSERT INTO highlights (article_id, text, note) VALUES (?,?,?)",
                        (article_id, text.strip(), note))
                    self._memory_conn.commit()
                    return cur.lastrowid
            conn = self._conn()
            cur = conn.execute("INSERT INTO highlights (article_id, text, note) VALUES (?,?,?)",
                               (article_id, text.strip(), note))
            conn.commit()
            return cur.lastrowid
        except sqlite3.Error as e:
            logger.error("add_highlight 失败: %s", e)
            return None

    def get_highlights(self, article_id: int) -> list[dict[str, Any]]:
        try:
            sql = "SELECT id, text, note, created_at FROM highlights WHERE article_id = ? ORDER BY id DESC"
            if self._memory_conn is not None:
                with self._memory_lock:
                    cur = self._memory_conn.execute(sql, (article_id,))
                    cols = [d[0] for d in cur.description]
                    return [dict(zip(cols, r)) for r in cur.fetchall()]
            cur = self._conn().execute(sql, (article_id,))
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, r)) for r in cur.fetchall()]
        except sqlite3.Error as e:
            logger.error("get_highlights 失败: %s", e)
            return []

    def delete_highlight(self, highlight_id: int) -> bool:
        try:
            if self._memory_conn is not None:
                with self._memory_lock:
                    cur = self._memory_conn.execute("DELETE FROM highlights WHERE id = ?", (highlight_id,))
                    self._memory_conn.commit()
                    return cur.rowcount > 0
            conn = self._conn()
            cur = conn.execute("DELETE FROM highlights WHERE id = ?", (highlight_id,))
            conn.commit()
            return cur.rowcount > 0
        except sqlite3.Error as e:
            logger.error("delete_highlight 失败: %s", e)
            return False

    # ── 对比 / 草稿结果 ─────────────────────────────────────────

    def save_result(self, kind: str, article_ids: list[int], content: str, model: str = "") -> Optional[int]:
        try:
            if self._memory_conn is not None:
                with self._memory_lock:
                    cur = self._memory_conn.execute(
                        "INSERT INTO compare_results (kind, article_ids, content, model) VALUES (?,?,?,?)",
                        (kind, json.dumps(article_ids), content, model))
                    self._memory_conn.commit()
                    return cur.lastrowid
            conn = self._conn()
            cur = conn.execute(
                "INSERT INTO compare_results (kind, article_ids, content, model) VALUES (?,?,?,?)",
                (kind, json.dumps(article_ids), content, model))
            conn.commit()
            return cur.lastrowid
        except sqlite3.Error as e:
            logger.error("save_result 失败: %s", e)
            return None

    def get_result(self, result_id: int) -> Optional[dict[str, Any]]:
        try:
            sql = "SELECT id, kind, article_ids, content, model, created_at FROM compare_results WHERE id = ?"
            if self._memory_conn is not None:
                with self._memory_lock:
                    cur = self._memory_conn.execute(sql, (result_id,))
                    row = cur.fetchone()
                    cols = [d[0] for d in cur.description]
            else:
                cur = self._conn().execute(sql, (result_id,))
                row = cur.fetchone()
                cols = [d[0] for d in cur.description]
            if not row:
                return None
            item = dict(zip(cols, row))
            try:
                item["article_ids"] = json.loads(item.get("article_ids") or "[]")
            except (TypeError, ValueError):
                pass
            return item
        except sqlite3.Error as e:
            logger.error("get_result 失败: %s", e)
            return None

    def list_results(self, limit: int = 20) -> list[dict[str, Any]]:
        try:
            sql = ("SELECT id, kind, article_ids, content, model, created_at FROM compare_results "
                   "ORDER BY id DESC LIMIT ?")
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
                item["content"] = (item.get("content") or "")[:120]
                try:
                    item["article_ids"] = json.loads(item.get("article_ids") or "[]")
                except (TypeError, ValueError):
                    pass
                items.append(item)
            return items
        except sqlite3.Error as e:
            logger.error("list_results 失败: %s", e)
            return []

    # ── 期刊源健康度 ────────────────────────────────────────────

    def update_journal_health(self, journal_id: int, ok: bool, error: str = "") -> None:
        try:
            if ok:
                sql = ("UPDATE journals SET last_run = ?, consecutive_failures = 0, "
                       "last_error = NULL WHERE id = ?")
            else:
                # last_run is the successful incremental cursor. Keep it intact
                # after failures so a later retry covers the failed window.
                sql = ("UPDATE journals SET consecutive_failures = COALESCE(consecutive_failures, 0) + 1, "
                       "last_error = ? WHERE id = ?")
            params = (error[:300], journal_id) if not ok else \
                     (datetime.now().isoformat(timespec="seconds"), journal_id)
            if self._memory_conn is not None:
                with self._memory_lock:
                    self._memory_conn.execute(sql, params)
                    self._memory_conn.commit()
            else:
                conn = self._conn()
                conn.execute(sql, params)
                conn.commit()
        except sqlite3.Error as e:
            logger.error("update_journal_health 失败: %s", e)

    def list_untranslated_titles(self, limit: int = 100) -> list[dict[str, Any]]:
        """返回尚未有中文标题的文章（优先高相关/收藏）。"""
        sql = (
            "SELECT id, title FROM articles "
            "WHERE title IS NOT NULL AND trim(title) != '' "
            "AND (title_zh IS NULL OR trim(title_zh) = '') "
            "ORDER BY COALESCE(relevance, 0) DESC, COALESCE(starred, 0) DESC, id DESC "
            "LIMIT ?"
        )
        try:
            if self._memory_conn is not None:
                with self._memory_lock:
                    cur = self._memory_conn.execute(sql, (limit,))
                    cols = [d[0] for d in cur.description]
                    return [dict(zip(cols, r)) for r in cur.fetchall()]
            cur = self._conn().execute(sql, (limit,))
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, r)) for r in cur.fetchall()]
        except sqlite3.Error as e:
            logger.error("list_untranslated_titles 失败: %s", e)
            return []

    def save_digest_entries(self, digest_date: str, digest_type: str,
                            items: list[dict[str, Any]]) -> int:
        """写入当日 digest 条目（幂等：同 date+type+article 覆盖分数）。"""
        if not items:
            return 0
        sql = (
            "INSERT INTO digest_entries "
            "(digest_date, digest_type, article_id, rank, category, "
            " relevance_score, final_score, selected_reason, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now')) "
            "ON CONFLICT(digest_date, digest_type, article_id) DO UPDATE SET "
            "rank=excluded.rank, category=excluded.category, "
            "relevance_score=excluded.relevance_score, "
            "final_score=excluded.final_score, "
            "selected_reason=excluded.selected_reason"
        )
        n = 0
        try:
            if self._memory_conn is not None:
                with self._memory_lock:
                    for it in items:
                        self._memory_conn.execute(sql, (
                            digest_date, digest_type, it.get("article_id"),
                            it.get("rank"), it.get("category"),
                            it.get("relevance_score"), it.get("final_score"),
                            it.get("selected_reason"),
                        ))
                        n += 1
                    self._memory_conn.commit()
            else:
                conn = self._conn()
                for it in items:
                    conn.execute(sql, (
                        digest_date, digest_type, it.get("article_id"),
                        it.get("rank"), it.get("category"),
                        it.get("relevance_score"), it.get("final_score"),
                        it.get("selected_reason"),
                    ))
                    n += 1
                conn.commit()
            return n
        except sqlite3.Error as e:
            logger.error("save_digest_entries 失败: %s", e)
            return 0

    def list_digest_article_ids_since(self, digest_type: str, since_date: str,
                                      before_date: str = "9999-12-31") -> list[int]:
        try:
            sql = (
                "SELECT DISTINCT article_id FROM digest_entries "
                "WHERE digest_type = ? AND digest_date >= ? AND digest_date < ?"
            )
            if self._memory_conn is not None:
                with self._memory_lock:
                    cur = self._memory_conn.execute(sql, (digest_type, since_date, before_date))
                    return [r[0] for r in cur.fetchall()]
            cur = self._conn().execute(sql, (digest_type, since_date, before_date))
            return [r[0] for r in cur.fetchall()]
        except sqlite3.Error as e:
            logger.error("list_digest_article_ids_since 失败: %s", e)
            return []

    # ── 版本化日报：digest_versions / items / artifacts / sends ────

    _VERSION_COLS = ("id, digest_type, digest_date, version, status, request_key, "
                     "config_json, config_hash, content_hash, stats_json, created_at, request_hash")

    @staticmethod
    def _digest_json(value: str, field: str) -> dict[str, Any]:
        try:
            parsed = json.loads(value)
            if not isinstance(parsed, dict):
                raise ValueError("expected object")
            return parsed
        except (TypeError, ValueError) as e:
            raise sqlite3.DatabaseError(f"Corrupt digest {field}") from e

    @staticmethod
    def _version_row(cur: sqlite3.Cursor, row) -> dict[str, Any]:
        out = dict(zip([c[0] for c in cur.description], row))
        out["stats"] = Database._digest_json(out.pop("stats_json"), "stats_json")
        Database._digest_json(out["config_json"], "config_json")
        return out

    def save_digest_version(self, *, digest_date: str, items: list[dict[str, Any]],
                            digest_type: str = "daily", config_json: str = "{}",
                            config_hash: str = "", content_hash: str = "",
                            stats_json: str = "{}",
                            request_key: Optional[str] = None,
                            status: str = "published", request_hash: Optional[str] = None,
                            reuse_published: bool = False,
                            channels: Optional[list[str]] = None) -> dict[str, Any]:
        """事务内分配版本号并写入版本 + 全部条目快照；失败整笔回滚。

        并发冲突依靠 UNIQUE(digest_type, digest_date, version) 与 request_key
        唯一索引兜底，冲突抛 sqlite3.IntegrityError 由服务层转换为 CONFLICT。
        """
        conn = self._memory_conn if self._memory_conn is not None else self._conn()
        lock = self._memory_lock if self._memory_conn is not None else None

        def _txn() -> dict[str, Any]:
            conn.execute("BEGIN IMMEDIATE")
            try:
                if request_key and request_hash:
                    row = conn.execute(
                        "SELECT id, digest_date, version, request_hash FROM digest_versions "
                        "WHERE request_key = ?", (request_key,)).fetchone()
                    if row:
                        if row[1] != digest_date or row[3] != request_hash:
                            raise sqlite3.IntegrityError("request_key content conflict")
                        conn.commit()
                        return {"version_id": row[0], "version": row[2], "created": False}
                if reuse_published:
                    row = conn.execute(
                        "SELECT id, version FROM digest_versions WHERE digest_type = ? "
                        "AND digest_date = ? AND status = 'published' ORDER BY version DESC LIMIT 1",
                        (digest_type, digest_date)).fetchone()
                    if row:
                        conn.commit()
                        return {"version_id": row[0], "version": row[1], "created": False}
                row = conn.execute(
                    "SELECT COALESCE(MAX(version), 0) + 1 FROM digest_versions "
                    "WHERE digest_type = ? AND digest_date = ?",
                    (digest_type, digest_date),
                ).fetchone()
                version = int(row[0])
                cur = conn.execute(
                    "INSERT INTO digest_versions (digest_type, digest_date, version, "
                    "status, request_key, config_json, config_hash, content_hash, stats_json, request_hash) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (digest_type, digest_date, version, status, request_key,
                     config_json, config_hash, content_hash, stats_json, request_hash),
                )
                version_id = cur.lastrowid
                for i, it in enumerate(items, start=1):
                    conn.execute(
                        "INSERT INTO digest_items (version_id, article_id, rank, "
                        "category, scores_json, reason, snapshot_json) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (version_id, it.get("article_id"), it.get("rank", i),
                         it.get("category"), it.get("scores_json"),
                         it.get("reason"), it.get("snapshot_json")),
                    )
                for fmt in ("markdown", "html"):
                    conn.execute("INSERT INTO digest_artifacts (version_id, format, status) "
                                 "VALUES (?, ?, 'pending')", (version_id, fmt))
                for channel in dict.fromkeys(channels or []):
                    conn.execute("INSERT INTO digest_sends (version_id, channel, status, request_key) "
                                 "VALUES (?, ?, ?, ?)",
                                 (version_id, channel, "pending" if items else "skipped", request_key))
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            return {"version_id": version_id, "digest_date": digest_date,
                    "digest_type": digest_type, "version": version,
                    "status": status, "item_count": len(items), "created": True}

        if lock is not None:
            with lock:
                return _txn()
        return _txn()

    def get_digest_version_by_id(self, version_id: int) -> Optional[dict[str, Any]]:
        sql = f"SELECT {self._VERSION_COLS} FROM digest_versions WHERE id = ?"
        try:
            if self._memory_conn is not None:
                with self._memory_lock:
                    cur = self._memory_conn.execute(sql, (int(version_id),))
                    row = cur.fetchone()
                    return self._version_row(cur, row) if row else None
            cur = self._conn().execute(sql, (int(version_id),))
            row = cur.fetchone()
            return self._version_row(cur, row) if row else None
        except sqlite3.Error as e:
            logger.error("get_digest_version_by_id 失败: %s", e)
            raise

    def get_digest_version_by_request_key(self, request_key: str) -> Optional[dict[str, Any]]:
        sql = f"SELECT {self._VERSION_COLS} FROM digest_versions WHERE request_key = ?"
        try:
            if self._memory_conn is not None:
                with self._memory_lock:
                    cur = self._memory_conn.execute(sql, (request_key,))
                    row = cur.fetchone()
                    return self._version_row(cur, row) if row else None
            cur = self._conn().execute(sql, (request_key,))
            row = cur.fetchone()
            return self._version_row(cur, row) if row else None
        except sqlite3.Error as e:
            logger.error("get_digest_version_by_request_key 失败: %s", e)
            raise

    def get_latest_published_digest_version(self, digest_date: str,
                                            digest_type: str = "daily") -> Optional[dict[str, Any]]:
        sql = (f"SELECT {self._VERSION_COLS} FROM digest_versions "
               f"WHERE digest_type = ? AND digest_date = ? AND status = 'published' "
               f"ORDER BY version DESC LIMIT 1")
        try:
            if self._memory_conn is not None:
                with self._memory_lock:
                    cur = self._memory_conn.execute(sql, (digest_type, digest_date))
                    row = cur.fetchone()
                    return self._version_row(cur, row) if row else None
            cur = self._conn().execute(sql, (digest_type, digest_date))
            row = cur.fetchone()
            return self._version_row(cur, row) if row else None
        except sqlite3.Error as e:
            logger.error("get_latest_published_digest_version 失败: %s", e)
            raise

    def list_digest_versions(self, digest_type: str = "daily",
                             date_from: Optional[str] = None,
                             date_to: Optional[str] = None,
                             limit: int = 50, offset: int = 0) -> list[dict[str, Any]]:
        sql = f"SELECT {self._VERSION_COLS} FROM digest_versions WHERE digest_type = ?"
        params: list[Any] = [digest_type]
        if date_from:
            sql += " AND digest_date >= ?"
            params.append(date_from)
        if date_to:
            sql += " AND digest_date <= ?"
            params.append(date_to)
        sql += " ORDER BY digest_date DESC, version DESC LIMIT ? OFFSET ?"
        params += [int(limit), int(offset)]
        try:
            if self._memory_conn is not None:
                with self._memory_lock:
                    cur = self._memory_conn.execute(sql, params)
                    return [self._version_row(cur, r) for r in cur.fetchall()]
            cur = self._conn().execute(sql, params)
            return [self._version_row(cur, r) for r in cur.fetchall()]
        except sqlite3.Error as e:
            logger.error("list_digest_versions 失败: %s", e)
            raise

    def get_digest_items(self, version_id: int) -> list[dict[str, Any]]:
        """按 rank 返回版本条目；scores_json / snapshot_json 解析为对象。"""
        sql = ("SELECT id, version_id, article_id, rank, category, scores_json, "
               "reason, snapshot_json FROM digest_items "
               "WHERE version_id = ? ORDER BY rank ASC")
        try:
            if self._memory_conn is not None:
                with self._memory_lock:
                    cur = self._memory_conn.execute(sql, (int(version_id),))
                    rows = cur.fetchall()
            else:
                cur = self._conn().execute(sql, (int(version_id),))
                rows = cur.fetchall()
        except sqlite3.Error as e:
            logger.error("get_digest_items 失败: %s", e)
            raise
        items = []
        for r in rows:
            it = dict(zip([c[0] for c in cur.description], r))
            it["scores"] = self._digest_json(it.pop("scores_json"), "scores_json")
            it["snapshot"] = self._digest_json(it.pop("snapshot_json"), "snapshot_json")
            items.append(it)
        return items

    def list_published_digest_article_ids_since(self, digest_type: str, since_date: str,
                                                before_date: str = "9999-12-31") -> list[int]:
        """防重集合：旧 digest_entries 与新已发布版本条目合并去重。

        与旧接口不同：读取失败抛 sqlite3.Error（历史读取失败必须显式报错，
        不得默认当成空历史）。
        """
        sql = (
            "SELECT DISTINCT article_id FROM ("
            "  SELECT article_id FROM digest_entries"
            "   WHERE digest_type = ? AND digest_date >= ? AND digest_date < ?"
            "  UNION"
            "  SELECT di.article_id FROM digest_items di"
            "   JOIN digest_versions dv ON dv.id = di.version_id"
            "   WHERE dv.digest_type = ? AND dv.digest_date >= ? AND dv.digest_date < ?"
            "     AND dv.status = 'published'"
            ") WHERE article_id IS NOT NULL"
        )
        params = (digest_type, since_date, before_date,
                  digest_type, since_date, before_date)
        if self._memory_conn is not None:
            with self._memory_lock:
                cur = self._memory_conn.execute(sql, params)
                return [r[0] for r in cur.fetchall()]
        cur = self._conn().execute(sql, params)
        return [r[0] for r in cur.fetchall()]

    def upsert_digest_artifact(self, version_id: int, fmt: str, *,
                               path: Optional[str] = None,
                               content_hash: Optional[str] = None,
                               status: str = "pending",
                               error: Optional[str] = None) -> None:
        sql = (
            "INSERT INTO digest_artifacts (version_id, format, path, content_hash, status, error) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(version_id, format) DO UPDATE SET "
            "path=excluded.path, content_hash=excluded.content_hash, "
            "status=excluded.status, error=excluded.error, updated_at=datetime('now')"
        )
        try:
            if self._memory_conn is not None:
                with self._memory_lock:
                    self._memory_conn.execute(sql, (version_id, fmt, path, content_hash, status, error))
                    self._memory_conn.commit()
            else:
                conn = self._conn()
                conn.execute(sql, (version_id, fmt, path, content_hash, status, error))
                conn.commit()
        except sqlite3.Error as e:
            logger.error("upsert_digest_artifact 失败: %s", e)
            raise

    def list_digest_artifacts(self, version_id: int) -> list[dict[str, Any]]:
        sql = ("SELECT id, version_id, format, path, content_hash, status, error, updated_at "
               "FROM digest_artifacts WHERE version_id = ? ORDER BY format ASC")
        try:
            if self._memory_conn is not None:
                with self._memory_lock:
                    cur = self._memory_conn.execute(sql, (int(version_id),))
                    return [dict(zip([c[0] for c in cur.description], r)) for r in cur.fetchall()]
            cur = self._conn().execute(sql, (int(version_id),))
            return [dict(zip([c[0] for c in cur.description], r)) for r in cur.fetchall()]
        except sqlite3.Error as e:
            logger.error("list_digest_artifacts 失败: %s", e)
            raise

    def init_digest_sends(self, version_id: int, channels: list[str]) -> int:
        """为指定渠道创建 pending 发送记录（已存在的不动）。"""
        n = 0
        try:
            for ch in channels:
                sql = ("INSERT INTO digest_sends (version_id, channel, status) "
                       "VALUES (?, ?, 'pending') "
                       "ON CONFLICT(version_id, channel) DO NOTHING")
                if self._memory_conn is not None:
                    with self._memory_lock:
                        cur = self._memory_conn.execute(sql, (version_id, ch))
                        n += cur.rowcount or 0
                        self._memory_conn.commit()
                else:
                    conn = self._conn()
                    cur = conn.execute(sql, (version_id, ch))
                    n += cur.rowcount or 0
                    conn.commit()
            return n
        except sqlite3.Error as e:
            logger.error("init_digest_sends 失败: %s", e)
            raise

    def list_digest_sends(self, version_id: int) -> list[dict[str, Any]]:
        sql = ("SELECT id, version_id, channel, status, attempts, request_key, "
               "claim_token, claimed_at, last_error, finished_at "
               "FROM digest_sends WHERE version_id = ? ORDER BY channel ASC")
        try:
            if self._memory_conn is not None:
                with self._memory_lock:
                    cur = self._memory_conn.execute(sql, (int(version_id),))
                    return [dict(zip([c[0] for c in cur.description], r)) for r in cur.fetchall()]
            cur = self._conn().execute(sql, (int(version_id),))
            return [dict(zip([c[0] for c in cur.description], r)) for r in cur.fetchall()]
        except sqlite3.Error as e:
            logger.error("list_digest_sends 失败: %s", e)
            raise

    def claim_digest_send(self, version_id: int, channel: str,
                          claim_token: str) -> Optional[dict[str, Any]]:
        """领取一条发送记录：仅 pending/failed 可领取（attempts+1，写入领取令牌）。

        领取失败（已被领取/不存在/sent/unknown）返回当前记录，调用方核对
        claim_token 判断是否获得所有权。"""
        claim_sql = (
            "UPDATE digest_sends SET status = 'sending', claim_token = ?, "
            "claimed_at = datetime('now'), attempts = attempts + 1, last_error = NULL "
            "WHERE version_id = ? AND channel = ? AND status IN ('pending', 'failed')"
        )
        select_sql = (
            "SELECT id, version_id, channel, status, attempts, request_key, "
            "claim_token, claimed_at, last_error, finished_at "
            "FROM digest_sends WHERE version_id = ? AND channel = ?"
        )
        try:
            if self._memory_conn is not None:
                with self._memory_lock:
                    self._memory_conn.execute(claim_sql, (claim_token, version_id, channel))
                    self._memory_conn.commit()
                    cur = self._memory_conn.execute(select_sql, (version_id, channel))
                    row = cur.fetchone()
                    return dict(zip([c[0] for c in cur.description], row)) if row else None
            conn = self._conn()
            conn.execute(claim_sql, (claim_token, version_id, channel))
            conn.commit()
            cur = conn.execute(select_sql, (version_id, channel))
            row = cur.fetchone()
            return dict(zip([c[0] for c in cur.description], row)) if row else None
        except sqlite3.Error as e:
            logger.error("claim_digest_send 失败: %s", e)
            raise

    def finish_digest_send(self, version_id: int, channel: str, claim_token: str,
                           status: str, error: Optional[str] = None) -> bool:
        """凭领取令牌回写发送结果；令牌不匹配（陈旧领取者）返回 False。"""
        if status not in ("sent", "failed", "unknown"):
            raise ValueError("Invalid delivery finish status")
        sql = ("UPDATE digest_sends SET status = ?, last_error = ?, "
               "finished_at = datetime('now') "
               "WHERE version_id = ? AND channel = ? AND claim_token = ? AND status = 'sending'")
        try:
            if self._memory_conn is not None:
                with self._memory_lock:
                    cur = self._memory_conn.execute(
                        sql, (status, error, version_id, channel, claim_token))
                    self._memory_conn.commit()
                    return bool(cur.rowcount)
            conn = self._conn()
            cur = conn.execute(sql, (status, error, version_id, channel, claim_token))
            conn.commit()
            return bool(cur.rowcount)
        except sqlite3.Error as e:
            self._conn().rollback()
            logger.error("finish_digest_send 失败: %s", e)
            raise

    def resolve_digest_send(self, version_id: int, channel: str, delivered: bool) -> bool:
        """Explicit reconciliation only; never performs transport or claims work."""
        sql = ("UPDATE digest_sends SET status = ?, claim_token = NULL, claimed_at = NULL, "
               "last_error = NULL, finished_at = CASE WHEN ? THEN datetime('now') ELSE NULL END "
               "WHERE version_id = ? AND channel = ? AND status = 'unknown'")
        def update():
            conn = self._conn()
            try:
                cur = conn.execute(sql, ("sent" if delivered else "pending", delivered, version_id, channel))
                conn.commit()
                return bool(cur.rowcount)
            except sqlite3.Error:
                conn.rollback()
                raise
        if self._memory_conn is not None:
            with self._memory_lock:
                return update()
        return update()

    def list_articles_created_between(self, start: str, end: str) -> list[dict[str, Any]]:
        """按入库日期区间（本地时区，含头不含尾）列出文章。"""
        try:
            sql = ("SELECT * FROM articles WHERE date(created_at, 'localtime') >= ? "
                   "AND date(created_at, 'localtime') < ? "
                   "ORDER BY COALESCE(relevance, 0) DESC")
            if self._memory_conn is not None:
                with self._memory_lock:
                    cur = self._memory_conn.execute(sql, (start, end))
                    cols = [d[0] for d in cur.description]
                    rows = cur.fetchall()
            else:
                cur = self._conn().execute(sql, (start, end))
                cols = [d[0] for d in cur.description]
                rows = cur.fetchall()
            items = []
            for r in rows:
                item = dict(zip(cols, r))
                item.pop("fulltext_text", None)
                items.append(item)
            return items
        except sqlite3.Error as e:
            logger.error("list_articles_created_between 失败: %s", e)
            return []

    def get_positive_profile_texts(self, limit: int = 60) -> list[str]:
        """取星标/已读/认可文献的摘要，用于构建相似度偏好画像。"""
        try:
            sql = ("SELECT abstract FROM articles "
                   "WHERE starred = 1 OR read_status IN ('reading', 'read') "
                   "OR relevance_feedback = 'relevant' "
                   "ORDER BY updated_at DESC, id DESC LIMIT ?")
            if self._memory_conn is not None:
                with self._memory_lock:
                    rows = self._memory_conn.execute(sql, (limit,)).fetchall()
            else:
                rows = self._conn().execute(sql, (limit,)).fetchall()
            return [r[0] for r in rows if r[0]]
        except sqlite3.Error as e:
            logger.error("get_positive_profile_texts 失败: %s", e)
            return []

    def block_articles_by_clause(self, clause: str, params: list[Any], reason: str) -> int:
        """把符合条件（同一清理 WHERE）的文章标识写入屏蔽名单。"""
        sql_select = ("SELECT id, doi, url, title_hash, title FROM articles "
                      f"WHERE {clause}")
        sql_insert = ("INSERT OR IGNORE INTO blocked_articles (doi, url, title_hash, title, reason) "
                      "VALUES (?,?,?,?,?)")
        n = 0
        if self._memory_conn is not None:
            with self._memory_lock:
                rows = self._memory_conn.execute(sql_select, params).fetchall()
                for r in rows:
                    # 列序: id, doi, url, title_hash, title
                    self._memory_conn.execute(sql_insert, (
                        (r[1] or None), (r[2] or None), (r[3] or None), (r[4] or None)[:200], reason))
                    n += 1
                self._memory_conn.commit()
            return n
        conn = self._conn()
        rows = conn.execute(sql_select, params).fetchall()
        for r in rows:
            conn.execute(sql_insert, (
                (r[1] or None), (r[2] or None), (r[3] or None), (r[4] or None)[:200], reason))
            n += 1
        conn.commit()
        return n

    def list_blocked_articles(self, limit: int = 500) -> list[dict[str, Any]]:
        try:
            sql = ("SELECT id, doi, url, title_hash, title, reason, blocked_at "
                   "FROM blocked_articles ORDER BY id DESC LIMIT ?")
            if self._memory_conn is not None:
                with self._memory_lock:
                    cur = self._memory_conn.execute(sql, (limit,))
                    cols = [d[0] for d in cur.description]
                    return [dict(zip(cols, r)) for r in cur.fetchall()]
            cur = self._conn().execute(sql, (limit,))
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, r)) for r in cur.fetchall()]
        except sqlite3.Error as e:
            logger.error("list_blocked_articles 失败: %s", e)
            return []

    def unblock_article(self, block_id: int) -> bool:
        try:
            if self._memory_conn is not None:
                with self._memory_lock:
                    cur = self._memory_conn.execute(
                        "DELETE FROM blocked_articles WHERE id = ?", (block_id,))
                    self._memory_conn.commit()
                    return cur.rowcount > 0
            conn = self._conn()
            cur = conn.execute("DELETE FROM blocked_articles WHERE id = ?", (block_id,))
            conn.commit()
            return cur.rowcount > 0
        except sqlite3.Error as e:
            logger.error("unblock_article 失败: %s", e)
            return False

    def count_blocked_articles(self) -> int:
        try:
            if self._memory_conn is not None:
                with self._memory_lock:
                    return self._memory_conn.execute(
                        "SELECT COUNT(*) FROM blocked_articles").fetchone()[0]
            return self._conn().execute(
                "SELECT COUNT(*) FROM blocked_articles").fetchone()[0]
        except sqlite3.Error as e:
            logger.error("count_blocked_articles 失败: %s", e)
            return 0
