"""
web_server.py - 轻量网页端（只读看板 + API + 手动触发任务）
"""
from __future__ import annotations

import copy
import json
import logging
import os
import re
import sqlite3
import threading
import time
import traceback
import uuid
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional
from urllib.parse import parse_qs, urlparse

from core.analyzer import LLMAnalyzer
from core.db import Database
from core.fetcher import JournalFetcher, detect_publisher_from_url
from core.notifier import classify_article
from main import load_config, load_config_from_obj, run_once, setup_logging, validate_config

logger = logging.getLogger("web")


class TaskRunner:
    def __init__(self, base_config: dict[str, Any]) -> None:
        self._base_config = base_config
        self._lock = threading.Lock()
        self._last_scheduler_date: Optional[str] = None
        self._scheduler_stop = threading.Event()
        self._scheduler_thread: Optional[threading.Thread] = None
        self._db: Optional[Database] = None

    def attach_db(self, db: Database) -> None:
        """注入数据库实例，用于持久化任务运行记录。"""
        self._db = db
        self.state: dict[str, Any] = {
            "running": False,
            "task_id": None,
            "trigger": None,
            "mode": None,
            "started_at": None,
            "ended_at": None,
            "last_success_at": None,
            "last_error": None,
            "last_report": None,
            "run_count": 0,
            "success_count": 0,
            "failure_count": 0,
            "last_stats": None,
        }

    def _make_run_cfg(self, mode: str) -> dict[str, Any]:
        cfg = copy.deepcopy(self._base_config)
        fetcher_cfg = cfg.setdefault("fetcher", {})
        if mode == "abstract":
            fetcher_cfg["use_fulltext"] = False
        elif mode == "fulltext":
            fetcher_cfg["use_fulltext"] = True
        return cfg

    def start_run(self, trigger: str, mode: str = "default", date_str: Optional[str] = None) -> tuple[bool, str]:
        with self._lock:
            if self.state["running"]:
                return False, "任务正在运行"

            task_id = str(uuid.uuid4())
            self.state.update({
                "running": True,
                "task_id": task_id,
                "trigger": trigger,
                "mode": mode,
                "started_at": datetime.now().isoformat(timespec="seconds"),
                "ended_at": None,
                "last_error": None,
            })

        thread = threading.Thread(
            target=self._run_task,
            args=(task_id, trigger, mode, date_str),
            daemon=True,
        )
        thread.start()
        return True, task_id

    def _run_task(self, task_id: str, trigger: str, mode: str, date_str: Optional[str]) -> None:
        cfg = self._make_run_cfg(mode)
        success = False
        error_msg = None
        stats: Optional[dict[str, Any]] = None

        if self._db is not None:
            self._db.task_start(task_id, trigger=trigger, mode=mode, date_str=date_str)

        try:
            logger.info("任务启动: task_id=%s trigger=%s mode=%s", task_id, trigger, mode)
            if mode == "weekly":
                from main import run_weekly
                stats = run_weekly(cfg, date_str=date_str, task_id=task_id)
            else:
                stats = run_once(cfg, date_str=date_str, task_id=task_id)
            success = True
            logger.info("任务完成: task_id=%s", task_id)
        except Exception as e:  # pragma: no cover - 运行期保护
            error_msg = f"{type(e).__name__}: {e}\n{traceback.format_exc()}"
            logger.error("任务失败: task_id=%s, error=%s", task_id, e, exc_info=True)

        with self._lock:
            self.state["running"] = False
            self.state["ended_at"] = datetime.now().isoformat(timespec="seconds")
            self.state["run_count"] += 1
            if stats is not None:
                self.state["last_stats"] = stats
            if success:
                self.state["success_count"] += 1
                self.state["last_success_at"] = self.state["ended_at"]
            else:
                self.state["failure_count"] += 1
                self.state["last_error"] = error_msg

        if self._db is not None:
            status = "success" if success else "failed"
            if success and stats and (stats.get("db_errors") or stats.get("scored_failed")
                                      or stats.get("analyzed_failed")):
                status = "partial"
            self._db.task_finish(task_id, status=status, stats=stats, error=error_msg,
                                 stage="done" if success else "failed")

    def get_state(self) -> dict[str, Any]:
        with self._lock:
            return dict(self.state)

    def update_base_config(self, new_config: dict[str, Any]) -> None:
        """配置保存后更新任务基线配置，让后续手动触发的任务使用新配置。"""
        with self._lock:
            self._base_config = new_config

    def start_scheduler(self, run_time: Optional[str] = None) -> None:
        if run_time is None:
            run_time = self._base_config.get("scheduler", {}).get("run_time", "08:00")
        # 停掉旧调度线程（若有），保证改时间后重启不会双线程重复触发
        self._scheduler_stop.set()
        self._scheduler_stop = threading.Event()
        self._last_scheduler_date = None
        thread = threading.Thread(
            target=self._scheduler_loop,
            args=(run_time, self._scheduler_stop),
            daemon=True,
            name="web-scheduler",
        )
        self._scheduler_thread = thread
        thread.start()
        logger.info("已启动网页端调度线程: run_time=%s", run_time)

    def _scheduler_loop(self, run_time: str, stop_event: threading.Event) -> None:
        try:
            hour, minute = map(int, run_time.split(":"))
        except ValueError:
            logger.error("scheduler.run_time 格式错误，应为 HH:MM，当前=%s", run_time)
            return

        while not stop_event.is_set():
            now = datetime.now()
            today = now.strftime("%Y-%m-%d")
            if now.hour == hour and now.minute == minute and self._last_scheduler_date != today:
                ok, msg = self.start_run(trigger="schedule", mode="default")
                if ok:
                    self._last_scheduler_date = today
                    logger.info("调度触发成功: %s", msg)
                else:
                    logger.warning("调度触发跳过: %s", msg)
            stop_event.wait(30)


