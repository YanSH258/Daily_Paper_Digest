"""
main.py - 主入口
分阶段流水线：
  1. RSS 抓取 → 批次内去重 → 数据库去重 → 立即入库基础记录
  2. LLM 相关性评分（含失败重试）→ 全文获取 → 深度解读
  3. 各阶段状态实时落库，支持中断续跑与失败补齐

用法：
  python src/main.py                          # 立即运行一次
  python src/main.py --schedule               # 按 config.yaml 中的时间每日定时运行
  python src/main.py --date 2024-01-15        # 指定报告日期
  python src/main.py --config config/config.yaml  # 指定配置文件路径
  python src/main.py --push-only 2024-01-15   # 只补发指定日期的日报推送
"""
import os
import sys
import copy
import json
import uuid
import time
import threading
import hashlib
import logging
import argparse
import concurrent.futures
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from pathlib import Path
from typing import Any, Optional

sys.path.insert(0, str(Path(__file__).parent))

import yaml

from core.db       import Database
from core.fetcher  import JournalFetcher, RSS_ONLY_PUBLISHERS
from core.analyzer import LLMAnalyzer, PROMPT_VERSION
from core.notifier import Notifier, classify_article
from core.tracking import (collect_tracking_articles, mark_tracking_cursor,
                           record_tracking_edges)
from digest.config import validate_digest_config
from digest.service import DigestError, DigestService
from fetchers.models import FetchResult
from utils.paths   import init_config, resolve_against_root, resolve_config_file, resolve_config_paths

# ── 日志配置 ──────────────────────────────────────────────────
LOG_DIR = resolve_against_root("data/logs")

_logger_ready = False


def setup_logging(config_path: Optional[str] = None) -> None:
    """初始化日志（幂等）。在 CLI 与 Web 配置路径确定后调用。"""
    global _logger_ready, LOG_DIR
    if _logger_ready:
        return
    if config_path:
        resolve_config_file(config_path)
    LOG_DIR = resolve_against_root("data/logs")
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(
                LOG_DIR / f"{datetime.now().strftime('%Y-%m-%d')}.log",
                encoding="utf-8"
            ),
        ],
    )
    _logger_ready = True


logger = logging.getLogger("main")
DEFAULT_SCHEDULER_TIMEZONE = "Asia/Shanghai"


def validate_scheduler_time(value: Any) -> str:
    """Validate and normalize a scheduler clock value."""
    if not isinstance(value, str):
        raise ValueError("scheduler.run_time 必须是 HH:MM 字符串")
    try:
        hour_str, minute_str = value.strip().split(":", 1)
        hour, minute = int(hour_str), int(minute_str)
    except (AttributeError, ValueError):
        raise ValueError("scheduler.run_time 必须是 HH:MM") from None
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError("scheduler.run_time 必须在 00:00 到 23:59 之间")
    return f"{hour:02d}:{minute:02d}"


def get_scheduler_timezone(config: dict) -> ZoneInfo:
    name = str((config.get("scheduler") or {}).get("timezone") or DEFAULT_SCHEDULER_TIMEZONE).strip()
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"scheduler.timezone 无效: {name!r}") from exc


# ── 核心流程 ──────────────────────────────────────────────────

def _apply_env_overrides(cfg: dict) -> dict:
    """环境变量覆盖（适配 GitHub Actions Secrets），优先级高于配置文件。"""
    # LLM API Key: 优先读取环境变量 DEEPSEEK_API_KEY / QWEN_API_KEY
    provider = cfg.get("llm", {}).get("provider", "deepseek")
    env_key_map = {"deepseek": "DEEPSEEK_API_KEY", "qwen": "QWEN_API_KEY"}
    env_key_name = env_key_map.get(provider, f"{provider.upper()}_API_KEY")
    env_api_key = os.environ.get(env_key_name)
    if env_api_key:
        cfg.setdefault("llm", {}).setdefault(provider, {})["api_key"] = env_api_key
        logger.info("已从环境变量 %s 读取 API Key", env_key_name)

    # Email 密码：优先读取环境变量 EMAIL_PASSWORD（配置位于 output.email 下）
    env_email_pwd = os.environ.get("EMAIL_PASSWORD")
    if env_email_pwd:
        cfg.setdefault("output", {}).setdefault("email", {})["password"] = env_email_pwd

    # OpenAlex API Key：优先读取环境变量 OPENALEX_API_KEY（配置位于 openalex 下）
    env_openalex_key = os.environ.get("OPENALEX_API_KEY")
    if env_openalex_key:
        cfg.setdefault("openalex", {})["api_key"] = env_openalex_key
        logger.info("已从环境变量 OPENALEX_API_KEY 读取 OpenAlex API Key")

    # Feishu Webhook：优先读取环境变量 FEISHU_WEBHOOK_URL（配置位于 output.feishu 下）
    env_feishu = os.environ.get("FEISHU_WEBHOOK_URL")
    if env_feishu:
        cfg.setdefault("output", {}).setdefault("feishu", {})["webhook_url"] = env_feishu

    # Zotero / Web of Science 凭据
    env_zotero = os.environ.get("ZOTERO_API_KEY")
    if env_zotero:
        cfg.setdefault("zotero", {})["api_key"] = env_zotero
    env_zuid = os.environ.get("ZOTERO_USER_ID")
    if env_zuid:
        cfg.setdefault("zotero", {})["user_id"] = env_zuid
    env_wos = os.environ.get("WOS_API_KEY")
    if env_wos:
        cfg.setdefault("wos", {})["api_key"] = env_wos

    return cfg


def load_config(path: str = "config/config.yaml") -> dict:
    config_path = resolve_config_file(path)
    with config_path.open("r", encoding="utf-8") as f:
        cfg: dict = yaml.safe_load(f)
    cfg = resolve_config_paths(_apply_env_overrides(cfg))
    db_path = cfg.get("database", {}).get("path")
    if db_path and db_path != ":memory:":
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    return cfg


def load_config_from_obj(cfg: dict) -> dict:
    """基于内存中的配置对象（如 ruamel CommentedMap）生成应用环境变量后的副本，
    供设置保存前做完整校验，不落盘。"""
    return resolve_config_paths(_apply_env_overrides(copy.deepcopy(cfg)))


def validate_config(config: dict) -> None:
    """启动前校验必填配置项，缺失时 raise ValueError。"""
    required_paths = [
        ("database", "path"),
        ("llm", "provider"),
    ]
    for keys in required_paths:
        node = config
        for k in keys:
            if not isinstance(node, dict) or k not in node:
                raise ValueError(
                    f"配置缺失必填项: {' -> '.join(keys)}，请检查 config.yaml"
                )
            node = node[k]

    # 校验 LLM provider 配置
    provider = config["llm"]["provider"]
    provider_cfg = config.get("llm", {}).get(provider, {})
    if not provider_cfg.get("api_key"):
        raise ValueError(
            f"LLM provider '{provider}' 缺少 api_key，"
            f"请在 config.yaml 或环境变量 {provider.upper()}_API_KEY 中配置"
        )

    # digest 配置校验（0 是合法值，如关闭防重；不合法在启动/保存时即拒绝）
    digest_errors = validate_digest_config(config)
    if digest_errors:
        raise ValueError("digest 配置无效: " + "; ".join(digest_errors))

    scheduler = config.get("scheduler") or {}
    validate_scheduler_time(scheduler.get("run_time", "08:00"))
    get_scheduler_timezone(config)
    from processing import validate_budget
    validate_budget(config)
    validate_budget(config, trial=True)


