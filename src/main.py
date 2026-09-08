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
import uuid
import time
import hashlib
import logging
import argparse
import concurrent.futures
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

sys.path.insert(0, str(Path(__file__).parent))

import yaml

from core.db       import Database
from core.fetcher  import JournalFetcher, RSS_ONLY_PUBLISHERS
from core.analyzer import LLMAnalyzer, PROMPT_VERSION
from core.notifier import Notifier, classify_article
from fetchers.models import FetchResult

# ── 日志配置 ──────────────────────────────────────────────────
LOG_DIR = Path(__file__).resolve().parent.parent / "data" / "logs"

_logger_ready = False


def setup_logging() -> None:
    """初始化日志（幂等）。在 CLI 入口与 Web 服务入口调用。"""
    global _logger_ready
    if _logger_ready:
        return
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


def get_output_dir(config: dict) -> Path:
    """统一的输出目录解析：config.output.output_dir，锚定到项目根目录。"""
    out = config.get("output", {}).get("output_dir", "data/output")
    p = Path(out)
    if not p.is_absolute():
        p = Path(__file__).resolve().parent.parent / p
    return p


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

    # Feishu Webhook：优先读取环境变量 FEISHU_WEBHOOK_URL（配置位于 output.feishu 下）
    env_feishu = os.environ.get("FEISHU_WEBHOOK_URL")
    if env_feishu:
        cfg.setdefault("output", {}).setdefault("feishu", {})["webhook_url"] = env_feishu

    return cfg


def load_config(path: str = "config/config.yaml") -> dict:
    with open(path, "r", encoding="utf-8") as f:
        cfg: dict = yaml.safe_load(f)
    return _apply_env_overrides(cfg)


def load_config_from_obj(cfg: dict) -> dict:
    """基于内存中的配置对象（如 ruamel CommentedMap）生成应用环境变量后的副本，
    供设置保存前做完整校验，不落盘。"""
    return _apply_env_overrides(copy.deepcopy(cfg))


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
            "name": j.get("name") or "未命名订阅",
            "rss": j["rss"],
            "publisher": j.get("publisher") or "DEFAULT",
            "max_articles": int(j.get("max_articles") or 100),
        }
        for j in rows
    ]
    logger.info(f"订阅源共 {len(journals)} 个（数据库，启用状态）")
    return journals