class WebContext:
    def __init__(self, config_path: str) -> None:
        self.config_path = config_path
        self.config = load_config(config_path)
        validate_config(self.config)
        self.runner = TaskRunner(self.config)
        self.db = Database(self.config["database"]["path"])
        self.runner.attach_db(self.db)
        # 上次异常退出遗留的 running 任务标记为 interrupted
        interrupted = self.db.mark_interrupted_tasks()
        if interrupted:
            logger.info("已将 %d 个遗留运行中的任务标记为 interrupted", interrupted)
        self.fetcher = JournalFetcher(self.config)

        output_cfg = self.config.get("output", {})
        self.output_dir = Path(output_cfg.get("output_dir", "data/output")).resolve()
        self.output_dir.mkdir(parents=True, exist_ok=True)

        db_path = self.config.get("database", {}).get("path", "data/db/chem_daily.db")
        self.db_path = Path(db_path).resolve()

        web_cfg = self.config.get("web", {})
        self.api_token = os.environ.get("WEB_API_TOKEN") or web_cfg.get("api_token")
        self.enable_scheduler = bool(web_cfg.get("enable_scheduler", False))

    def connect_db(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        return conn


def _to_int(value: Optional[str], default: int, min_v: int, max_v: int) -> int:
    if value is None:
        return default
    try:
        v = int(value)
    except ValueError:
        return default
    return max(min_v, min(max_v, v))


def _parse_bool(value: Optional[str], default: bool = False) -> bool:
    if value is None:
        return default
    return value.lower() in {"1", "true", "yes", "on"}


def _list_articles(ctx: WebContext, query: dict[str, list[str]]) -> dict[str, Any]:
    q = (query.get("q", [""])[0] or "").strip()
    journal = (query.get("journal", [""])[0] or "").strip()
    topic = (query.get("topic", [""])[0] or "").strip()
    tag = (query.get("tag", [""])[0] or "").strip()
    min_score = float(query.get("min_score", ["0"])[0] or 0)
    analyzed_only = _parse_bool((query.get("analyzed_only", ["false"])[0] or "false"))
    starred_only = _parse_bool((query.get("starred", ["false"])[0] or "false"))
    read_status = (query.get("read_status", [""])[0] or "").strip()
    limit = _to_int(query.get("limit", ["50"])[0], 50, 1, 200)
    offset = _to_int(query.get("offset", ["0"])[0], 0, 0, 100000)

    where = ["1=1"]
    params: list[Any] = []

    if q:
        where.append("(a.title LIKE ? OR a.abstract LIKE ? OR a.authors LIKE ? OR a.journal LIKE ?)")
        like = f"%{q}%"
        params.extend([like, like, like, like])
    if journal:
        where.append("a.journal = ?")
        params.append(journal)
    if starred_only:
        where.append("COALESCE(a.starred, 0) = 1")
    if tag:
        # tags 为逗号分隔存储，用首尾加逗号的方式做整词匹配
        where.append("(',' || COALESCE(a.tags, '') || ',') LIKE ?")
        params.append(f"%,{tag},%")
    if read_status == "none":
        where.append("COALESCE(a.read_status, '') = ''")
    elif read_status:
        where.append("a.read_status = ?")
        params.append(read_status)

    where.append("COALESCE(a.relevance, 0) >= ?")
    params.append(min_score)

    if analyzed_only:
        where.append("a.analysis IS NOT NULL AND a.analysis != ''")
    if topic:
        where.append("a.topic = ?")
        params.append(topic)

    where_clause = " AND ".join(where)

    conn = ctx.connect_db()
    try:
        # 1. 直接通过 SQL 获取总数
        count_sql = f"SELECT COUNT(*) FROM articles a WHERE {where_clause}"
        total = conn.execute(count_sql, params).fetchone()[0]

        # 2. 数据库层面物理分页，避免全量内存加载（不含 fulltext_text 大字段）
        sql = (
            "SELECT id, doi, title, journal, authors, pub_date, url, relevance, analysis, "
            "abstract, starred, tags, created_at, topic, relevance_reason, score_status, "
            "evidence_level, analysis_status, read_status, relevance_feedback, "
            "zotero_key, discovered_via, cited_count, sim_prior, title_zh, "
            "jm.if_value AS impact_factor, jm.cas_zone AS cas_zone "
            "FROM articles a "
            "LEFT JOIN journal_metrics jm ON a.journal = jm.name "
            f"WHERE {where_clause} "
            "ORDER BY a.relevance DESC, a.created_at DESC "
            "LIMIT ? OFFSET ?"
        )
        query_params = list(params) + [limit, offset]
        rows = conn.execute(sql, query_params).fetchall()

        page = []
        for row in rows:
            item = dict(row)
            # 兼容历史未打 topic 标签的数据
            if not item.get("topic"):
                item["topic"] = classify_article(item)
            item["has_analysis"] = bool(item.get("analysis"))
            item["has_fulltext"] = item.get("evidence_level") == "FULLTEXT"
            page.append(item)

        journal_rows = conn.execute(
            "SELECT DISTINCT journal FROM articles WHERE journal IS NOT NULL AND journal != '' ORDER BY journal"
        ).fetchall()
        journals = [r[0] for r in journal_rows]

        report_rows = conn.execute(
            "SELECT report_date FROM daily_reports ORDER BY report_date DESC LIMIT 14"
        ).fetchall()
        recent_dates = [r[0] for r in report_rows]

        return {
            "items": page,
            "count": len(page),
            "total": total,
            "offset": offset,
            "limit": limit,
            "journals": journals,
            "recent_report_dates": recent_dates,
        }
    finally:
        conn.close()


def _today_view(ctx: WebContext, query: dict[str, list[str]]) -> dict[str, Any]:
    """今日精选：按评分分桶（优先阅读 / 值得关注 / 快速浏览）+ 处理中队列。"""
    date = (query.get("date", [""])[0] or "").strip() or datetime.now().strftime("%Y-%m-%d")
    rows = ctx.db.list_articles_by_created_date(date, limit=500)
    buckets: dict[str, list[dict[str, Any]]] = {"top": [], "notable": [], "browse": []}
    processing: list[dict[str, Any]] = []

    for r in rows:
        if not r.get("topic"):
            r["topic"] = classify_article(r)
        r["has_analysis"] = bool(r.get("analysis"))
        r["has_fulltext"] = r.get("evidence_level") == "FULLTEXT"
        r.pop("fulltext_text", None)
        if r.get("score_status") != "ok" or r.get("relevance") is None:
            processing.append(r)
            continue
        score = float(r["relevance"])
        if score >= 8:
            buckets["top"].append(r)
        elif score >= 6:
            buckets["notable"].append(r)
        else:
            buckets["browse"].append(r)

    return {
        "date": date,
        "total": len(rows),
        "counts": {
            "top": len(buckets["top"]),
            "notable": len(buckets["notable"]),
            "browse": len(buckets["browse"]),
            "processing": len(processing),
        },
        "buckets": buckets,
        "processing": processing[:50],
        "task": ctx.runner.get_state(),
    }


def _set_read_status(ctx: WebContext, article_id: int, data: dict[str, Any]) -> tuple[dict[str, Any], int]:
    allowed = {"queued", "reading", "read", ""}
    status = str(data.get("read_status", "") or "")
    if status not in allowed:
        return {"ok": False, "error": f"read_status 必须是 {'/'.join(sorted(allowed))}"}, 400
    ctx.db.update_article_fields(article_id, read_status=status)
    return _article_action_result(ctx, article_id)


def _set_feedback(ctx: WebContext, article_id: int, data: dict[str, Any]) -> tuple[dict[str, Any], int]:
    allowed = {"relevant", "irrelevant", ""}
    feedback = str(data.get("feedback", "") or "")
    if feedback not in allowed:
        return {"ok": False, "error": f"feedback 必须是 {'/'.join(sorted(allowed))}"}, 400
    ctx.db.update_article_fields(article_id, relevance_feedback=feedback)
    return _article_action_result(ctx, article_id)


def _normalize_doi(raw: str) -> str:
    s = (raw or "").strip()
    s = re.sub(r"^https?://(dx\.)?doi\.org/", "", s, flags=re.I)
    return s.lower()


def _manual_add_article(ctx: WebContext, data: dict[str, Any]) -> tuple[dict[str, Any], int]:
    """手动添加文献：可只给 DOI 由 OpenAlex 补全，也可手工填字段。"""
    doi = _normalize_doi(str(data.get("doi") or ""))
    title = str(data.get("title") or "").strip()
    url = str(data.get("url") or "").strip()
    journal = str(data.get("journal") or "").strip()
    abstract = str(data.get("abstract") or "").strip()
    authors = str(data.get("authors") or "").strip()
    pub_date = str(data.get("pub_date") or "").strip()
    topic = str(data.get("topic") or "").strip() or None
    tags = str(data.get("tags") or "").strip()
    note = str(data.get("note") or "").strip()
    try:
        relevance = float(data.get("relevance")) if data.get("relevance") not in (None, "") else 8.0
    except (TypeError, ValueError):
        relevance = 8.0

    fetched = False
    if doi and (not title or not journal or not abstract):
        from integrations import openalex
        openalex.set_polite_email(ctx.config.get("unpaywall_email", "your@email.com"))
        work = openalex.get_work_by_doi(doi)
        if work:
            fetched = True
            title = title or work.get("title") or ""
            journal = journal or work.get("journal") or ""
            abstract = abstract or work.get("abstract") or ""
            authors = authors or ", ".join(work.get("authors") or [])
            pub_date = pub_date or work.get("pub_date") or ""
            url = url or work.get("url") or ""

    if not title and not doi:
        return {"ok": False, "error": "至少需要 DOI 或标题"}, 400

    if data.get("dry_run"):
        return {
            "ok": True,
            "dry_run": True,
            "fetched_from_openalex": fetched,
            "article": {
                "doi": doi,
                "title": title,
                "journal": journal,
                "authors": authors,
                "pub_date": pub_date,
                "url": url,
                "abstract": abstract,
            },
        }, 200

    article = {
        "doi": doi,
        "title": title,
        "journal": journal,
        "authors": authors,
        "pub_date": pub_date,
        "url": url or (f"https://doi.org/{doi}" if doi else ""),
        "abstract": abstract,
        "relevance": relevance,
        "topic": topic,
        "tags": tags,
        "note": note,
    }
    try:
        new_id = ctx.db.save_manual_article(article)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}, 500
    if new_id is None:
        # 重复：按 DOI/URL 回查已有条目
        existing_id = None
        conn = ctx.connect_db()
        try:
            if doi:
                row = conn.execute(
                    "SELECT id FROM articles WHERE lower(trim(doi)) = ?", (doi,)
                ).fetchone()
                if row:
                    existing_id = row[0]
            if existing_id is None and article["url"]:
                row = conn.execute(
                    "SELECT id FROM articles WHERE url = ?", (article["url"],)
                ).fetchone()
                if row:
                    existing_id = row[0]
        finally:
            conn.close()
        if existing_id:
            return {"ok": True, "duplicate": True, "id": existing_id, "message": "库中已有该文献"}, 200
        return {"ok": False, "error": "添加失败：可能缺标题或数据库错误"}, 400

    metric = ctx.db.get_journal_metric(journal) if journal else None
    return {
        "ok": True,
        "id": new_id,
        "fetched_from_openalex": fetched,
        "impact_factor": (metric or {}).get("if_value"),
        "cas_zone": (metric or {}).get("cas_zone"),
        "message": "已加入文献库（默认收藏，避免被低分清理）",
    }, 200


def _cleanup_low_relevance(ctx: WebContext, data: dict[str, Any]) -> tuple[dict[str, Any], int]:
    """清理相关性过低的文献。preview=true 只统计；否则执行删除。"""
    try:
        min_score = float(data.get("min_score", 5))
    except (TypeError, ValueError):
        return {"ok": False, "error": "min_score 无效"}, 400
    if not (0 <= min_score <= 10):
        return {"ok": False, "error": "min_score 需在 0–10"}, 400
    preview = bool(data.get("preview", True))
    count = ctx.db.count_low_relevance(min_score)
    sample = ctx.db.sample_low_relevance(min_score, limit=8)
    if preview:
        return {
            "ok": True,
            "preview": True,
            "min_score": min_score,
            "would_delete": count,
            "sample": sample,
            "protected": "收藏 / 笔记 / 标签 / Zotero / 阅读状态 / 反馈 会被保留",
        }, 200
    if count == 0:
        return {"ok": True, "deleted": 0, "min_score": min_score}, 200
    try:
        deleted = ctx.db.delete_low_relevance(min_score)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"删除失败: {e}"}, 500
    return {"ok": True, "deleted": deleted, "min_score": min_score}, 200


def _cleanup_export_csv(ctx: WebContext, data: dict[str, Any]) -> tuple[str, int, str]:
    """导出将被清理的文章 CSV（UTF-8 BOM，Excel 可直接打开）。"""
    import csv
    import io

    try:
        min_score = float(data.get("min_score", 5))
    except (TypeError, ValueError):
        return json.dumps({"ok": False, "error": "min_score 无效"}, ensure_ascii=False), 400, "application/json; charset=utf-8"
    if not (0 <= min_score <= 10):
        return json.dumps({"ok": False, "error": "min_score 需在 0–10"}, ensure_ascii=False), 400, "application/json; charset=utf-8"

    rows = ctx.db.list_low_relevance_rows(min_score)
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([
        "id", "doi", "title", "journal", "authors", "pub_date", "url",
        "abstract", "relevance", "topic", "tags", "note", "created_at",
    ])
    for r in rows:
        writer.writerow([
            r.get("id"), r.get("doi") or "", r.get("title") or "",
            r.get("journal") or "", r.get("authors") or "", r.get("pub_date") or "",
            r.get("url") or "", (r.get("abstract") or "").replace("\n", " ").replace("\r", " "),
            r.get("relevance"), r.get("topic") or "", r.get("tags") or "",
            (r.get("note") or "").replace("\n", " ").replace("\r", " "),
            r.get("created_at") or "",
        ])

    # 服务端同时落一份备份
    fname = f"cleanup-backup-lt{min_score:g}-{datetime.now():%Y%m%d-%H%M%S}.csv"
    try:
        out_cfg = ctx.config.get("output") or {}
        out_dir = Path(str(out_cfg.get("dir") or "data/output"))
        out_dir.mkdir(parents=True, exist_ok=True)
        backup_path = out_dir / fname
        backup_path.write_text("﻿" + buf.getvalue(), encoding="utf-8")
        logger.info("清理备份已保存: %s (%d 行)", backup_path, len(rows))
    except Exception as e:  # noqa: BLE001
        logger.warning("清理备份写盘失败（仍返回下载内容）: %s", e)

    body = "﻿" + buf.getvalue()
    ctx._export_filename = fname  # type: ignore[attr-defined]
    return body, 200, "text/csv; charset=utf-8"


def _journal_metrics_list(ctx: WebContext) -> dict[str, Any]:
    return {"ok": True, "items": ctx.db.list_journal_metrics()}


def _journal_metrics_upsert(ctx: WebContext, data: dict[str, Any]) -> tuple[dict[str, Any], int]:
    name = str(data.get("name") or "").strip()
    if not name:
        return {"ok": False, "error": "name 不能为空"}, 400
    full_name = str(data.get("full_name") or "").strip()
    issn = str(data.get("issn") or "").strip()
    try:
        if_value = float(data["if_value"]) if data.get("if_value") not in (None, "") else None
    except (TypeError, ValueError):
        return {"ok": False, "error": "if_value 无效"}, 400
    try:
        cas_zone = int(data["cas_zone"]) if data.get("cas_zone") not in (None, "") else None
        if cas_zone is not None and cas_zone not in (1, 2, 3, 4):
            return {"ok": False, "error": "cas_zone 需为 1–4"}, 400
    except (TypeError, ValueError):
        return {"ok": False, "error": "cas_zone 无效"}, 400
    ok = ctx.db.upsert_journal_metric(name, full_name, if_value, cas_zone, issn)
    if not ok:
        return {"ok": False, "error": "写入失败"}, 500
    return {"ok": True, "item": ctx.db.get_journal_metric(name)}, 200