def _write_dry_run_report(config: dict, date_str: str, report: str) -> Optional[str]:
    """落盘 dry-run 审阅文件，返回路径；失败返回 None（不影响预览本身）。

    语义（docs/DIGEST_RELEASE_SPEC.md §5）：这是 dry-run 的**唯一**落盘产物，
    属审阅快照而非正式报告——同日重跑直接覆盖；不写 digest 版本/条目、
    不计入 30 日防重、不触发任何推送。正式报告仅由发布动作产生。
    """
    try:
        out = resolve_against_root(config.get("output", {}).get("output_dir") or "data/output")
        out.mkdir(parents=True, exist_ok=True)
        path = out / f"digest-dry-run-{date_str}.txt"
        path.write_text(report, encoding="utf-8")
        return str(path)
    except Exception as e:  # noqa: BLE001 - 审阅文件写失败不应中断预览
        logger.warning("写入 dry-run 报告失败: %s", e)
        return None


def load_journals_config(config: dict, db: Database) -> list[dict[str, Any]]:
    """订阅源唯一来源是数据库：
    1. 旧版 config.yaml 若还有 journals，自动迁移入库（一次性，之后 config 里不再保留）；
    2. 返回库中所有启用中的订阅，供抓取使用。
    """
    legacy = config.get("journals") or []
    if legacy:
        added = db.migrate_config_journals(legacy)
        if added:
            logger.info(f"已将 config.yaml 中 {added} 个订阅源迁移至数据库")
        config.pop("journals", None)

    rows = db.list_journals(enabled_only=True)
    journals = [
        {
            "id": j.get("id"),
            "name": j.get("name") or "未命名订阅",
            "rss": j["rss"],
            "publisher": j.get("publisher") or "DEFAULT",
            "max_articles": int(j.get("max_articles") or 100),
            "source_type": j.get("source_type") or "rss",
            "query": j.get("query") or "",
            "last_run": j.get("last_run") or "",
        }
        for j in rows
    ]
    logger.info(f"订阅源共 {len(journals)} 个（数据库，启用状态）")
    return journals