def _sha256(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


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


def run_once(config: dict, date_str: Optional[str] = None, task_id: Optional[str] = None) -> dict:
    """执行一次完整流水线，返回结构化运行结果。

    分阶段状态实时落库（score_status / evidence_level / analysis_status），
    评分或分析失败的文章保留在库中，下次运行自动补齐失败阶段。
    外部调用方（如 TaskRunner）传入 task_id 时由其负责收尾任务记录；
    否则 run_once 自己记录 start/finish。
    """
    date_str  = date_str or datetime.now().strftime("%Y-%m-%d")
    threshold = config.get("relevance_threshold", 5)
    retry_window_days = int(config.get("fetcher", {}).get("retry_window_days", 7))

    stats: dict[str, Any] = {
        "date": date_str,
        "fetched": 0, "batch_duplicates": 0, "db_duplicates": 0,
        "new_articles": 0, "retried_score": 0, "retried_analysis": 0,
        "scored_ok": 0, "scored_failed": 0,
        "candidates": 0, "fulltext_fetched": 0, "abstract_completed": 0,
        "analyzed_ok": 0, "analyzed_failed": 0, "analyzed_skipped": 0,
        "saved": 0, "db_errors": 0,
        "report_skipped_reason": None, "report_path": None,
        "push_results": None,
    }

    own_task = False
    if not task_id:
        task_id = str(uuid.uuid4())
        own_task = True

    logger.info(f"========== 开始运行: {date_str} (task_id={task_id}) ==========")

    db       = Database(config["database"]["path"])
    config["journals"] = load_journals_config(config, db)
    fetcher  = JournalFetcher(config)
    analyzer = LLMAnalyzer(config)
    notifier = Notifier(config)

    if own_task:
        db.task_start(task_id, trigger="cli", mode="default", date_str=date_str)

    def progress(stage: str) -> None:
        if not own_task:
            db.task_finish(task_id, status="running", stats=stats, stage=stage)

    try:
        # ── Step 1: 抓取 RSS ──────────────────────────────────
        logger.info("Step 1: 抓取期刊 RSS")
        raw_articles = fetcher.fetch_all()
        stats["fetched"] = len(raw_articles)
        logger.info(f"  共抓取: {len(raw_articles)} 篇原始文章")

        # ── Step 1.5: 批次内去重 ──────────────────────────────
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
        inserted_ids = db.save_articles_batch(new_articles)
        stats["saved"] = sum(1 for i in inserted_ids if i is not None)
        if stats["saved"] < len(new_articles):
            stats["db_errors"] += 1
            logger.error(f"  有 {len(new_articles) - stats['saved']} 篇文章入库失败")
        for a, aid in zip(new_articles, inserted_ids):
            a["id"] = aid
        new_articles = [a for a in new_articles if a.get("id") is not None]
        logger.info(f"  已入库基础记录 {stats['saved']} 篇（待评分）")
        progress("saved_base")

        # ── Step 2.6: 载入需要重试的历史文章 ──────────────────
        retry = db.get_retry_articles(threshold, days=retry_window_days)
        score_retry = retry.get("score_failed", [])
        analysis_retry = retry.get("analysis_failed", [])
        stats["retried_score"] = len(score_retry)
        stats["retried_analysis"] = len(analysis_retry)
        if score_retry or analysis_retry:
            logger.info(
                f"  重试队列: 评分失败 {len(score_retry)} 篇，分析失败 {len(analysis_retry)} 篇"
            )
            for a in score_retry:
                a["_retry_score"] = True

        # ── Step 2.7: 缺摘要文章先补全，再评分（避免把缺信息误判为低相关）──
        if new_articles:
            from fetchers.oa_fetcher import get_openalex_abstract
            unpaywall_email = config.get("unpaywall_email", "your@email.com")
            for a in new_articles:
                if a.get("abstract", "").strip() or not a.get("doi"):
                    continue
                try:
                    completed = get_openalex_abstract(a["doi"])
                except Exception as e:
                    logger.debug(f"  摘要补全查询失败 ({a['doi']}): {e}")
                    completed = None
                if completed:
                    a["abstract"] = completed
                    db.update_article_fields(a["id"], abstract=completed)
                    stats["abstract_completed"] += 1
                    logger.info(f"  评分前摘要补全: {a['title'][:50]}... ({len(completed)} 字)")
        progress("abstract_backfill")

        # ── Step 3: LLM 相关性评分（新文章 + 评分失败重试）────
        to_score = list(new_articles) + list(score_retry)
        logger.info(f"Step 3: LLM 相关性评分 (阈值={threshold}，共 {len(to_score)} 篇)")
        score_results: list[Any] = [None] * len(to_score)

        def _score_one(idx_article):
            idx, article = idx_article
            logger.info(f"  [{idx+1}/{len(to_score)}] 评分: {article['title'][:60]}...")
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
                )
                article.update({
                    "relevance": res["score"], "relevance_reason": res["reason"],
                    "score_status": "ok",
                })
                stats["scored_ok"] += 1
                if not ok:
                    stats["db_errors"] += 1
                if res["score"] >= threshold:
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
        candidate_articles.extend(analysis_retry)
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

        # ── Step 7: 生成报告并推送 ─────────────────────────────
        logger.info("Step 7: 生成报告并推送")
        relevant_articles.sort(key=lambda a: -(a.get("relevance") or 0))
        existing_report = db.get_report(date_str)

        if (not new_articles and not score_retry and not analysis_retry
                and existing_report is not None):
            # 无任何新处理且当日报告已存在：保留已有内容，不覆盖不重推
            reason = "无新增文章且当日报告已存在，跳过重新生成与推送"
            stats["report_skipped_reason"] = reason
            logger.info(f"  {reason}")
        else:
            md_path, push_results = notifier.notify(
                relevant_articles,
                all_articles=new_articles,
                date_str=date_str,
            )
            db.save_report(
                report_date=date_str,
                file_path=md_path,
                total_found=len(new_articles),
                total_pushed=len(relevant_articles),
                push_results=push_results,
            )
            stats["report_path"] = md_path
            stats["push_results"] = push_results

        logger.info(
            f"========== 完成！抓取: {stats['fetched']} → 新增: {stats['new_articles']} "
            f"→ 相关: {len(relevant_articles)} =========="
        )

        if own_task:
            has_failure = (stats["db_errors"] or stats["scored_failed"]
                           or stats["analyzed_failed"])
            db.task_finish(task_id, status="partial" if has_failure else "success", stats=stats)
        return stats
    except Exception as e:
        if own_task:
            db.task_finish(task_id, status="failed", stats=stats, error=str(e))
        raise

# ── 定时调度 ──────────────────────────────────────────────────

def run_scheduler(config: dict):
    run_time = config.get("scheduler", {}).get("run_time", "08:00")
    hour, minute = map(int, run_time.split(":"))
    logger.info(f"调度模式启动，每日 {run_time} 运行")

    last_run_date = None
    while True:
        now   = datetime.now()
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
    """只补发指定日期的日报推送，不重新抓取、评分或分析。"""
    notifier = Notifier(config)
    md_path, push_results = notifier.resend(date_str)
    logger.info(f"补发完成: {push_results}")

    db = Database(config["database"]["path"])
    rep = db.get_report(date_str)
    db.save_report(
        report_date=date_str,
        file_path=rep["file_path"] if rep else md_path,
        total_found=rep["total_found"] if rep else 0,
        total_pushed=rep["total_pushed"] if rep else 0,
        push_results=push_results,
    )
    return {"date": date_str, "report_path": md_path, "push_results": push_results}


def main():
    setup_logging()
    parser = argparse.ArgumentParser(description="化学文献日报工具")
    parser.add_argument("--config",   default="config/config.yaml", help="配置文件路径")
    parser.add_argument("--schedule", action="store_true",   help="开启每日定时模式")
    parser.add_argument("--date",     default=None,          help="指定报告日期 (YYYY-MM-DD)")
    parser.add_argument("--push-only", default=None, metavar="DATE",
                        help="只补发指定日期的日报推送（不重新抓取/评分/分析）")
    args = parser.parse_args()

    config = load_config(args.config)
    validate_config(config)

    # 启动时把上次异常退出遗留的 running 任务标记为 interrupted
    try:
        _db = Database(config["database"]["path"])
        n = _db.mark_interrupted_tasks()
        if n:
            logger.info(f"已将 {n} 个遗留运行中的任务标记为 interrupted")
        _db.close()
    except Exception as e:
        logger.warning(f"初始化任务记录失败: {e}")

    if args.push_only:
        result = push_only(config, args.push_only)
        logger.info(f"推送结果: {result['push_results']}")
        return

    if args.schedule:
        run_scheduler(config)
    else:
        run_once(config, date_str=args.date)


if __name__ == "__main__":
    main()