def _journal_metrics_reseed(ctx: WebContext) -> tuple[dict[str, Any], int]:
    n = ctx.db.reseed_journal_metrics()
    return {"ok": True, "updated": n, "items": ctx.db.list_journal_metrics()}, 200


def _batch_translate_titles(ctx: WebContext, data: dict[str, Any]) -> tuple[dict[str, Any], int]:
    """批量用免费接口翻译标题（默认 MyMemory）。

    传 ids：只翻译这些文章（适合「当前页」）；
    不传 ids：按全局高相关未译条目取 limit 条。
    """
    from utils.translate import looks_chinese, translate_title

    try:
        limit = int(data.get("limit") or 30)
    except (TypeError, ValueError):
        limit = 30
    limit = max(1, min(limit, 100))
    provider = str(data.get("provider") or "mymemory").strip().lower()
    email = str(ctx.config.get("unpaywall_email") or "")
    libre_url = str(data.get("libre_url") or "")
    libre_key = str(data.get("libre_key") or "")

    ids = [int(i) for i in (data.get("ids") or []) if str(i).isdigit()][:limit]
    if ids:
        rows = [
            {"id": a["id"], "title": a.get("title"), "title_zh": a.get("title_zh")}
            for a in ctx.db.get_articles_by_ids(ids)
        ]
    else:
        rows = ctx.db.list_untranslated_titles(limit=limit)

    translated = 0
    skipped = 0
    failed = 0
    for row in rows:
        aid = row.get("id")
        title = (row.get("title") or "").strip()
        if aid is None or not title:
            skipped += 1
            continue
        # 已有译文则跳过（按 ids 重跑时不会重复请求）
        if (row.get("title_zh") or "").strip() and not data.get("force"):
            skipped += 1
            continue
        if looks_chinese(title):
            ctx.db.update_article_fields(aid, title_zh=title)
            skipped += 1
            continue
        zh = translate_title(
            title, provider=provider,
            libre_url=libre_url, libre_key=libre_key, email=email, pause=0.4,
        )
        if not zh:
            # 一次重试，缓解免费接口偶发失败
            time.sleep(1.2)
            zh = translate_title(
                title, provider=provider,
                libre_url=libre_url, libre_key=libre_key, email=email, pause=0.2,
            )
        if zh:
            if ctx.db.update_article_fields(aid, title_zh=zh):
                translated += 1
        else:
            failed += 1
    return {
        "ok": True,
        "candidates": len(rows),
        "translated": translated,
        "skipped": skipped,
        "failed": failed,
        "provider": provider,
    }, 200


def _batch_update(ctx: WebContext, data: dict[str, Any]) -> tuple[dict[str, Any], int]:
    """批量操作：加入阅读清单 / 标记已读等。"""
    ids = data.get("ids") or []
    ids = [int(i) for i in ids if str(i).isdigit()][:200]
    if not ids:
        return {"ok": False, "error": "ids 不能为空"}, 400
    allowed = {"queued", "reading", "read", ""}
    read_status = str(data.get("read_status", "") or "")
    if read_status not in allowed:
        return {"ok": False, "error": "read_status 无效"}, 400
    updated = sum(1 for i in ids if ctx.db.update_article_fields(i, read_status=read_status))
    return {"ok": True, "updated": updated, "requested": len(ids)}, 200


def _batch_delete_articles(ctx: WebContext, data: dict[str, Any]) -> tuple[dict[str, Any], int]:
    """手动删除选中文献（连带聊天/划线/专题/追踪/digest 关联）。"""
    ids = data.get("ids") or []
    ids = [int(i) for i in ids if str(i).isdigit()][:500]
    if not ids:
        return {"ok": False, "error": "ids 不能为空"}, 400
    if not data.get("confirm"):
        return {
            "ok": False,
            "error": "需要 confirm=true 才能删除",
            "requested": len(ids),
        }, 400
    try:
        deleted = ctx.db.delete_articles_by_ids(ids)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"删除失败: {e}"}, 500
    return {"ok": True, "deleted": deleted, "requested": len(ids)}, 200


def _citation(ctx: WebContext, article_id: int, fmt: str) -> tuple[str, int, str]:
    from utils.citation import format_article_citation
    item = _get_article_detail(ctx, article_id)
    if item is None:
        return json.dumps({"error": "article not found"}), 404, "application/json; charset=utf-8"
    text = format_article_citation(item, fmt)
    return text, 200, "text/plain; charset=utf-8"


def _batch_citation(ctx: WebContext, data: dict[str, Any]) -> tuple[str, int, str]:
    from utils.citation import format_article_citation
    fmt = str(data.get("format") or "bibtex").lower()
    if fmt not in {"bibtex", "ris"}:
        return json.dumps({"error": "format 必须是 bibtex 或 ris"}), 400, "application/json; charset=utf-8"
    ids = [int(i) for i in (data.get("ids") or []) if str(i).isdigit()][:200]
    items = ctx.db.get_articles_by_ids(ids)
    items.sort(key=lambda a: ids.index(a["id"]) if a["id"] in ids else 999)
    parts = [format_article_citation(a, fmt) for a in items]
    return "\n\n".join(parts), 200, "text/plain; charset=utf-8"


def _get_article_detail(ctx: WebContext, article_id: int) -> Optional[dict[str, Any]]:
    conn = ctx.connect_db()
    try:
        row = conn.execute(
            "SELECT * FROM articles WHERE id = ?",
            (article_id,),
        ).fetchone()
        if not row:
            return None
        item = dict(row)
        item["topic"] = classify_article(item)
        item["has_analysis"] = bool(item.get("analysis"))
        item["chat_count"] = ctx.db.get_article_chat_count(article_id)
        item["watched"] = ctx.db.is_seed_watched(article_id)
        metric = ctx.db.get_journal_metric(item.get("journal") or "")
        if metric:
            item["impact_factor"] = metric.get("if_value")
            item["cas_zone"] = metric.get("cas_zone")
        else:
            item["impact_factor"] = None
            item["cas_zone"] = None
        return item
    finally:
        conn.close()