def _sha256(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


class RunBusyError(RuntimeError):
    """已有流水线任务在运行"""


class RunLock:
    """基于文件锁的跨进程互斥：统一约束 CLI、网页触发与定时任务。

    进程崩溃时由操作系统自动释放，不产生死锁残留。
    """

    def __init__(self, config: dict) -> None:
        try:
            import fcntl
        except ImportError:  # Windows 使用 msvcrt 文件锁
            fcntl = None  # noqa: F841
        self._fcntl_available = fcntl is not None
        db_path = resolve_against_root(config.get("database", {}).get("path", "data/db/chem_daily.db"))
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock_path = db_path.parent / ".pipeline.lock"
        self._handle: Optional[Any] = None

    def acquire(self) -> None:
        if not self._fcntl_available:
            import msvcrt
            self._handle = open(self._lock_path, "a+b")
            try:
                self._handle.seek(0, 2)
                if self._handle.tell() == 0:
                    self._handle.write(b"0")
                    self._handle.flush()
                self._handle.seek(0)
                msvcrt.locking(self._handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                self._handle.close()
                self._handle = None
                raise RunBusyError("已有流水线任务正在运行") from exc
            return
        import fcntl
        self._handle = open(self._lock_path, "w")
        try:
            fcntl.flock(self._handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, OSError) as e:
            self._handle.close()
            self._handle = None
            raise RunBusyError("已有流水线任务正在运行（数据库文件锁被占用）") from e

    def release(self) -> None:
        if self._handle is None:
            return
        if not self._fcntl_available:
            import msvcrt
            try:
                self._handle.seek(0)
                msvcrt.locking(self._handle.fileno(), msvcrt.LK_UNLCK, 1)
            finally:
                self._handle.close()
                self._handle = None
            return
        import fcntl
        try:
            fcntl.flock(self._handle, fcntl.LOCK_UN)
        finally:
            self._handle.close()
            self._handle = None


def dedupe_batch(articles: list[dict]) -> list[dict]:
    """批次内去重：同一 DOI/URL/标题只保留第一条。

    同一文章常被多个订阅源同时收录，先在批次内去重再评分，
    避免对同一篇文章重复调用 LLM 计费。
    """
    seen: set[str] = set()
    unique: list[dict] = []
    for a in articles:
        doi = (a.get("doi") or "").strip()
        url = (a.get("url") or "").strip()
        title = (a.get("title") or "").strip()
        if doi:
            key = f"doi:{doi.lower()}"
        elif url:
            key = f"url:{url}"
        elif title:
            key = "title:" + " ".join(title.lower().split())
        else:
            continue
        if key in seen:
            continue
        seen.add(key)
        unique.append(a)
    return unique


def _preview_cache_path(config: dict, date_str: str) -> Path:
    """Cache location for preview reuse; overridable to keep tests off real data."""
    configured = (config.get("fetcher") or {}).get("preview_cache_dir")
    if configured:
        cache_dir = Path(configured).expanduser()
    else:
        from utils.paths import resolve_against_root
        cache_dir = resolve_against_root("data/cache")
    return cache_dir / f"preview-{date_str}.json"


def preview_collection(config: dict, date_str: str, *, refresh: bool = False) -> dict:
    """采集预检只读文章库，不构造 Database 或模型，也不推进游标。

    同一天的成功结果会缓存复用，避免重复预览时反复请求外部来源；
    refresh=True 强制重新采集。
    """
    import sqlite3
    from processing import admission_decision, build_queue, validate_budget
    cache_path = _preview_cache_path(config, date_str)
    if not refresh:
        try:
            cached = json.loads(cache_path.read_text(encoding="utf-8"))
            if cached.get("date") == date_str and isinstance(cached.get("result"), dict):
                return {**cached["result"], "cached": True,
                        "cached_at": cached.get("created_at")}
        except (OSError, ValueError):
            pass
    config = copy.deepcopy(config)
    config["_run_date"] = date_str
    path = Path(config["database"]["path"]).resolve()
    with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as conn:
        conn.row_factory = sqlite3.Row
        journals = [dict(r) for r in conn.execute("SELECT * FROM journals WHERE enabled=1 ORDER BY id")]
        existing = [dict(r) for r in conn.execute("SELECT * FROM articles")]
    config["journals"] = journals
    fetcher = JournalFetcher(config)
    try:
        raw = fetcher.fetch_all()
        sources = getattr(fetcher, "source_results", [])
    finally:
        fetcher.close()
    result = {"preview": True, "date": date_str, "fetched_raw": len(raw),
              "outside_window": 0, "needs_date": 0, "invalid_date": 0,
              "new_eligible": 0, "new_quarantined": 0, "already_in_db": 0,
              "score_attempted": 0, "llm_requests": 0, "sources": sources,
              "report_skipped_reason": "采集预览不调用模型、不入库、不生成正式日报或推送"}
    unique = dedupe_batch(raw)
    result["cross_source_duplicates"] = len(raw) - len(unique)
    known = dedupe_batch(existing)
    dois = {a.get("doi") for a in known if a.get("doi")}
    urls = {a.get("url") for a in known if a.get("url")}
    titles = {" ".join(str(a.get("title") or "").lower().split()) for a in known}
    pending = []
    for a in existing:
        if a.get("score_status") == "ok" or a.get("processed"):
            continue
        decision = admission_decision(a, date_str, config)
        if a.get("processing_status") == "eligible" or (a.get("processing_status") in (None, "", "unreviewed") and decision["decision"] == "eligible"):
            pending.append({**a, "processing_status": "eligible"})
    for index, a in enumerate(unique, 1):
        decision = admission_decision(a, date_str, config)
        if decision["decision"] != "eligible":
            result[decision["decision"]] += 1
        title = " ".join(str(a.get("title") or "").lower().split())
        if (a.get("doi") and a["doi"] in dois) or (a.get("url") and a["url"] in urls) or (title and title in titles):
            result["already_in_db"] += 1
            continue
        if decision["decision"] == "eligible":
            result["new_eligible"] += 1
            pending.append({**a, "id": -index, "processing_status": "eligible"})
        else:
            result["new_quarantined"] += 1
    selected = build_queue(pending, config, validate_budget(config))
    result.update(score_queue_total=len(pending), score_planned=len(selected),
                  score_deferred=len(pending) - len(selected))
    # Truncation is normal (we keep the newest N), so only real failures block
    # reuse: a failed source must be retried on the next preview.
    failed = sum(1 for s in sources if not s.get("success"))
    incomplete = sum(1 for s in sources if s.get("success") and not s.get("complete"))
    result["source_incomplete"] = incomplete
    if not failed:
        try:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(json.dumps(
                {"date": date_str, "created_at": datetime.now().isoformat(timespec="seconds"),
                 "result": result}, ensure_ascii=False), encoding="utf-8")
        except OSError as exc:
            logger.debug("写入预览缓存失败: %s", exc)
    return result


def run_once(config: dict, date_str: Optional[str] = None, task_id: Optional[str] = None,
             *, trial: bool = False, preview: bool = False, refresh: bool = False) -> dict:
    """执行一次完整流水线，返回结构化运行结果。

    分阶段状态实时落库（score_status / evidence_level / analysis_status），
    评分或分析失败的文章保留在库中，下次运行自动补齐失败阶段。
    外部调用方（如 TaskRunner）传入 task_id 时由其负责收尾任务记录；
    否则 run_once 自己记录 start/finish。
    """
    config = copy.deepcopy(config)
    date_str = date_str or datetime.now(get_scheduler_timezone(config)).strftime("%Y-%m-%d")
    config["_run_date"] = date_str
    if preview:
        return preview_collection(config, date_str, refresh=refresh)
    threshold = config.get("relevance_threshold", 5)
    retry_window_days = int(config.get("fetcher", {}).get("retry_window_days", 7))
    processing_cfg = config.get("processing", {}) or {}
    from processing import admission_decision, validate_budget
    validate_budget(config, trial=trial)
    score_budget = int(processing_cfg.get("trial_max_score_articles", 30) if trial else
                       processing_cfg.get("max_score_articles_per_run", 100))

    stats: dict[str, Any] = {
        "date": date_str,
        "fetched": 0, "batch_duplicates": 0, "db_duplicates": 0,
        "new_articles": 0, "retried_score": 0, "retried_analysis": 0,
        "scored_ok": 0, "scored_failed": 0,
        "candidates": 0, "fulltext_fetched": 0, "abstract_completed": 0,
        "analyzed_ok": 0, "analyzed_failed": 0, "analyzed_skipped": 0,
        "saved": 0, "db_errors": 0,
        "fetched_raw": 0, "outside_window": 0, "needs_date": 0,
        "invalid_date": 0, "new_eligible": 0, "new_quarantined": 0,
        "score_queue_total": 0, "score_attempted": 0, "score_deferred": 0,
        "report_skipped_reason": None, "report_path": None,
        "push_results": None,
    }

    own_task = False
    if not task_id:
        task_id = str(uuid.uuid4())
        own_task = True

    logger.info(f"========== 开始运行: {date_str} (task_id={task_id}) ==========")

    _run_lock = RunLock(config)
    _run_lock.acquire()
    db = fetcher = None
    try:
        db       = Database(config["database"]["path"])
        config["journals"] = load_journals_config(config, db)
        fetcher  = JournalFetcher(config)
        analyzer = LLMAnalyzer(config)
        notifier = Notifier(config)

        # 推荐质量闭环：注入用户历史偏好样例
        fb = db.get_feedback_examples()
        analyzer.set_feedback_examples(fb.get("liked", []), fb.get("disliked", []))
        if fb.get("liked") or fb.get("disliked"):
            logger.info(
                f"  偏好注入: 喜欢 {len(fb['liked'])} 条 / 不喜欢 {len(fb['disliked'])} 条"
            )

    except Exception:
        if db is not None:
            db.close()
        _run_lock.release()
        raise

    if own_task:
        db.task_start(task_id, trigger="cli", mode="default", date_str=date_str)

    def progress(stage: str) -> None:
        if not own_task:
            db.task_finish(task_id, status="running", stats=stats, stage=stage)

    try:
        # ── Step 1: 抓取订阅源（与追踪并行，受总预算约束）──────
        logger.info("Step 1: 抓取期刊 RSS")
        budget_seconds = int((config.get("fetcher") or {}).get("collection_budget_seconds", 300) or 0)
        deadline = time.monotonic() + budget_seconds if budget_seconds > 0 else None
        stats["collection_budget_seconds"] = budget_seconds
        source_health: dict[Any, Any] = {}
        tracking_box: dict[str, Any] = {}
        tracking_thread: Optional[threading.Thread] = None

        def _collect_tracking() -> None:
            try:
                tracking_box["articles"], tracking_box["meta"] = collect_tracking_articles(config, db)
            except Exception as exc:  # noqa: BLE001 - 追踪失败不阻断日常流水线
                tracking_box["error"] = str(exc)

        # Tracking hits the same throttled APIs, so run it alongside collection
        # instead of serializing it after; a stuck tracker must not block Step 1.
        if config.get("tracking", {}).get("enabled", True):
            tracking_thread = threading.Thread(target=_collect_tracking, daemon=True)
            tracking_thread.start()

        raw_articles = fetcher.fetch_all(
            health_callback=lambda jid, ok, err="": source_health.update({jid: (ok, err)}),
            deadline=deadline,
        )
        stats["fetched_rss"] = len(raw_articles)
        stats["fetched_raw"] = len(raw_articles)
        logger.info(f"  来源共抓取: {len(raw_articles)} 篇原始文章")

        # ── Step 1.2: 引文/作者追踪采集 ───────────────────────
        tracking_articles, tracking_meta = [], {}
        if tracking_thread is not None:
            remaining = None if deadline is None else max(1.0, deadline - time.monotonic())
            tracking_thread.join(timeout=remaining)
            if tracking_thread.is_alive():
                # No cursors advance: the window stays queued for the next run.
                logger.warning("追踪采集超出 Step 1 预算，本次跳过（游标不推进，下次重试）")
                stats["tracking_skipped"] = "collection budget exceeded"
            elif tracking_box.get("error"):
                logger.error(f"追踪采集失败: {tracking_box['error']}")
            else:
                tracking_articles = tracking_box.get("articles") or []
                tracking_meta = tracking_box.get("meta") or {}
                if tracking_articles:
                    logger.info(f"  追踪采集: 引文/作者新文章 {len(tracking_articles)} 篇")
        raw_articles = list(raw_articles) + list(tracking_articles)
        stats["fetched"] = len(raw_articles)
        stats["fetched_raw"] = sum(r.get("raw_count", 0) for r in getattr(fetcher, "source_results", [])) + len(tracking_articles) if getattr(fetcher, "source_results", []) else len(raw_articles)
        for article in raw_articles:
            decision = admission_decision(article, date_str, config)
            article["_admission"] = decision
            article["processing_status"] = decision["decision"]
            article["processing_reason"] = decision["reason"]
            article["date_source"] = decision["date_source"]
            if decision.get("publication_date"):
                article["pub_date"] = decision["publication_date"]
            if decision["decision"] != "eligible":
                stats[decision["decision"]] += 1

        # ── Step 1.5: 批次内去重 ──────────────────────────────
        all_discoveries = list(raw_articles)
        raw_articles = dedupe_batch(raw_articles)
        stats["batch_duplicates"] = stats["fetched"] - len(raw_articles)
        logger.info(
            f"  批次内去重后: {len(raw_articles)} 篇（合并多源重复 {stats['batch_duplicates']} 篇）"
        )

        # ── Step 2: 数据库去重 ────────────────────────────────
        logger.info("Step 2: 数据库去重")
        new_articles = []
        for a in raw_articles:
            is_dup, reason = db.check_duplicate(a)
            if is_dup:
                logger.debug(f"  跳过重复: {reason}")
                stats["db_duplicates"] += 1
            else:
                new_articles.append(a)
        stats["new_articles"] = len(new_articles)
        logger.info(
            f"  去重后: {len(new_articles)} 篇新文章（跳过 {stats['db_duplicates']} 篇重复）"
        )

        # ── Step 2.5: 立即入库基础记录（processed=0）───────────
        for a in new_articles:
            if not a.get("topic"):
                a["topic"] = classify_article(a)
        inserted_ids = db.save_articles_batch(new_articles)
        stats["saved"] = sum(1 for i in inserted_ids if i is not None)
        if stats["saved"] < len(new_articles):
            stats["db_errors"] += 1
            logger.error(f"  有 {len(new_articles) - stats['saved']} 篇文章入库失败")
        inserted_pairs = []
        for a, aid in zip(new_articles, inserted_ids):
            a["id"] = aid
            if aid is not None:
                inserted_pairs.append((aid, a))
        if db.persist_admission([(aid, a["_admission"]) for aid, a in inserted_pairs]) != len(inserted_pairs):
            raise RuntimeError("准入状态持久化失败，停止处理且不推进游标")
        new_articles = [a for a in new_articles if a.get("id") is not None]
        # 追踪来源与引文关联落库
        for a in new_articles:
            via = a.get("discovered_via")
            if via and via != "rss":
                db.update_article_fields(a["id"], discovered_via=via)
        discovery_pairs = []
        for discovery in all_discoveries:
            aid = db.record_article_sources(discovery)
            if aid is not None:
                discovery_pairs.append((aid, discovery))
        if tracking_meta:
            edges = record_tracking_edges(db, discovery_pairs, tracking_meta)
            if edges:
                stats["citation_edges"] = edges
            # 追踪文章已全部尝试入库：此时才推进引文/作者游标。
            # 入库失败（save_articles_batch 部分失败）时不推进对应窗口，下轮可重试。
            if stats["saved"] == stats.get("new_articles", 0):
                mark_tracking_cursor(db, tracking_meta)
            else:
                logger.warning("  有文章入库失败，本次不推进追踪游标（失败窗口将重试）")
        logger.info(f"  已入库基础记录 {stats['saved']} 篇（待评分）")
        progress("saved_base")

        source_results = getattr(fetcher, "source_results", [])
        stats["sources"] = source_results
        stats["source_failed"] = sum(not r["success"] for r in source_results)
        stats["source_incomplete"] = sum(r["success"] and not r["complete"] for r in source_results)
        persisted = stats["saved"] == stats["new_articles"] and not stats["db_errors"]
        for result in source_results:
            db.record_source_result(result, persisted=persisted)
        stats["new_eligible"] = sum(a.get("processing_status") == "eligible" for a in new_articles)
        stats["new_quarantined"] = len(new_articles) - stats["new_eligible"]
        stats["cross_source_duplicates"] = stats["batch_duplicates"]
        stats["already_in_db"] = stats["db_duplicates"]
        queue = db.get_processing_queue(config, date_str, trial=trial)
        to_score = queue.pop("selected")
        stats.update(queue)
        stats["score_attempted"] = 0
        new_id_set = {a["id"] for a in new_articles}
        score_retry = [a for a in to_score if a.get("score_status") == "failed"]
        stats["retried_score"] = len(score_retry)
        analysis_queue = db.get_analysis_queue(config, date_str, trial=trial)
        analysis_retry = analysis_queue["selected"]
        stats["analysis_deferred"] = analysis_queue["analysis_deferred"]
        analysis_retry = [a for a in analysis_retry if a["id"] not in {r["id"] for r in to_score}]
        stats["retried_analysis"] = len(analysis_retry)
        eligible_new_articles = to_score

        # ── Step 2.7: 缺摘要/摘要被 RSS 截断时先补全，再评分 ──
        if eligible_new_articles:
            from fetchers.oa_fetcher import get_dedup_abstract, looks_truncated_abstract
            unpaywall_email = config.get("unpaywall_email", "your@email.com")
            for a in eligible_new_articles:
                if not a.get("doi"):
                    continue
                abs_txt = (a.get("abstract") or "").strip()
                if abs_txt and not looks_truncated_abstract(abs_txt):
                    continue
                try:
                    completed = get_dedup_abstract(a["doi"], email=unpaywall_email)
                except Exception as e:
                    logger.debug(f"  摘要补全查询失败 ({a['doi']}): {e}")
                    completed = None
                if completed and len(completed) > len(abs_txt):
                    a["abstract"] = completed
                    db.update_article_fields(a["id"], abstract=completed)
                    stats["abstract_completed"] += 1
                    logger.info(
                        "  评分前摘要补全: %s... (%s → %s 字)",
                        a["title"][:50], len(abs_txt), len(completed),
                    )
        progress("abstract_backfill")

        # ── Step 2.75: 标题自动翻译（免费接口，粗筛阅读用）────
        if eligible_new_articles:
            from utils.translate import looks_chinese, translate_title
            t_email = config.get("unpaywall_email", "")
            translated = 0
            for a in eligible_new_articles[:80]:  # 单次任务上限，防止接口超时拖长任务
                title = (a.get("title") or "").strip()
                if not title or looks_chinese(title) or a.get("title_zh"):
                    continue
                zh = translate_title(title, email=t_email, pause=0.3)
                if zh:
                    db.update_article_fields(a["id"], title_zh=zh)
                    a["title_zh"] = zh
                    translated += 1
            if translated:
                stats["titles_translated"] = translated
                logger.info(f"  标题自动翻译: {translated} 篇")
        progress("title_translation")

        # ── Step 2.8: WOS 元数据增强（可选，需 WOS_API_KEY）────
        if eligible_new_articles and (os.environ.get("WOS_API_KEY") or config.get("wos", {}).get("api_key")):
            try:
                from integrations.wos_client import WOSClient
                wos = WOSClient(config)
                enriched = 0
                for a in eligible_new_articles[:30]:  # 限额，防止单次任务消耗过多配额
                    if not (a.get("doi") or a.get("title")):
                        continue
                    try:
                        updates = wos.enrich_article(a)
                    except Exception as e:  # noqa: BLE001
                        logger.debug(f"WOS 增强失败 ({a.get('doi')}): {e}")
                        continue
                    if updates:
                        db.update_article_fields(a["id"], **updates)
                        a.update(updates)
                        enriched += 1
                if enriched:
                    logger.info(f"  WOS 元数据增强: {enriched} 篇")
                    stats["wos_enriched"] = enriched
            except ValueError as e:
                logger.debug(f"WOS 增强跳过: {e}")
        progress("wos_enrich")

        # ── Step 2.9: 相似度先验（推荐质量闭环）───────────────
        try:
            from utils.similarity import build_profile, compute_prior
            profile = build_profile(db.get_positive_profile_texts())
            if profile:
                for a in new_articles:
                    prior = compute_prior(profile, a.get("title", "") + " " + (a.get("abstract") or ""))
                    if prior is not None:
                        a["sim_prior"] = prior
                logger.info("  相似度先验: 已为候选计算（正样本画像就绪）")
        except Exception as e:  # noqa: BLE001 - 先验失败不影响主流程
            logger.debug(f"相似度先验跳过: {e}")

        # ── Step 3: LLM 相关性评分（新文章 + 评分失败重试）────
        logger.info(f"Step 3: LLM 相关性评分 (阈值={threshold}，本轮 {len(to_score)}/{stats['score_queue_total']} 篇)")
        score_results: list[Any] = [None] * len(to_score)

        score_counter_lock = __import__("threading").Lock()

        def _score_one(idx_article):
            idx, article = idx_article
            logger.info(f"  [{idx+1}/{len(to_score)}] 评分: {article['title'][:60]}...")
            with score_counter_lock:
                stats["score_attempted"] += 1
            try:
                score_results[idx] = analyzer.filter_relevance(article)
            except Exception as e:  # noqa: BLE001 - 单篇评分失败不阻断批次
                logger.error(f"  评分失败 (idx={idx}): {e}")
                score_results[idx] = e

        llm_concurrency = config.get("performance", {}).get("llm_concurrency", 3)
        with concurrent.futures.ThreadPoolExecutor(max_workers=llm_concurrency) as executor:
            list(executor.map(_score_one, enumerate(to_score)))

        candidate_articles: list[dict] = []
        for article, res in zip(to_score, score_results):
            if isinstance(res, dict):
                ok = db.update_article_fields(
                    article["id"],
                    relevance=res["score"], relevance_reason=res["reason"],
                    score_status="ok", score_model=res["model"], score_basis=res["basis"],
                    sim_prior=article.get("sim_prior"),
                )
                article.update({
                    "relevance": res["score"], "relevance_reason": res["reason"],
                    "score_status": "ok",
                })
                stats["scored_ok"] += 1
                if not ok:
                    stats["db_errors"] += 1
                if ok and res["score"] >= threshold:
                    candidate_articles.append(article)
                    logger.info(f"    ✓ 入选: {article['title'][:60]} (score={res['score']:.1f})")
                else:
                    # 评分完成且低于阈值：该文章处理结束
                    db.update_article_fields(article["id"], processed=1)
                    logger.info(f"    ✗ 过滤: {article['title'][:60]} (score={res['score']:.1f})")
            else:
                err = res if isinstance(res, Exception) else RuntimeError("未知评分错误")
                ok = db.update_article_fields(
                    article["id"], score_status="failed", score_error=str(err)[:500],
                )
                article["score_status"] = "failed"
                stats["scored_failed"] += 1
                if not ok:
                    stats["db_errors"] += 1

        # 分析失败重试的文章直接进入候选（评分已通过）
        candidate_articles = list({a["id"]: a for a in analysis_retry + candidate_articles}.values())
        stats["analysis_deferred"] += max(0, len(candidate_articles) - score_budget)
        candidate_articles = candidate_articles[:score_budget]
        stats["candidates"] = len(candidate_articles)
        logger.info(f"  初筛通过: {len(candidate_articles)} 篇")
        progress("scored")

        # ── Step 4: 对入选且尚无全文的文章获取全文 ────────────
        logger.info("Step 4: 获取相关文章全文（摘要保持原文，全文独立存储）")
        use_fulltext = config.get("fetcher", {}).get("use_fulltext", True)

        if use_fulltext and candidate_articles:
            articles_to_fetch = [
                a for a in candidate_articles
                if a.get("evidence_level") != "FULLTEXT"
                and a.get("publisher", "DEFAULT") not in RSS_ONLY_PUBLISHERS
            ]
            if articles_to_fetch:
                logger.info(f"  并发全文获取（共 {len(articles_to_fetch)} 篇）")
                fetch_results = fetcher.fetch_fulltext_batch(articles_to_fetch)
                for article, fr in zip(articles_to_fetch, fetch_results):
                    if fr is None:
                        fr = FetchResult()
                    evidence_val = getattr(fr.evidence_level, "value", str(fr.evidence_level))
                    updates: dict[str, Any] = {
                        "fetch_status": getattr(fr.fetch_status, "value", str(fr.fetch_status)),
                        "evidence_level": evidence_val,
                        "network_mode": fr.network_mode,
                        "access_path": fr.access_path,
                        "fulltext_url": fr.source_url or None,
                    }
                    if fr.has_fulltext and fr.text:
                        # 全文独立存储，摘要保持原文
                        updates["fulltext_text"] = fr.text
                        updates["content_hash"] = _sha256(fr.text)
                        article["fulltext_text"] = fr.text
                        stats["fulltext_fetched"] += 1
                        logger.info(
                            f"    ✓ 全文 {len(fr.text)} 字 [{fr.best_available_format},"
                            f" {fr.network_mode}/{fr.access_path}]"
                        )
                    elif fr.text and len(fr.text) >= 100:
                        # OpenAlex 等补全的摘要：写回 abstract 字段（不冒充全文）
                        updates["abstract"] = fr.text
                        article["abstract"] = fr.text
                        stats["abstract_completed"] += 1
                        logger.info(f"    ⚠ 摘要补全 {len(fr.text)} 字（不作为全文）")
                    else:
                        logger.info(
                            f"    ⚠ 未获取到全文 [status={fr.fetch_status}]"
                        )
                    if not db.update_article_fields(article["id"], **updates):
                        stats["db_errors"] += 1
                    article["evidence_level"] = evidence_val
        else:
            logger.info("  全文抓取已关闭或无候选文章，跳过。")
        progress("fulltext")

        # ── Step 5: LLM 深度解读（含分析失败重试）─────────────
        logger.info("Step 5: LLM 深度解读")
        relevant_articles = candidate_articles
        analyze_abstract_only = config.get("analyzer", {}).get("analyze_abstract_only", True)
        fulltext_count = sum(1 for a in relevant_articles if a.get("evidence_level") == "FULLTEXT")
        logger.info(
            f"  待解读: 全文 {fulltext_count} 篇，"
            f"仅摘要 {len(relevant_articles) - fulltext_count} 篇"
            f" (仅摘要解读开关={analyze_abstract_only})"
        )

        def _analyze_one(idx_article):
            idx, article = idx_article
            has_ft = article.get("evidence_level") == "FULLTEXT"
            if not has_ft and not analyze_abstract_only:
                article["analysis_status"] = "skipped"
                db.update_article_fields(article["id"], analysis_status="skipped", processed=1)
                stats["analyzed_skipped"] += 1
                return
            mode_str = "全文" if has_ft else "仅摘要"
            logger.info(
                f"  [{idx+1}/{len(relevant_articles)}] 解读({mode_str}): "
                f"{article['title'][:55]}..."
            )
            try:
                result = analyzer.analyze_article(article)
            except Exception as e:  # noqa: BLE001 - 单篇解读失败不阻断批次
                result = {"success": False, "analysis": "", "error": str(e), "evidence_level": "ERROR"}
            if result.get("success"):
                input_text = article.get("fulltext_text") or article.get("abstract") or ""
                ok = db.update_article_fields(
                    article["id"],
                    analysis=result["analysis"], analysis_status="ok",
                    analysis_model=analyzer.model, analysis_prompt_version=PROMPT_VERSION,
                    analysis_input_hash=_sha256(input_text + "|" + PROMPT_VERSION),
                    analyzed_at=datetime.now().isoformat(timespec="seconds"),
                    processed=1,
                )
                article["analysis"] = result["analysis"]
                article["analysis_status"] = "ok"
                stats["analyzed_ok"] += 1
                if not ok:
                    stats["db_errors"] += 1
            else:
                # 失败不写伪文本，保留失败状态供下次重试
                db.update_article_fields(
                    article["id"], analysis_status="failed",
                    analysis_error=str(result.get("error", ""))[:500],
                )
                article["analysis"] = None
                article["analysis_status"] = "failed"
                stats["analyzed_failed"] += 1

        with concurrent.futures.ThreadPoolExecutor(max_workers=llm_concurrency) as executor:
            list(executor.map(_analyze_one, enumerate(relevant_articles)))
        progress("analyzed")

        stats["score_succeeded"] = stats["scored_ok"]
        stats["score_failed"] = stats["scored_failed"]
        stats["llm_requests"] = analyzer.usage.get("requests", analyzer.usage.get("calls", 0))
        stats["budget_completed"] = True
        stats["backlog_remaining"] = stats["score_deferred"] + stats.get("analysis_deferred", 0)
        if trial:
            stats["trial"] = True
            stats["report_skipped_reason"] = "试运行不生成正式日报或推送"
            stats["tokens"] = dict(analyzer.usage)
            if own_task:
                db.task_finish(task_id, status="partial" if stats["scored_failed"] or stats["db_errors"] or stats.get("source_failed") or stats.get("source_incomplete") else "success", stats=stats)
            return stats

        # ── Step 6: 更新 HTML 索引（邮件需要附加最新版本）─────
        logger.info("Step 6: 更新数据库 HTML 索引")
        html_index_path = notifier.output_dir / "paper_index.html"
        logger.info(f"  HTML 索引路径: {html_index_path}")
        try:
            from utils.stat_db import build_html_index
            with db.get_connection() as conn:
                build_html_index(conn, threshold, str(html_index_path))
            if html_index_path.exists():
                logger.info(f"✅ HTML 索引已更新: {html_index_path}")
            else:
                logger.warning(f"⚠️ HTML 索引文件不存在: {html_index_path}")
        except Exception as e:
            logger.exception(f"HTML 索引生成失败: {e}")
        progress("index")

        # ── Step 7: 发布版本化日报（快照 → 渲染 → 渠道投递）────
        logger.info("Step 7: 发布版本化日报")
        output_cfg = config.get("output", {})
        report_channels = [
            ch for ch, enabled in (
                ("email", output_cfg.get("email", {}).get("enabled", False)),
                ("feishu", output_cfg.get("feishu", {}).get("enabled", False)),
            ) if enabled
        ]
        digest_service = DigestService(db, config, notifier=notifier)
        # 无任何新处理且当日已有发布版本：保留已有内容，不覆盖不重推。
        # 无新增但当日尚未发布时，仍从历史合格池选文发布（任务书 §4.1）
        existing_version = db.get_latest_published_digest_version(date_str)
        if (not new_articles and not stats["scored_ok"] and not stats["analyzed_ok"]
                and existing_version is not None):
            reason = "无新增文章且当日日报版本已存在，跳过重新生成与推送"
            stats["report_skipped_reason"] = reason
            existing_result = digest_service.get_digest(existing_version["id"])
            stats.update(digest_version_id=existing_result["version_id"],
                         digest_version=existing_result["version"],
                         digest_overall_status=existing_result["overall_status"],
                         digest_errors=existing_result["errors"],
                         selected_count=existing_result["selected_count"])
            logger.info(f"  {reason}")
        else:
            digest_result = None
            try:
                has_changes = bool(stats["scored_ok"] or stats["analyzed_ok"] or new_articles)
                if has_changes and existing_version is not None:
                    digest_result = digest_service.regenerate_digest(
                        date_str,
                        request_key=f"pipeline:{task_id or uuid.uuid4().hex}:{date_str}",
                        channels=report_channels,
                    )
                else:
                    digest_result = digest_service.publish_digest(
                        date_str, channels=report_channels)
            except DigestError as e:
                # 历史/配置/持久化失败：停止正式发布，不默认当成空历史
                logger.error(f"  日报发布失败 [{e.code}]: {e}")
                stats["report_skipped_reason"] = f"{e.code}: {e}"
                stats["digest_overall_status"] = "failed"
                stats["digest_errors"] = [e.to_dict()]
            if digest_result is not None:
                md_art = next((a for a in digest_result["artifacts"]
                               if a.get("format") == "markdown"), None)
                stats["report_path"] = (md_art or {}).get("path")
                stats["digest_version_id"] = digest_result["version_id"]
                stats["digest_version"] = digest_result["version"]
                stats["selected_count"] = digest_result["selected_count"]
                stats["digest_overall_status"] = digest_result["overall_status"]
                stats["digest_errors"] = digest_result["errors"]
                stats["digest_artifacts"] = digest_result["artifacts"]
                stats["digest_deliveries"] = digest_result["deliveries"]
                push_results = {
                    d["channel"]: d["status"] == "sent"
                    for d in digest_result["deliveries"]
                    if d.get("status") in ("sent", "failed")
                }
                stats["push_results"] = push_results or None
                if digest_result.get("note"):
                    logger.info(f"  {digest_result['note']}")
                for err in digest_result["errors"]:
                    logger.warning(f"  日报告警 [{err.get('code')}]: {err.get('message')}")
                # 兼容指针：daily_reports 保留为最新版本索引（权威内容在 digest_versions）
                db.save_report(
                    report_date=date_str,
                    file_path=stats["report_path"] or "",
                    total_found=stats.get("fetched_rss", 0),
                    total_pushed=digest_result["selected_count"],
                    push_results=push_results or None,
                )
                logger.info(
                    "  日报版本 v%d 已发布（created=%s, 精选 %d 篇）: %s",
                    digest_result["version"], digest_result["created"],
                    digest_result["selected_count"], stats["report_path"],
                )

        logger.info(
            f"========== 完成！抓取: {stats['fetched']} → 新增: {stats['new_articles']} "
            f"→ 相关: {len(relevant_articles)} =========="
        )
        stats["tokens"] = dict(analyzer.usage)

        if own_task:
            push_ok = all(bool(v) for v in (stats.get("push_results") or {}).values()) \
                if stats.get("push_results") else True
            has_failure = bool(
                stats["db_errors"] or stats["scored_failed"]
                or stats["analyzed_failed"] or stats.get("source_failed") or stats.get("source_incomplete") or not push_ok
                or stats.get("digest_overall_status") in ("partial", "failed")
            )
            task_status = ("failed" if stats.get("digest_overall_status") == "failed"
                           else "partial" if has_failure else "success")
            db.task_finish(task_id, status=task_status, stats=stats)
        return stats
    except Exception as e:
        if own_task:
            db.task_finish(task_id, status="failed", stats=stats, error=str(e))
        raise
    finally:
        try:
            if hasattr(fetcher, "close"):
                fetcher.close()
        finally:
            db.close()
            _run_lock.release()

# ── 定时调度 ──────────────────────────────────────────────────

def run_scheduler(config: dict):
    run_time = validate_scheduler_time(
        (config.get("scheduler") or {}).get("run_time", "08:00")
    )
    timezone = get_scheduler_timezone(config)
    hour, minute = map(int, run_time.split(":"))
    logger.info(f"调度模式启动，每日 {run_time} ({timezone.key}) 运行")

    last_run_date = None
    while True:
        now   = datetime.now(timezone)
        today = now.strftime("%Y-%m-%d")
        if now.hour == hour and now.minute == minute and last_run_date != today:
            logger.info("触发定时任务")
            try:
                run_once(config)
                last_run_date = today
            except Exception as e:
                logger.error(f"定时任务失败: {e}", exc_info=True)
        time.sleep(30)


# ── CLI 入口 ──────────────────────────────────────────────────

def push_only(config: dict, date_str: str) -> dict:
    """优先补发最新固定版本；无版本记录时兼容旧日报文件。"""
    db = Database(config["database"]["path"])
    try:
        version = db.get_latest_published_digest_version(date_str)
        notifier = Notifier(config)
        if version is not None:
            result = DigestService(db, config, notifier=notifier).retry_digest_send(version["id"])
            md_path = next((a.get("path") for a in result["artifacts"]
                            if a.get("format") == "markdown"), None)
            # retry 的 deliveries 只含本次尝试；兼容结果保留全部渠道状态。
            sends = db.list_digest_sends(version["id"])
            push_results = {
                s["channel"]: (True if s["status"] == "sent" else
                               False if s["status"] == "failed" else None)
                for s in sends
            }
            db.save_report(
                report_date=date_str, file_path=md_path or "",
                total_found=result["stats"].get("candidates", 0),
                total_pushed=result["selected_count"], push_results=push_results,
            )
            logger.info("版本 v%s 补发完成: %s", result["version"], push_results)
            return {**result, "report_path": md_path, "push_results": push_results}

        # 有版本记录但没有已发布版本时，也不能拿旧文件冒充补发。
        if db.list_digest_versions(date_from=date_str, date_to=date_str, limit=1):
            raise DigestError("当日版本尚未发布，无法补发", code="CONFLICT")
        md_path, push_results = notifier.resend(date_str)
        rep = db.get_report(date_str)
        db.save_report(
            report_date=date_str,
            file_path=rep["file_path"] if rep else md_path,
            total_found=rep["total_found"] if rep else 0,
            total_pushed=rep["total_pushed"] if rep else 0,
            push_results=push_results,
        )
        logger.info("旧日报补发完成: %s", push_results)
        return {"date": date_str, "report_path": md_path, "push_results": push_results,
                "version_id": None, "version": None}
    finally:
        db.close()


def run_weekly(config: dict, date_str: Optional[str] = None, task_id: Optional[str] = None) -> dict:
    """生成文献周报（方向分布对比/阅读盘点/引文追踪/热词）。"""
    from utils.weekly import build_weekly, _week_bounds
    db = Database(config["database"]["path"])
    try:
        date_str = date_str or datetime.now().strftime("%Y-%m-%d")
        md_path, html_path, label = build_weekly(config, db, date_str)
        start, end, _ = _week_bounds(date_str)
        week_articles = db.list_articles_created_between(start, end)
        relevant = [a for a in week_articles
                    if (a.get("relevance") or 0) >= config.get("relevance_threshold", 5)]
        db.save_report(
            report_date=f"week-{label}",
            file_path=md_path,
            total_found=len(week_articles),
            total_pushed=len(relevant),
            kind="weekly",
        )
        return {"label": label, "report_path": md_path, "html_path": html_path,
                "total_found": len(week_articles), "total_pushed": len(relevant)}
    finally:
        db.close()


def backup_database(config: dict, keep: int = 7) -> str:
    """备份数据库到 data/backups/，滚动保留最近 keep 份。"""
    import sqlite3
    from contextlib import closing
    src = resolve_against_root(config["database"]["path"])
    backup_dir = resolve_against_root(config.get("backup", {}).get("directory") or "data/backups")
    backup_dir.mkdir(parents=True, exist_ok=True)
    dest = backup_dir / f"{src.stem}-{datetime.now().strftime('%Y%m%d-%H%M%S-%f')}{src.suffix}"
    with closing(sqlite3.connect(src.as_uri() + "?mode=ro", uri=True)) as source:
        with closing(sqlite3.connect(dest)) as backup:
            source.backup(backup)
    backups = sorted(backup_dir.glob(f"{src.stem}-*{src.suffix}"))
    for old in backups[:-keep] if keep > 0 else []:
        old.unlink(missing_ok=True)
    logger.info(f"数据库已备份: {dest}（保留最近 {keep} 份）")
    return str(dest)


def main():
    parser = argparse.ArgumentParser(description="化学文献日报工具")
    parser.add_argument("--config",   default="config/config.yaml", help="配置路径（相对数据根）")
    parser.add_argument("--init-config", action="store_true", help="从内置模板创建配置，不覆盖已有文件")
    parser.add_argument("--schedule", action="store_true",   help="开启每日定时模式")
    parser.add_argument("--date",     default=None,          help="指定报告日期 (YYYY-MM-DD)")
    parser.add_argument("--push-only", default=None, metavar="DATE",
                        help="只补发指定日期的日报推送（不重新抓取/评分/分析）")
    parser.add_argument("--weekly", action="store_true", help="生成本周文献周报")
    parser.add_argument("--backup", action="store_true", help="备份数据库（滚动保留最近 N 份）")
    digest_mode = parser.add_mutually_exclusive_group()
    digest_mode.add_argument("--digest-dry-run", action="store_true",
                             help="预览 Top-N；只写审阅文本，不记录选择历史或发送")
    digest_mode.add_argument("--digest", action="store_true",
                             help="固定并渲染当日正式日报版本；同日复用，不抓取、不发送")
    parser.add_argument("--import-sources", metavar="PRESET_JSON",
                        help="导入来源预设到数据库（如 config/source_presets.json）；"
                             "与 --dry-run 同用只预览不导入。不触发采集/评分/推送")
    parser.add_argument("--dry-run", action="store_true",
                        help="与 --import-sources 同用：只显示将导入的清单")
    process_mode = parser.add_mutually_exclusive_group()
    process_mode.add_argument("--trial", action="store_true", help="小批量评分试运行，不生成正式日报或推送")
    process_mode.add_argument("--collect-preview", action="store_true", help="只读采集预览，不调用模型或写入业务库")
    parser.add_argument("--refresh", action="store_true",
                        help="忽略当日预览缓存，强制重新采集（与 --collect-preview 同用）")
    args = parser.parse_args()
    if args.dry_run and not args.import_sources:
        parser.error("--dry-run 必须与 --import-sources 配合；采集预览使用 --collect-preview")
    setup_logging(args.config)

    if args.init_config:
        try:
            path = init_config(args.config)
        except FileExistsError:
            parser.error(f"配置已存在，不覆盖: {resolve_against_root(args.config)}")
        print(f"配置已创建: {path}；请编辑后再运行。")
        return

    if args.import_sources:
        from utils import presets
        if args.dry_run:
            items = presets.preview_preset(args.import_sources)
            bad = sum(1 for it in items if not it["ok"])
            summary = f"预设共 {len(items)} 个来源（预览，未导入任何内容）"
            if bad:
                summary += f"，其中 {bad} 条无效（导入时将跳过并逐条报告）"
            print(summary + "：")
            for it in items:
                if not it["ok"]:
                    print(f"- {it['name']} [无效] {it['reason']}")
                    continue
                print(f"- {it['name']} [可导入] [{it['source_type']}] {it['canonical_url']}")
                if it.get("limitations"):
                    print(f"  已知限制: {it['limitations']}")
                print(f"  核验: {it['verified'] or '未核验'}")
            return
        config = load_config(args.config)
        db = Database(config["database"]["path"])
        try:
            result = presets.import_preset(args.import_sources, db)
        finally:
            db.close()
        print(f"预设 {result['preset']}: 新增 {len(result['added'])} 条，"
              f"跳过 {len(result['skipped'])} 条")
        for it in result["added"]:
            print(f"  + [{it['source_type']}] {it['name']} → {it['url']}")
        for it in result["skipped"]:
            print(f"  - {it['name']}: {it['reason']}")
        return

    config = load_config(args.config)
    if args.collect_preview:
        import json
        print(json.dumps(run_once(config, args.date, preview=True, refresh=args.refresh),
                         ensure_ascii=False, indent=2))
        return
    validate_config(config)

    # 查询/发布已有文献不拥有其他进程的流水线任务，不修改其 running 状态。
    if not (args.digest_dry_run or args.digest or args.push_only or args.backup):
        try:
            _db = Database(config["database"]["path"])
            try:
                n = _db.mark_interrupted_tasks()
                if n:
                    logger.info(f"已将 {n} 个遗留运行中的任务标记为 interrupted")
            finally:
                _db.close()
        except Exception as e:
            logger.warning(f"初始化任务记录失败: {e}")

    if args.push_only:
        result = push_only(config, args.push_only)
        logger.info(f"推送结果: {result['push_results']}")
        return

    if args.weekly:
        run_weekly(config, date_str=args.date)
        return

    if args.backup:
        keep = int(config.get("backup", {}).get("keep", 7))
        backup_database(config, keep=keep)
        return

    if args.digest_dry_run or args.digest:
        from digest.builder import build_daily_digest, format_dry_run_report
        db = Database(config["database"]["path"])
        try:
            if args.digest:
                import json
                result = DigestService(db, config, notifier=Notifier(config)).publish_digest(
                    args.date or datetime.now().strftime("%Y-%m-%d"), channels=[])
                print(json.dumps(result, ensure_ascii=False, indent=2))
            else:
                result = build_daily_digest(db, config, date_str=args.date, dry_run=True)
                report = format_dry_run_report(result)
                print(report)
                path = _write_dry_run_report(config, result["date"], report)
                if path:
                    logger.info(f"  dry-run 审阅文件: {path}")
        finally:
            db.close()
        return

    if args.schedule:
        run_scheduler(config)
    else:
        run_once(config, date_str=args.date, trial=args.trial)


if __name__ == "__main__":
    main()