def _list_reports(ctx: WebContext, limit: int = 30) -> list[dict[str, Any]]:
    conn = ctx.connect_db()
    try:
        rows = conn.execute(
            "SELECT report_date, file_path, total_found, total_pushed, push_results, kind, created_at "
            "FROM daily_reports ORDER BY report_date DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def _mask_secret(v: Optional[str]) -> str:
    v = v or ""
    if not v:
        return ""
    if len(v) <= 8:
        return "****"
    return v[:4] + "****" + v[-4:]


_SETTINGS_FIELDS = [
    "llm.provider", "llm.model", "llm.api_key", "llm.base_url",
    "relevance_threshold", "research_topics",
    "fetcher.use_fulltext", "fetcher.use_browser",
    "fetcher.max_articles_per_journal", "fetcher.date_filter_days",
    "scheduler.run_time", "web.api_token", "web.api_token_clear", "web.protect_read",
    "output.email_enabled", "output.email_recipients",
    "output.feishu_enabled", "output.feishu_webhook",
    "zotero.enabled", "zotero.user_id", "zotero.api_key", "zotero.collection",
    "zotero.include_note", "zotero.attach_oa_pdf",
    "tracking.enabled", "tracking.citation_check_days", "tracking.author_check_days",
]


def _settings_view(ctx: WebContext) -> dict[str, Any]:
    """设置页数据：脱敏配置 + 可编辑字段清单。密钥只回传掩码，绝不回传明文。"""
    c = ctx.config
    llm = c.get("llm", {}) or {}
    provider = llm.get("provider", "")
    provider_cfg = llm.get(provider, {}) or {}
    fetcher = c.get("fetcher", {}) or {}
    output = c.get("output", {}) or {}
    email = output.get("email", {}) or {}
    feishu = output.get("feishu", {}) or {}
    web = c.get("web", {}) or {}
    zotero = c.get("zotero", {}) or {}
    tracking_cfg = c.get("tracking", {}) or {}

    # 已配置的 provider 列表（供前端切换时回填 base_url / model）
    known_providers: dict[str, dict[str, Any]] = {}
    for name, pcfg in llm.items():
        if isinstance(pcfg, dict) and (pcfg.get("base_url") or pcfg.get("model") or pcfg.get("api_key")):
            known_providers[name] = {
                "base_url": pcfg.get("base_url", ""),
                "model": pcfg.get("model", ""),
                "api_key_set": bool(pcfg.get("api_key")),
            }

    return {
        "editable": _SETTINGS_FIELDS,
        "llm": {
            "provider": provider,
            "model": provider_cfg.get("model", ""),
            "base_url": provider_cfg.get("base_url", ""),
            "api_key_set": bool(provider_cfg.get("api_key")),
            "api_key_masked": _mask_secret(provider_cfg.get("api_key")),
            "providers": known_providers,
        },
        "relevance_threshold": c.get("relevance_threshold"),
        "research_topics": c.get("research_topics", []),
        "fetcher": {
            "use_fulltext": fetcher.get("use_fulltext"),
            "use_browser": fetcher.get("use_browser"),
            "max_articles_per_journal": fetcher.get("max_articles_per_journal"),
            "date_filter_days": fetcher.get("date_filter_days"),
        },
        "scheduler": c.get("scheduler", {}),
        "web": {
            "api_token_set": bool(web.get("api_token")),
            "enable_scheduler": web.get("enable_scheduler", False),
            "protect_read": bool(web.get("protect_read", False)),
        },
        "zotero": {
            "enabled": bool(zotero.get("enabled")),
            "user_id": zotero.get("user_id", ""),
            "api_key_set": bool(zotero.get("api_key")),
            "collection": zotero.get("collection", ""),
            "include_note": bool(zotero.get("include_note", True)),
            "attach_oa_pdf": bool(zotero.get("attach_oa_pdf", True)),
        },
        "tracking": {
            "enabled": bool(tracking_cfg.get("enabled", True)),
            "citation_check_days": tracking_cfg.get("citation_check_days", 3),
            "author_check_days": tracking_cfg.get("author_check_days", 3),
        },
        "output": {
            "email_enabled": bool(email.get("enabled")),
            "email_recipients": email.get("recipients", []),
            "email_username": email.get("username", ""),
            "feishu_enabled": bool(feishu.get("enabled")),
        },
        "config_path": ctx.config_path,
    }


_SETTINGS_LOCK = threading.Lock()


def _save_settings(ctx: WebContext, data: dict[str, Any]) -> tuple[dict[str, Any], int]:
    """把白名单字段写回 config.yaml（ruamel 保留注释）。

    密钥类字段（llm.api_key / web.api_token）留空 = 不修改；
    web.api_token_clear=true = 显式清除 Token。
    校验通过后才原子写入（临时文件 + rename），非法配置不会覆盖原文件。
    """
    from ruamel.yaml import YAML
    from ruamel.yaml.comments import CommentedMap

    allowed = set(_SETTINGS_FIELDS)
    updates = {k: v for k, v in (data or {}).items() if k in allowed}
    if not updates:
        return {"ok": False, "error": "没有可更新的字段（或字段不在白名单内）"}, 400

    yaml_io = YAML()
    yaml_io.preserve_quotes = True
    cfg_path = Path(ctx.config_path)

    with _SETTINGS_LOCK:
        try:
            with open(cfg_path, encoding="utf-8") as f:
                cfg = yaml_io.load(f)
            if cfg is None:
                cfg = CommentedMap()
        except FileNotFoundError:
            return {"ok": False, "error": f"配置文件不存在: {cfg_path}"}, 400

        def ensure(keys: list) -> CommentedMap:
            node = cfg
            for k in keys:
                if k not in node or not isinstance(node[k], dict):
                    node[k] = CommentedMap()
                node = node[k]
            return node

        provider = str(updates.get("llm.provider") or cfg.get("llm", {}).get("provider") or "deepseek")
        changed: list[str] = []

        try:
            for key, value in updates.items():
                if key == "llm.provider":
                    # 自定义提供方：任意字母/数字/下划线/短横线组合
                    s = str(value).strip()
                    if s and re.match(r"^[A-Za-z0-9_-]{1,32}$", s):
                        ensure(["llm"])["provider"] = s
                        changed.append(key)
                elif key == "llm.model":
                    if value:
                        ensure(["llm", provider])["model"] = str(value)
                        changed.append(key)
                elif key == "llm.base_url":
                    s = str(value).strip()
                    if s:
                        if not s.lower().startswith(("http://", "https://")):
                            return {"ok": False, "error": "Base URL 必须以 http:// 或 https:// 开头"}, 400
                        ensure(["llm", provider])["base_url"] = s
                        changed.append(key)
                elif key == "llm.api_key":
                    if value:  # 留空 = 保持不变
                        ensure(["llm", provider])["api_key"] = str(value)
                        changed.append(key)
                elif key == "relevance_threshold":
                    v = float(value)
                    if 0 <= v <= 10:
                        cfg["relevance_threshold"] = v
                        changed.append(key)
                elif key == "research_topics":
                    topics = [str(t).strip() for t in (value or []) if str(t).strip()]
                    if topics:
                        cfg["research_topics"] = topics
                        changed.append(key)
                elif key == "scheduler.run_time":
                    s = str(value).strip()
                    if re.match(r"^\d{1,2}:\d{2}$", s):
                        ensure(["scheduler"])["run_time"] = s
                        changed.append(key)
                elif key == "web.api_token":
                    if value:  # 留空 = 保持不变
                        ensure(["web"])["api_token"] = str(value)
                        changed.append(key)
                elif key == "web.api_token_clear":
                    if value:
                        ensure(["web"])["api_token"] = ""
                        changed.append("web.api_token")
                elif key == "web.protect_read":
                    ensure(["web"])["protect_read"] = bool(value)
                    changed.append(key)
                elif key.startswith("zotero."):
                    sub = key.split(".", 1)[1]
                    node = ensure(["zotero"])
                    if sub in ("enabled", "include_note", "attach_oa_pdf"):
                        node[sub] = bool(value)
                    elif sub == "api_key":
                        if value:  # 留空 = 保持不变
                            node[sub] = str(value)
                    else:
                        node[sub] = str(value or "").strip()
                    changed.append(key)
                elif key.startswith("tracking."):
                    sub = key.split(".", 1)[1]
                    node = ensure(["tracking"])
                    if sub == "enabled":
                        node[sub] = bool(value)
                    else:
                        node[sub] = max(1, int(value))
                    changed.append(key)
                elif key == "output.email_enabled":
                    ensure(["output", "email"])["enabled"] = bool(value)
                    changed.append(key)
                elif key == "output.email_recipients":
                    recips = [str(r).strip() for r in (value or []) if str(r).strip()]
                    ensure(["output", "email"])["recipients"] = recips
                    changed.append(key)
                elif key == "output.feishu_enabled":
                    ensure(["output", "feishu"])["enabled"] = bool(value)
                    changed.append(key)
                elif key == "output.feishu_webhook":
                    if value:
                        ensure(["output", "feishu"])["webhook_url"] = str(value)
                        changed.append(key)
                elif key.startswith("fetcher."):
                    sub = key.split(".", 1)[1]
                    node = ensure(["fetcher"])
                    if sub == "max_articles_per_journal":
                        node[sub] = max(1, int(value))
                    elif sub == "date_filter_days":
                        node[sub] = max(0, int(value))
                    else:
                        node[sub] = bool(value)
                    changed.append(key)
        except (ValueError, TypeError) as e:
            return {"ok": False, "error": f"字段值格式无效: {e}"}, 400

        if not changed:
            return {"ok": False, "error": "没有字段通过校验"}, 400

        # ★ 写入前先用更新后的配置跑一次启动校验，非法配置不落盘
        try:
            validate_config(load_config_from_obj(cfg))
        except Exception as e:
            return {"ok": False, "error": f"配置校验未通过，未保存: {e}"}, 400

        # ★ 原子写入：先写临时文件再 rename，避免半写状态损坏配置
        tmp_path = cfg_path.with_suffix(cfg_path.suffix + ".tmp")
        with open(tmp_path, "w", encoding="utf-8") as f:
            yaml_io.dump(cfg, f)
        os.replace(tmp_path, cfg_path)

    # 写入成功后热重载（后续任务/Token/调度均用新配置）
    try:
        ctx.config = load_config(ctx.config_path)
        validate_config(ctx.config)
    except Exception as e:
        return {"ok": True, "changed": changed, "warning": f"已写入文件，但重载校验失败: {e}"}, 200

    # 热更新：任务基线配置与 API Token 立即生效；调度时间变更则重启调度线程
    ctx.runner.update_base_config(ctx.config)
    ctx.api_token = os.environ.get("WEB_API_TOKEN") or ctx.config.get("web", {}).get("api_token")
    if "scheduler.run_time" in changed and ctx.enable_scheduler:
        new_run_time = str(ctx.config.get("scheduler", {}).get("run_time", "08:00"))
        ctx.runner.start_scheduler(run_time=new_run_time)
        logger.info("scheduler.run_time 已变更，调度线程已按 %s 重启", new_run_time)

    return {"ok": True, "changed": changed}, 200


def _db_summary(ctx: WebContext) -> dict[str, Any]:
    conn = ctx.connect_db()
    try:
        total = conn.execute("SELECT COUNT(*) FROM articles").fetchone()[0]
        analyzed = conn.execute(
            "SELECT COUNT(*) FROM articles WHERE analysis IS NOT NULL AND analysis != ''"
        ).fetchone()[0]
        avg_score = conn.execute("SELECT ROUND(AVG(COALESCE(relevance, 0)), 2) FROM articles").fetchone()[0]
        return {
            "total_articles": total,
            "analyzed_articles": analyzed,
            "avg_relevance": avg_score or 0,
        }
    finally:
        conn.close()


# ── 期刊订阅管理 ──────────────────────────────────────────────

def _read_json_body(handler: "Handler") -> dict[str, Any]:
    length = int(handler.headers.get("Content-Length", "0"))
    body = handler.rfile.read(length) if length > 0 else b"{}"
    if not body.strip():
        return {}
    return json.loads(body.decode("utf-8"))


def _list_journals(ctx: WebContext) -> dict[str, Any]:
    journals = ctx.db.list_journals()
    return {"items": journals, "count": len(journals)}


def _add_journal(ctx: WebContext, data: dict[str, Any]) -> tuple[dict[str, Any], int]:
    rss = (data.get("rss") or "").strip()
    if not rss:
        return {"ok": False, "error": "RSS 链接不能为空"}, 400
    if not rss.lower().startswith(("http://", "https://")):
        return {"ok": False, "error": "RSS 链接必须以 http:// 或 https:// 开头"}, 400

    if ctx.db.get_journal_by_rss(rss) is not None:
        return {"ok": False, "error": "该 RSS 链接已存在"}, 409

    name = (data.get("name") or "").strip()
    publisher = (data.get("publisher") or "").strip()
    if not publisher:
        publisher = detect_publisher_from_url(rss)
    max_articles = _to_int(str(data.get("max_articles", "100")), 100, 1, 500)

    journal_id = ctx.db.add_journal(
        name=name, rss=rss, publisher=publisher, max_articles=max_articles
    )
    if journal_id is None:
        return {"ok": False, "error": "该 RSS 链接已存在或保存失败"}, 409

    item = ctx.db.get_journal_by_rss(rss)
    return {"ok": True, "id": journal_id, "item": item}, 200


def _test_journal(ctx: WebContext, data: dict[str, Any]) -> tuple[dict[str, Any], int]:
    rss = (data.get("rss") or "").strip()
    if not rss:
        return {"ok": False, "error": "RSS 链接不能为空"}, 400
    publisher = (data.get("publisher") or "").strip() or detect_publisher_from_url(rss)
    result = ctx.fetcher.test_feed(rss, publisher=publisher)
    result["publisher"] = publisher
    return result, 200


def _toggle_journal(ctx: WebContext, journal_id: int) -> tuple[dict[str, Any], int]:
    journals = ctx.db.list_journals()
    item = next((j for j in journals if j["id"] == journal_id), None)
    if item is None:
        return {"ok": False, "error": "订阅不存在"}, 404
    new_enabled = not bool(item.get("enabled"))
    ctx.db.set_journal_enabled(journal_id, new_enabled)
    return {"ok": True, "id": journal_id, "enabled": new_enabled}, 200


def _delete_journal(ctx: WebContext, journal_id: int) -> tuple[dict[str, Any], int]:
    ctx.db.delete_journal(journal_id)
    return {"ok": True, "id": journal_id}, 200


def _parse_opml(text: str, publisher_override: str) -> Optional[list[tuple[str, str, str]]]:
    """解析 OPML XML，返回 (name, rss, publisher) 列表；解析失败返回 None。"""
    import xml.etree.ElementTree as ET

    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return None

    entries: list[tuple[str, str, str]] = []
    for outline in root.iter("outline"):
        xml_url = outline.get("xmlUrl") or outline.get("xmlurl") or ""
        if not xml_url:
            continue
        name = outline.get("text") or outline.get("title") or ""
        entries.append((name, xml_url, publisher_override))
    return entries


def _import_journals(ctx: WebContext, data: dict[str, Any]) -> tuple[dict[str, Any], int]:
    text = (data.get("text") or "").strip()
    if not text:
        return {"ok": False, "error": "内容为空，请粘贴链接或 OPML"}, 400

    publisher_override = (data.get("publisher") or "").strip()

    if text.lstrip().startswith("<"):
        entries = _parse_opml(text, publisher_override)
        if entries is None:
            return {"ok": False, "error": "OPML 解析失败，请检查 XML 格式"}, 400
    else:
        entries = []
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            entries.append(("", line, publisher_override))

    added: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for name, rss, publisher in entries:
        rss = (rss or "").strip()
        if not rss.lower().startswith(("http://", "https://")):
            skipped.append({"url": rss, "reason": "无效链接"})
            continue
        if ctx.db.get_journal_by_rss(rss):
            skipped.append({"url": rss, "reason": "已存在"})
            continue
        p = (publisher or "").strip() or detect_publisher_from_url(rss)
        jid = ctx.db.add_journal(name=(name or "").strip(), rss=rss, publisher=p)
        if jid is None:
            skipped.append({"url": rss, "reason": "保存失败"})
        else:
            added.append({"id": jid, "name": (name or "").strip(), "rss": rss, "publisher": p})

    return {
        "ok": True,
        "added": added,
        "skipped": skipped,
        "added_count": len(added),
        "skipped_count": len(skipped),
    }, 200


# ── 静态前端 ──────────────────────────────────────────────────

def _find_static_dir() -> Path:
    """静态资源目录：源码运行时为 src/static；pip 安装后在 webassets 包内。

    两个分支都必须返回 resolve() 后的路径：/_serve_static_file 用
    relative_to 做目录穿越校验，macOS 上 /tmp 与 /private/tmp 不一致会误判。
    """
    src_dir = (Path(__file__).resolve().parent / "static").resolve()
    if src_dir.is_dir():
        return src_dir
    import importlib.util
    spec = importlib.util.find_spec("webassets")
    if spec and spec.submodule_search_locations:
        return Path(list(spec.submodule_search_locations)[0]).resolve()
    return src_dir


STATIC_DIR = _find_static_dir()

_STATIC_CONTENT_TYPES: dict[str, str] = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".mjs": "application/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".ico": "image/x-icon",
    ".woff2": "font/woff2",
}


# ── 个人文献库：星标 / 笔记 / 标签 / 对话 ──────────────────────

def _list_tags(ctx: WebContext) -> dict[str, Any]:
    return {"items": ctx.db.list_all_tags()}


def _article_action_result(
    ctx: WebContext, article_id: int
) -> tuple[dict[str, Any], int]:
    """文章字段更新成功后，返回最新详情供前端刷新。"""
    item = _get_article_detail(ctx, article_id)
    if item is None:
        return {"ok": False, "error": "article not found"}, 404
    return {"ok": True, "id": article_id, "item": item}, 200


def _set_article_star(ctx: WebContext, article_id: int, data: dict[str, Any]) -> tuple[dict[str, Any], int]:
    starred = bool(data.get("starred"))
    ctx.db.set_article_star(article_id, starred)
    return _article_action_result(ctx, article_id)


def _set_article_note(ctx: WebContext, article_id: int, data: dict[str, Any]) -> tuple[dict[str, Any], int]:
    note = str(data.get("note", "") or "")
    if len(note) > 20000:
        return {"ok": False, "error": "笔记过长（上限 20000 字符）"}, 400
    ctx.db.update_article_note(article_id, note)
    return _article_action_result(ctx, article_id)


def _set_article_tags(ctx: WebContext, article_id: int, data: dict[str, Any]) -> tuple[dict[str, Any], int]:
    tags = data.get("tags", [])
    if isinstance(tags, str):
        tags = tags.split(",")
    if len(list(tags or [])) > 20:
        return {"ok": False, "error": "标签数量过多（上限 20 个）"}, 400
    ctx.db.update_article_tags(article_id, tags)
    return _article_action_result(ctx, article_id)


def _test_llm(ctx: WebContext, data: dict[str, Any]) -> tuple[dict[str, Any], int]:
    """测试 LLM API 可用性。

    表单值可选传入（provider/base_url/model/api_key）；api_key 留空时
    回退到该 provider 已保存的 key（便于先测试再保存，也能测已有配置）。
    """
    llm_cfg = ctx.config.get("llm", {}) or {}
    provider = str(data.get("provider") or "").strip() or str(llm_cfg.get("provider") or "deepseek")
    provider_cfg = llm_cfg.get(provider, {}) or {}
    if not isinstance(provider_cfg, dict):
        provider_cfg = {}

    base_url = str(data.get("base_url") or "").strip() or str(provider_cfg.get("base_url") or "").strip()
    model = str(data.get("model") or "").strip() or str(provider_cfg.get("model") or "").strip()
    api_key = str(data.get("api_key") or "").strip() or str(provider_cfg.get("api_key") or "").strip()

    missing = [
        label for label, val in
        [("Base URL", base_url), ("模型名", model), ("API Key", api_key)]
        if not val
    ]
    if missing:
        return {"ok": False, "error": f"缺少 {'、'.join(missing)}（表单和已保存配置中都没有）"}, 400
    if not base_url.lower().startswith(("http://", "https://")):
        return {"ok": False, "error": "Base URL 必须以 http:// 或 https:// 开头"}, 400

    import openai as _openai
    client = _openai.OpenAI(api_key=api_key, base_url=base_url, timeout=25, max_retries=0)
    started = time.monotonic()
    try:
        resp = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": "这是一次连通性测试，请只回复两个字：正常"}],
            max_tokens=512,
            temperature=0,
        )
    except _openai.AuthenticationError as e:
        return {"ok": False, "error": f"认证失败：API key 无效或账户余额不足（{e}）"}, 200
    except _openai.RateLimitError as e:
        return {"ok": False, "error": f"速率限制/配额不足（{e}）"}, 200
    except _openai.NotFoundError as e:
        return {"ok": False, "error": f"接口地址或模型名不存在（{e}）"}, 200
    except _openai.APITimeoutError as e:
        return {"ok": False, "error": f"请求超时（25s）：{e}"}, 200
    except _openai.APIStatusError as e:
        return {"ok": False, "error": f"API 返回错误 HTTP {getattr(e, 'status_code', '?')}：{e}"}, 200
    except Exception as e:  # noqa: BLE001 - 连通性测试需要把任何失败反馈给前端
        return {"ok": False, "error": f"连接失败：{type(e).__name__}: {e}"}, 200

    latency_ms = int((time.monotonic() - started) * 1000)
    reply = ""
    if getattr(resp, "choices", None):
        reply = (resp.choices[0].message.content or "").strip()
    return {
        "ok": True,
        "latency_ms": latency_ms,
        "model": model,
        "base_url": base_url,
        "provider": provider,
        "reply": reply,
    }, 200


# ── 研究工作台：Zotero / 追踪 / 专题 / 对比 / 趋势 / 高亮 ─────

def _zotero_push(ctx: WebContext, article_id: int) -> tuple[dict[str, Any], int]:
    item = _get_article_detail(ctx, article_id)
    if item is None:
        return {"ok": False, "error": "article not found"}, 404
    if item.get("zotero_key"):
        return {"ok": True, "key": item["zotero_key"], "already": True}, 200
    try:
        from integrations.zotero_client import ZoteroClient
        client = ZoteroClient(ctx.config)
        key = client.push_article(item)
    except Exception as e:  # noqa: BLE001 - 推送失败原因需要回传前端
        logger.error("Zotero 推送失败: %s", e, exc_info=True)
        return {"ok": False, "error": f"推送失败: {e}"}, 500
    ctx.db.update_article_fields(article_id, zotero_key=key)
    return {"ok": True, "key": key}, 200


def _zotero_batch(ctx: WebContext, data: dict[str, Any]) -> tuple[dict[str, Any], int]:
    ids = [int(i) for i in (data.get("ids") or []) if str(i).isdigit()][:100]
    if not ids:
        return {"ok": False, "error": "ids 不能为空"}, 400
    try:
        from integrations.zotero_client import ZoteroClient
        client = ZoteroClient(ctx.config)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}, 400
    pushed, skipped, failed = 0, 0, []
    for aid in ids:
        item = _get_article_detail(ctx, aid)
        if item is None:
            failed.append({"id": aid, "error": "not found"})
            continue
        if item.get("zotero_key"):
            skipped += 1
            continue
        try:
            key = client.push_article(item)
            ctx.db.update_article_fields(aid, zotero_key=key)
            pushed += 1
        except Exception as e:  # noqa: BLE001
            failed.append({"id": aid, "error": str(e)[:200]})
    return {"ok": True, "pushed": pushed, "skipped": skipped, "failed": failed}, 200


def _zotero_test(ctx: WebContext, data: Optional[dict[str, Any]] = None) -> tuple[dict[str, Any], int]:
    """测试 Zotero 连接。表单可传 api_key/user_id（未保存也可先测）；留空回退已保存配置。"""
    try:
        from integrations.zotero_client import ZoteroClient
        cfg = copy.deepcopy(ctx.config)
        zot = dict(cfg.get("zotero") or {})
        data = data or {}
        form_key = str(data.get("api_key") or "").strip()
        form_uid = str(data.get("user_id") or "").strip()
        if form_key:
            zot["api_key"] = form_key
        if form_uid:
            zot["user_id"] = form_uid
        cfg["zotero"] = zot
        client = ZoteroClient(cfg)
        return client.test_connection(), 200
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}, 200


def _set_watch(ctx: WebContext, article_id: int, data: dict[str, Any]) -> tuple[dict[str, Any], int]:
    active = bool(data.get("active"))
    ctx.db.set_watch_seed(article_id, active)
    return {"ok": True, "id": article_id, "active": active}, 200


def _author_search(ctx: WebContext, data: dict[str, Any]) -> tuple[dict[str, Any], int]:
    from integrations import openalex
    openalex.set_polite_email(ctx.config.get("unpaywall_email", "your@email.com"))
    name = str(data.get("name") or "").strip()
    if len(name) < 2:
        return {"ok": False, "error": "name 过短"}, 400
    return {"ok": True, "items": openalex.search_authors(name, limit=6)}, 200


def _topics_list(ctx: WebContext) -> dict[str, Any]:
    return {"items": ctx.db.list_topics()}


def _topic_detail(ctx: WebContext, topic_id: int):
    topic = ctx.db.get_topic(topic_id)
    if topic is None:
        return {"error": "topic not found"}, 404
    return topic, 200


def _run_tool(ctx: WebContext, data: dict[str, Any], kind: str) -> tuple[dict[str, Any], int]:
    ids = [int(i) for i in (data.get("ids") or []) if str(i).isdigit()]
    max_ids = 4 if kind == "compare" else 8
    ids = ids[:max_ids]
    if len(ids) < 2:
        return {"ok": False, "error": "至少选择 2 篇文献"}, 400
    items = ctx.db.get_articles_by_ids(ids)
    if len(items) < 2:
        return {"ok": False, "error": "文献不足"}, 400
    try:
        analyzer = LLMAnalyzer(ctx.config)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"LLM 初始化失败: {e}"}, 500
    try:
        if kind == "compare":
            content = analyzer.compare_articles(items)
        else:
            content = analyzer.related_work_draft(items, str(data.get("focus") or ""))
    except Exception as e:  # noqa: BLE001
        logger.error("研究工具生成失败: %s", e, exc_info=True)
        return {"ok": False, "error": f"生成失败: {e}"}, 500
    rid = ctx.db.save_result(kind, ids, content, analyzer.model)
    return {"ok": True, "id": rid}, 200


def _trends_view(ctx: WebContext, query: dict[str, list[str]]) -> dict[str, Any]:
    months = _to_int(query.get("months", ["6"])[0], 6, 2, 24)
    threshold = float(ctx.config.get("relevance_threshold", 4))
    conn = ctx.connect_db()
    try:
        monthly = conn.execute(
            "SELECT strftime('%Y-%m', date(created_at, 'localtime')) AS m, COUNT(*), "
            "SUM(CASE WHEN COALESCE(relevance, 0) >= ? THEN 1 ELSE 0 END) "
            "FROM articles GROUP BY m ORDER BY m DESC LIMIT ?",
            (threshold, months)).fetchall()
        journals = conn.execute(
            "SELECT journal, COUNT(*) AS total, "
            "SUM(CASE WHEN COALESCE(relevance, 0) >= ? THEN 1 ELSE 0 END) AS relevant "
            "FROM articles WHERE journal IS NOT NULL AND journal != '' "
            "GROUP BY journal ORDER BY relevant DESC LIMIT 12",
            (threshold,)).fetchall()
        via = conn.execute(
            "SELECT COALESCE(discovered_via, 'rss') AS src, COUNT(*) FROM articles "
            "GROUP BY src ORDER BY 2 DESC").fetchall()
        statuses = conn.execute(
            "SELECT COALESCE(read_status, '') AS st, COUNT(*) FROM articles GROUP BY st").fetchall()
        return {
            "monthly": [{"month": r[0], "total": r[1], "relevant": r[2] or 0} for r in monthly],
            "journals": [{"journal": r[0], "total": r[1], "relevant": r[2] or 0} for r in journals],
            "discovered_via": [{"via": r[0], "count": r[1]} for r in via],
            "read_status": [{"status": r[0] or "未加入清单", "count": r[1]} for r in statuses],
        }
    finally:
        conn.close()



def _digest_view(ctx: WebContext, date_str: str = "", dry_run: bool = True) -> dict[str, Any]:
    """每日 Top-N 选文（digest 模块的 HTTP 入口）。

    GET  = dry-run 只读；POST = 正式落盘 digest_entries。
    """
    from digest.builder import build_daily_digest
    result = build_daily_digest(ctx.db, ctx.config, date_str=date_str or None, dry_run=dry_run)
    sel = result["selection"]
    selected = []
    for i, row in enumerate(sel.selected, 1):
        scores = row.get("scores") or {}
        selected.append({
            "rank": i,
            "id": row.get("id"),
            "title": row.get("title"),
            "journal": row.get("journal"),
            "pub_date": row.get("pub_date"),
            "url": row.get("url"),
            "topic": row.get("topic"),
            "relevance_reason": (row.get("relevance_reason") or "")[:300],
            "title_zh": row.get("title_zh") or "",
            "scores": scores,
        })
    from digest.builder import format_dry_run_report
    return {
        "date": result["date"],
        "dry_run": result["dry_run"],
        "articles_above_threshold": result["articles_above_threshold"],
        "excluded_repeat": sel.stats.get("excluded_repeat", 0),
        "stats": sel.stats,
        "selected": selected,
        "report_text": format_dry_run_report(result),
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "DailyPaperWeb/0.1"

    @property
    def ctx(self) -> WebContext:
        return self.server.context  # type: ignore[attr-defined]

    def _json_response(self, payload: dict[str, Any], code: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _text_response(self, text: str, code: int = 200, content_type: str = "text/plain; charset=utf-8",
                       filename: Optional[str] = None) -> None:
        body = text.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        if filename:
            from urllib.parse import quote
            self.send_header("Content-Disposition", f"attachment; filename*=UTF-8''{quote(filename)}")
        self.end_headers()
        self.wfile.write(body)

    def _serve_file(self, path: Path, content_type: str) -> None:
        if not path.exists() or not path.is_file():
            self._json_response({"error": "file not found", "path": str(path)}, code=404)
            return
        body = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_static_file(self, rel_path: str) -> None:
        """服务 src/static/ 下的静态文件（防目录穿越，禁缓存）。"""
        rel_path = (rel_path or "").strip().lstrip("/")
        if not rel_path:
            rel_path = "index.html"
        target = (STATIC_DIR / rel_path).resolve()
        try:
            target.relative_to(STATIC_DIR)
        except ValueError:
            self._json_response({"error": "bad path"}, code=400)
            return
        if not target.is_file():
            self._json_response({"error": "not found"}, code=404)
            return
        content_type = _STATIC_CONTENT_TYPES.get(
            target.suffix.lower(), "application/octet-stream"
        )
        body = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def _sse_start(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True

    def _sse_send(self, payload: dict[str, Any]) -> bool:
        """发送一条 SSE 事件；连接断开时返回 False。"""
        data = json.dumps(payload, ensure_ascii=False)
        try:
            self.wfile.write(f"data: {data}\n\n".encode("utf-8"))
            self.wfile.flush()
            return True
        except (BrokenPipeError, ConnectionResetError):
            return False

    def _handle_chat_stream(self, article_id: int) -> None:
        """POST /api/articles/{id}/chat：SSE 流式回答，并把对话落库。

        事件协议：{"context": 依据范围} → {"delta": ...} → {"done": true}；
        失败发 {"error": ...} 且不落库伪回答。regenerate=true 时不重复记录问题。
        """
        try:
            data = _read_json_body(self)
        except json.JSONDecodeError:
            self._json_response({"error": "invalid json"}, code=400)
            return

        question = str(data.get("question") or "").strip()
        regenerate = bool(data.get("regenerate"))
        if not question:
            self._json_response({"error": "question 不能为空"}, code=400)
            return
        if len(question) > 8000:
            self._json_response({"error": "question 过长（上限 8000 字符）"}, code=400)
            return

        article = _get_article_detail(self.ctx, article_id)
        if article is None:
            self._json_response({"error": "article not found"}, code=404)
            return

        try:
            analyzer = LLMAnalyzer(self.ctx.config)
        except Exception as e:  # noqa: BLE001 - 配置缺失时给前端明确错误
            self._json_response({"error": f"LLM 初始化失败，请检查配置: {e}"}, code=500)
            return

        history = self.ctx.db.get_chat_messages(article_id)
        if regenerate:
            # 问题已在上次落库，去掉末尾重复的用户消息，避免上下文重复
            if history and history[-1].get("role") == "user" and history[-1].get("content") == question:
                history = history[:-1]
        else:
            self.ctx.db.add_chat_message(article_id, "user", question)

        self._sse_start()
        try:
            self._sse_send({"context": analyzer.chat_context_summary(article)})
        except Exception:  # noqa: BLE001 - 上下文说明失败不阻断对话
            pass

        pieces: list[str] = []
        try:
            for delta in analyzer.chat_with_article(article, history, question):
                pieces.append(delta)
                if not self._sse_send({"delta": delta}):
                    break
        except Exception as e:  # noqa: BLE001 - 流式兜底，保证前端拿到 error 事件
            logger.error("文献对话流式输出失败: %s", e, exc_info=True)
            self._sse_send({"error": f"对话失败: {e}"})

        # 只落库真实回答；错误信息不伪装成 assistant 消息保存
        answer = "".join(pieces)
        if answer:
            self.ctx.db.add_chat_message(article_id, "assistant", answer)
        self._sse_send({"done": True})

    def _require_token(self) -> bool:
        token = self.ctx.api_token
        if not token:
            return True
        req_token = self.headers.get("X-API-Token", "")
        # iframe/直链无法带 Header，允许 ?token= 查询参数
        if not req_token:
            q = parse_qs(urlparse(self.path).query)
            req_token = (q.get("token") or [""])[0]
        if req_token == token:
            return True
        self._json_response({"error": "unauthorized"}, code=401)
        return False

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)

        try:
            # web.protect_read=true 时 GET API 也要求 Token（静态页与连接入口除外）
            if (path.startswith("/api/") and path not in {"/healthz", "/api/auth/token"}
                    and (self.ctx.config.get("web", {}) or {}).get("protect_read")):
                if not self._require_token():
                    return

            if path == "/api/auth/token":
                # 公开：供新浏览器连接已有 Token（不校验 X-API-Token，只验证 body）
                self._json_response({
                    "ok": True,
                    "required": bool(self.ctx.api_token),
                    "protect_read": bool((self.ctx.config.get("web") or {}).get("protect_read")),
                })
                return

            if path == "/" or path == "/dashboard":
                self._serve_static_file("index.html")
                return

            if path.startswith("/results/"):
                self._serve_static_file("result.html")
                return

            if path.startswith("/article/"):
                pid = path.removeprefix("/article/")
                if not pid.isdigit():
                    self._json_response({"error": "invalid article id"}, code=400)
                    return
                self._serve_static_file("article.html")
                return

            if path.startswith("/static/"):
                self._serve_static_file(path.removeprefix("/static/"))
                return

            if path == "/healthz":
                self._json_response({"ok": True, "time": datetime.now().isoformat(timespec="seconds")})
                return

            if path == "/api/journal-metrics":
                self._json_response(_journal_metrics_list(self.ctx))
                return

            if path == "/paper-index":
                if (self.ctx.config.get("web") or {}).get("protect_read") and not self._require_token():
                    return
                self._serve_file(self.ctx.output_dir / "paper_index.html", "text/html; charset=utf-8")
                return

            if path.startswith("/reports/"):
                if (self.ctx.config.get("web") or {}).get("protect_read") and not self._require_token():
                    return
                filename = path.removeprefix("/reports/")
                safe_name = Path(filename).name
                if safe_name != filename:
                    self._json_response({"error": "bad filename"}, code=400)
                    return
                suffix = Path(safe_name).suffix.lower()
                ctype = "text/plain; charset=utf-8"
                if suffix == ".html":
                    ctype = "text/html; charset=utf-8"
                elif suffix == ".md":
                    ctype = "text/markdown; charset=utf-8"
                self._serve_file(self.ctx.output_dir / safe_name, ctype)
                return

            if path == "/api/status":
                self._json_response({
                    "task": self.ctx.runner.get_state(),
                    "db": _db_summary(self.ctx),
                    "output_dir": str(self.ctx.output_dir),
                    "db_path": str(self.ctx.db_path),
                })
                return

            if path == "/api/topics":
                self._json_response(_topics_list(self.ctx))
                return

            if path.startswith("/api/topics/") and path.count("/") == 3:
                tid = path.removeprefix("/api/topics/")
                if not tid.isdigit():
                    self._json_response({"error": "invalid topic id"}, code=400)
                    return
                topic, code = _topic_detail(self.ctx, int(tid))
                self._json_response(topic, code=code)
                return

            if path == "/api/watch/authors":
                self._json_response({"items": self.ctx.db.list_watch_authors(enabled_only=False)})
                return

            if path == "/api/stats/trends":
                self._json_response(_trends_view(self.ctx, query))
                return

            if path == "/api/digest":
                self._json_response(_digest_view(
                    self.ctx, (query.get("date", [""])[0] or "").strip(), dry_run=True))
                return

            if path == "/api/results":
                self._json_response({"items": self.ctx.db.list_results()})
                return

            if path.startswith("/api/results/"):
                rid = path.removeprefix("/api/results/")
                if not rid.isdigit():
                    self._json_response({"error": "invalid result id"}, code=400)
                    return
                item = self.ctx.db.get_result(int(rid))
                if item is None:
                    self._json_response({"error": "not found"}, code=404)
                    return
                self._json_response(item)
                return

            if path.startswith("/api/articles/") and path.endswith("/highlights"):
                aid_raw = path.removeprefix("/api/articles/").removesuffix("/highlights")
                if not aid_raw.isdigit():
                    self._json_response({"error": "invalid article id"}, code=400)
                    return
                self._json_response({"items": self.ctx.db.get_highlights(int(aid_raw))})
                return

            if path == "/api/today":
                self._json_response(_today_view(self.ctx, query))
                return

            if path == "/api/articles":
                self._json_response(_list_articles(self.ctx, query))
                return

            if path.startswith("/api/articles/") and path.endswith("/chat"):
                article_id_raw = path.removeprefix("/api/articles/").removesuffix("/chat")
                if not article_id_raw.isdigit():
                    self._json_response({"error": "invalid article id"}, code=400)
                    return
                messages = self.ctx.db.get_chat_messages(int(article_id_raw))
                self._json_response({"items": messages, "count": len(messages)})
                return

            if path.startswith("/api/articles/"):
                article_id_raw = path.removeprefix("/api/articles/")
                if article_id_raw.endswith("/citation"):
                    aid = article_id_raw.removesuffix("/citation")
                    if not aid.isdigit():
                        self._json_response({"error": "invalid article id"}, code=400)
                        return
                    fmt = (query.get("format", ["bibtex"])[0] or "bibtex").lower()
                    body, code, ctype = _citation(self.ctx, int(aid), fmt)
                    self._text_response(body, code=code, content_type=ctype)
                    return
                if not article_id_raw.isdigit():
                    self._json_response({"error": "invalid article id"}, code=400)
                    return
                item = _get_article_detail(self.ctx, int(article_id_raw))
                if not item:
                    self._json_response({"error": "article not found"}, code=404)
                    return
                self._json_response(item)
                return

            if path == "/api/tags":
                self._json_response(_list_tags(self.ctx))
                return

            if path == "/api/reports":
                limit = _to_int(query.get("limit", ["30"])[0], 30, 1, 365)
                self._json_response({"items": _list_reports(self.ctx, limit=limit)})
                return

            if path == "/api/journals":
                self._json_response(_list_journals(self.ctx))
                return

            if path == "/api/settings":
                self._json_response(_settings_view(self.ctx))
                return

            self._json_response({"error": "not found"}, code=404)
        except Exception as e:  # pragma: no cover - 运行期保护
            logger.error("GET 处理失败: %s", e, exc_info=True)
            self._json_response({"error": str(e)}, code=500)

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path

        try:
            # 公开：新浏览器连接已有 Token（校验 body 中的 token 是否匹配服务端）
            if path == "/api/auth/token":
                data = _read_json_body(self) or {}
                candidate = str(data.get("token") or "").strip()
                server_token = self.ctx.api_token
                if not server_token:
                    self._json_response({"ok": True, "required": False, "message": "服务端未启用 Token"})
                    return
                if candidate and candidate == server_token:
                    self._json_response({"ok": True, "required": True, "matched": True})
                    return
                self._json_response({"ok": False, "required": True, "error": "Token 不匹配"}, code=401)
                return

            if path == "/api/run":
                if not self._require_token():
                    return
                data = _read_json_body(self)
                mode = str(data.get("mode", "default"))
                date_str = data.get("date")
                if mode not in {"default", "abstract", "fulltext", "weekly"}:
                    self._json_response({"error": "mode must be one of default/abstract/fulltext/weekly"}, code=400)
                    return

                ok, msg = self.ctx.runner.start_run(trigger="manual", mode=mode, date_str=date_str)
                if not ok:
                    self._json_response({"ok": False, "error": msg, "message": msg}, code=409)
                    return
                self._json_response({"ok": True, "task_id": msg})
                return

            if path == "/api/articles/manual":
                if not self._require_token():
                    return
                data = _read_json_body(self) or {}
                payload, code = _manual_add_article(self.ctx, data)
                self._json_response(payload, code=code)
                return

            if path == "/api/settings":
                if not self._require_token():
                    return
                data = _read_json_body(self)
                payload, code = _save_settings(self.ctx, data)
                self._json_response(payload, code=code)
                return

            if path.startswith("/api/articles/") and path.endswith("/chat"):
                if not self._require_token():
                    return
                article_id_raw = path.removeprefix("/api/articles/").removesuffix("/chat")
                if not article_id_raw.isdigit():
                    self._json_response({"error": "invalid article id"}, code=400)
                    return
                self._handle_chat_stream(int(article_id_raw))
                return

            if path.startswith("/api/articles/") and path.endswith("/star"):
                if not self._require_token():
                    return
                article_id_raw = path.removeprefix("/api/articles/").removesuffix("/star")
                if not article_id_raw.isdigit():
                    self._json_response({"error": "invalid article id"}, code=400)
                    return
                data = _read_json_body(self)
                payload, code = _set_article_star(self.ctx, int(article_id_raw), data)
                self._json_response(payload, code=code)
                return

            if path.startswith("/api/articles/") and path.endswith("/note"):
                if not self._require_token():
                    return
                article_id_raw = path.removeprefix("/api/articles/").removesuffix("/note")
                if not article_id_raw.isdigit():
                    self._json_response({"error": "invalid article id"}, code=400)
                    return
                data = _read_json_body(self)
                payload, code = _set_article_note(self.ctx, int(article_id_raw), data)
                self._json_response(payload, code=code)
                return

            if path.startswith("/api/articles/") and path.endswith("/tags"):
                if not self._require_token():
                    return
                article_id_raw = path.removeprefix("/api/articles/").removesuffix("/tags")
                if not article_id_raw.isdigit():
                    self._json_response({"error": "invalid article id"}, code=400)
                    return
                data = _read_json_body(self)
                payload, code = _set_article_tags(self.ctx, int(article_id_raw), data)
                self._json_response(payload, code=code)
                return

            if path.startswith("/api/articles/") and path.endswith("/status"):
                if not self._require_token():
                    return
                article_id_raw = path.removeprefix("/api/articles/").removesuffix("/status")
                if not article_id_raw.isdigit():
                    self._json_response({"error": "invalid article id"}, code=400)
                    return
                data = _read_json_body(self)
                payload, code = _set_read_status(self.ctx, int(article_id_raw), data)
                self._json_response(payload, code=code)
                return

            if path.startswith("/api/articles/") and path.endswith("/feedback"):
                if not self._require_token():
                    return
                article_id_raw = path.removeprefix("/api/articles/").removesuffix("/feedback")
                if not article_id_raw.isdigit():
                    self._json_response({"error": "invalid article id"}, code=400)
                    return
                data = _read_json_body(self)
                payload, code = _set_feedback(self.ctx, int(article_id_raw), data)
                self._json_response(payload, code=code)
                return

            if path.startswith("/api/articles/") and path.endswith("/zotero"):
                if not self._require_token():
                    return
                aid_raw = path.removeprefix("/api/articles/").removesuffix("/zotero")
                if not aid_raw.isdigit():
                    self._json_response({"error": "invalid article id"}, code=400)
                    return
                payload, code = _zotero_push(self.ctx, int(aid_raw))
                self._json_response(payload, code=code)
                return

            if path == "/api/zotero/batch":
                if not self._require_token():
                    return
                data = _read_json_body(self)
                payload, code = _zotero_batch(self.ctx, data)
                self._json_response(payload, code=code)
                return

            if path == "/api/zotero/test":
                if not self._require_token():
                    return
                data = _read_json_body(self) or {}
                payload, code = _zotero_test(self.ctx, data)
                self._json_response(payload, code=code)
                return

            if path.startswith("/api/articles/") and path.endswith("/watch"):
                if not self._require_token():
                    return
                aid_raw = path.removeprefix("/api/articles/").removesuffix("/watch")
                if not aid_raw.isdigit():
                    self._json_response({"error": "invalid article id"}, code=400)
                    return
                data = _read_json_body(self)
                payload, code = _set_watch(self.ctx, int(aid_raw), data)
                self._json_response(payload, code=code)
                return

            if path == "/api/watch/authors/search":
                if not self._require_token():
                    return
                data = _read_json_body(self)
                payload, code = _author_search(self.ctx, data)
                self._json_response(payload, code=code)
                return

            if path == "/api/watch/authors":
                if not self._require_token():
                    return
                data = _read_json_body(self)
                name = str(data.get("name") or "").strip()
                if not name:
                    self._json_response({"error": "name 不能为空"}, code=400)
                    return
                aid = self.ctx.db.add_watch_author(name, data.get("openalex_id"))
                self._json_response({"ok": aid is not None, "id": aid}, code=200)
                return

            if path == "/api/topics":
                if not self._require_token():
                    return
                data = _read_json_body(self)
                name = str(data.get("name") or "").strip()
                if not name:
                    self._json_response({"error": "name 不能为空"}, code=400)
                    return
                tid = self.ctx.db.create_topic(name, str(data.get("research_question") or ""),
                                               str(data.get("notes") or ""))
                self._json_response({"ok": tid is not None, "id": tid}, code=200)
                return

            if path.startswith("/api/topics/") and path.endswith("/papers"):
                if not self._require_token():
                    return
                tid = path.removeprefix("/api/topics/").removesuffix("/papers")
                if not tid.isdigit():
                    self._json_response({"error": "invalid topic id"}, code=400)
                    return
                data = _read_json_body(self)
                added = self.ctx.db.add_topic_papers(
                    int(tid), [int(i) for i in (data.get("ids") or []) if str(i).isdigit()])
                self._json_response({"ok": True, "added": added}, code=200)
                return

            if path.startswith("/api/topics/"):
                if not self._require_token():
                    return
                tid = path.removeprefix("/api/topics/")
                if not tid.isdigit():
                    self._json_response({"error": "invalid topic id"}, code=400)
                    return
                data = _read_json_body(self)
                name = str(data.get("name") or "").strip()
                if not name:
                    self._json_response({"error": "name 不能为空"}, code=400)
                    return
                ok = self.ctx.db.update_topic(int(tid), name,
                                              str(data.get("research_question") or ""),
                                              str(data.get("notes") or ""))
                self._json_response({"ok": ok}, code=200)
                return

            if path == "/api/digest":
                if not self._require_token():
                    return
                data = _read_json_body(self) or {}
                payload = _digest_view(self.ctx, str(data.get("date") or ""), dry_run=False)
                self._json_response(payload, code=200)
                return

            if path == "/api/compare" or path == "/api/related-work":
                if not self._require_token():
                    return
                data = _read_json_body(self)
                payload, code = _run_tool(self.ctx, data, "compare" if path == "/api/compare" else "related")
                self._json_response(payload, code=code)
                return

            if path == "/api/suggest-topics":
                if not self._require_token():
                    return
                try:
                    analyzer = LLMAnalyzer(self.ctx.config)
                    recent = [a for a in self.ctx.db.list_articles_created_between(
                        "2000-01-01", "2999-01-01")
                        if (a.get("relevance") or 0) >= 7][:20]
                    result = analyzer.suggest_topics(recent, self.ctx.config.get("research_topics", []))
                    self._json_response({"ok": True, **result})
                except Exception as e:  # noqa: BLE001
                    self._json_response({"ok": False, "error": str(e)}, code=500)
                return

            if path == "/api/articles/translate":
                if not self._require_token():
                    return
                data = _read_json_body(self) or {}
                payload, code = _batch_translate_titles(self.ctx, data)
                self._json_response(payload, code=code)
                return

            if path == "/api/articles/cleanup":
                if not self._require_token():
                    return
                data = _read_json_body(self) or {}
                payload, code = _cleanup_low_relevance(self.ctx, data)
                self._json_response(payload, code=code)
                return

            if path == "/api/articles/cleanup/export":
                if not self._require_token():
                    return
                data = _read_json_body(self) or {}
                body, code, ctype = _cleanup_export_csv(self.ctx, data)
                if code == 200:
                    fname = getattr(self.ctx, "_export_filename", None)
                    self._text_response(body, code=code, content_type=ctype, filename=fname)
                else:
                    self._json_response(json.loads(body), code=code)
                return

            if path == "/api/journal-metrics":
                if not self._require_token():
                    return
                data = _read_json_body(self) or {}
                payload, code = _journal_metrics_upsert(self.ctx, data)
                self._json_response(payload, code=code)
                return

            if path == "/api/journal-metrics/reseed":
                if not self._require_token():
                    return
                payload, code = _journal_metrics_reseed(self.ctx)
                self._json_response(payload, code=code)
                return

            if path == "/api/articles/batch-delete":
                if not self._require_token():
                    return
                data = _read_json_body(self) or {}
                payload, code = _batch_delete_articles(self.ctx, data)
                self._json_response(payload, code=code)
                return

            if path == "/api/articles/batch":
                if not self._require_token():
                    return
                data = _read_json_body(self)
                payload, code = _batch_update(self.ctx, data)
                self._json_response(payload, code=code)
                return

            if path.startswith("/api/articles/") and path.endswith("/highlights"):
                if not self._require_token():
                    return
                aid_raw = path.removeprefix("/api/articles/").removesuffix("/highlights")
                if not aid_raw.isdigit():
                    self._json_response({"error": "invalid article id"}, code=400)
                    return
                data = _read_json_body(self)
                text = str(data.get("text") or "").strip()
                if not text:
                    self._json_response({"error": "text 不能为空"}, code=400)
                    return
                hid = self.ctx.db.add_highlight(int(aid_raw), text[:2000], str(data.get("note") or ""))
                self._json_response({"ok": hid is not None, "id": hid}, code=200)
                return

            if path == "/api/articles/citation":
                if not self._require_token():
                    return
                data = _read_json_body(self)
                body, code, ctype = _batch_citation(self.ctx, data)
                self._text_response(body, code=code, content_type=ctype)
                return

            if path == "/api/reports/resend":
                if not self._require_token():
                    return
                data = _read_json_body(self)
                date_str = str(data.get("date") or "").strip()
                if not re.match(r"^\d{4}-\d{2}-\d{2}$", date_str):
                    self._json_response({"error": "date 格式应为 YYYY-MM-DD"}, code=400)
                    return
                try:
                    from main import push_only
                    result = push_only(self.ctx.config, date_str)
                    self._json_response({"ok": True, **result})
                except FileNotFoundError as e:
                    self._json_response({"ok": False, "error": str(e)}, code=404)
                except Exception as e:  # noqa: BLE001 - 推送失败原因需要回传前端
                    logger.error("补发推送失败: %s", e, exc_info=True)
                    self._json_response({"ok": False, "error": f"补发失败: {e}"}, code=500)
                return

            if path == "/api/llm/test":
                if not self._require_token():
                    return
                data = _read_json_body(self)
                payload, code = _test_llm(self.ctx, data)
                self._json_response(payload, code=code)
                return

            if path == "/api/journals":
                if not self._require_token():
                    return
                data = _read_json_body(self)
                payload, code = _add_journal(self.ctx, data)
                self._json_response(payload, code=code)
                return

            if path == "/api/journals/import":
                if not self._require_token():
                    return
                data = _read_json_body(self)
                payload, code = _import_journals(self.ctx, data)
                self._json_response(payload, code=code)
                return

            if path == "/api/journals/test":
                if not self._require_token():
                    return
                data = _read_json_body(self)
                payload, code = _test_journal(self.ctx, data)
                self._json_response(payload, code=code)
                return

            if path.startswith("/api/journals/") and path.endswith("/toggle"):
                if not self._require_token():
                    return
                journal_id_raw = path.removeprefix("/api/journals/").removesuffix("/toggle")
                if not journal_id_raw.isdigit():
                    self._json_response({"error": "invalid journal id"}, code=400)
                    return
                payload, code = _toggle_journal(self.ctx, int(journal_id_raw))
                self._json_response(payload, code=code)
                return

            self._json_response({"error": "not found"}, code=404)
        except json.JSONDecodeError:
            self._json_response({"error": "invalid json"}, code=400)
        except Exception as e:  # pragma: no cover - 运行期保护
            logger.error("POST 处理失败: %s", e, exc_info=True)
            self._json_response({"error": str(e)}, code=500)

    def do_DELETE(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path

        try:
            if path.startswith("/api/articles/") and path.endswith("/chat"):
                if not self._require_token():
                    return
                article_id_raw = path.removeprefix("/api/articles/").removesuffix("/chat")
                if not article_id_raw.isdigit():
                    self._json_response({"error": "invalid article id"}, code=400)
                    return
                self.ctx.db.clear_chat_messages(int(article_id_raw))
                self._json_response({"ok": True, "id": int(article_id_raw)})
                return

            if path.startswith("/api/topics/") and path.endswith("/papers"):
                if not self._require_token():
                    return
                rest = path.removeprefix("/api/topics/").removesuffix("/papers")
                parts = rest.split("/")
                if len(parts) != 2 or not all(x.isdigit() for x in parts):
                    self._json_response({"error": "invalid path"}, code=400)
                    return
                ok = self.ctx.db.remove_topic_paper(int(parts[0]), int(parts[1]))
                self._json_response({"ok": ok}, code=200)
                return

            if path.startswith("/api/topics/"):
                if not self._require_token():
                    return
                tid = path.removeprefix("/api/topics/")
                if not tid.isdigit():
                    self._json_response({"error": "invalid topic id"}, code=400)
                    return
                self._json_response({"ok": self.ctx.db.delete_topic(int(tid))}, code=200)
                return

            if path.startswith("/api/watch/authors/"):
                if not self._require_token():
                    return
                aid = path.removeprefix("/api/watch/authors/")
                if not aid.isdigit():
                    self._json_response({"error": "invalid author id"}, code=400)
                    return
                self._json_response({"ok": self.ctx.db.delete_watch_author(int(aid))}, code=200)
                return

            if path.startswith("/api/highlights/"):
                if not self._require_token():
                    return
                hid = path.removeprefix("/api/highlights/")
                if not hid.isdigit():
                    self._json_response({"error": "invalid highlight id"}, code=400)
                    return
                self._json_response({"ok": self.ctx.db.delete_highlight(int(hid))}, code=200)
                return

            if path.startswith("/api/journals/"):
                if not self._require_token():
                    return
                journal_id_raw = path.removeprefix("/api/journals/")
                if not journal_id_raw.isdigit():
                    self._json_response({"error": "invalid journal id"}, code=400)
                    return
                payload, code = _delete_journal(self.ctx, int(journal_id_raw))
                self._json_response(payload, code=code)
                return

            self._json_response({"error": "not found"}, code=404)
        except Exception as e:  # pragma: no cover - 运行期保护
            logger.error("DELETE 处理失败: %s", e, exc_info=True)
            self._json_response({"error": str(e)}, code=500)

    def log_message(self, fmt: str, *args: Any) -> None:
        logger.info("%s - %s", self.address_string(), fmt % args)


def run_web_server(config_path: str = "config/config.yaml", host: str = "0.0.0.0", port: int = 8080) -> None:
    setup_logging()
    ctx = WebContext(config_path=config_path)

    server = ThreadingHTTPServer((host, port), Handler)
    server.context = ctx  # type: ignore[attr-defined]

    web_cfg = ctx.config.get("web", {})
    if ctx.enable_scheduler:
        ctx.runner.start_scheduler()

    logger.info("Web 控制台已启动: http://%s:%s", host, port)
    logger.info("数据库: %s", ctx.db_path)
    logger.info("输出目录: %s", ctx.output_dir)
    logger.info("调度线程: %s", "启用" if ctx.enable_scheduler else "关闭")
    if ctx.api_token:
        logger.info("POST /api/run 已启用 Token 校验（X-API-Token）")
    if web_cfg.get("readonly", False):
        logger.info("readonly=true: 请在反向代理层禁用 POST /api/run")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("收到中断信号，正在退出...")
    finally:
        server.server_close()


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Daily Paper Digest 网页控制台")
    parser.add_argument("--config", default="config/config.yaml", help="配置文件路径")
    parser.add_argument("--host", default="0.0.0.0", help="监听地址")
    parser.add_argument("--port", type=int, default=8080, help="监听端口")
    args = parser.parse_args()

    run_web_server(config_path=args.config, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
