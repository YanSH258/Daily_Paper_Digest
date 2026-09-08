"""
main.py - 主入口
两阶段流程：
  1. RSS摘要快速过滤相关性
  2. 只对通过门槛的文章读取全文
  3. 用全文做深度解读

用法：
  python src/main.py                          # 立即运行一次
  python src/main.py --schedule               # 按 config.yaml 中的时间每日定时运行
  python src/main.py --date 2024-01-15        # 指定报告日期
  python src/main.py --config config/config.yaml  # 指定配置文件路径
"""
import os
import sys
import copy
import time
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
from core.analyzer import LLMAnalyzer
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


def run_once(config: dict, date_str: Optional[str] = None) -> None:
    date_str  = date_str or datetime.now().strftime("%Y-%m-%d")
    threshold = config.get("relevance_threshold", 5)

    logger.info(f"========== 开始运行: {date_str} ==========")

    db       = Database(config["database"]["path"])
    config["journals"] = load_journals_config(config, db)
    fetcher  = JournalFetcher(config)
    analyzer = LLMAnalyzer(config)
    notifier = Notifier(config)

    # ── Step 1: 抓取 RSS ──────────────────────────────────────
    logger.info("Step 1: 抓取期刊 RSS")
    raw_articles = fetcher.fetch_all()
    logger.info(f"  共抓取: {len(raw_articles)} 篇原始文章")

    # ── Step 2: 数据库去重 ────────────────────────────────────
    logger.info("Step 2: 数据库去重")
    new_articles = []
    skipped_count = 0
    for a in raw_articles:
        is_dup, reason = db.check_duplicate(a)
        if is_dup:
            logger.debug(f"  跳过重复: {reason}")
            skipped_count += 1
        else:
            new_articles.append(a)
    logger.info(
        f"  去重后: {len(new_articles)} 篇新文章（跳过 {skipped_count} 篇重复）"
    )

    if not new_articles:
        logger.info("没有新文章，流程结束。")
        # 仍然生成空报告
        notifier.notify([], all_articles=[], date_str=date_str)
        return

    # ── Step 3: 第一阶段 - 用摘要快速过滤相关性 ──────────────
    logger.info(f"Step 3: LLM 相关性过滤-第一阶段 (阈值={threshold}, 使用摘要)")
    candidate_articles = []
    scores = [None] * len(new_articles)

    def _score_one(idx_article):
        idx, article = idx_article
        logger.info(f"  [{idx+1}/{len(new_articles)}] 评分: {article['title'][:60]}...")
        try:
            score = analyzer.filter_relevance(article)
            scores[idx] = score
        except Exception as e:
            logger.error(f"  评分失败 (idx={idx}): {e}")
            scores[idx] = -1

    llm_concurrency = config.get("performance", {}).get("llm_concurrency", 3)
    with concurrent.futures.ThreadPoolExecutor(max_workers=llm_concurrency) as executor:
        list(executor.map(_score_one, enumerate(new_articles)))

    for i, article in enumerate(new_articles):
        score = scores[i] if scores[i] is not None else -1
        article["relevance"] = score
        if score >= threshold:
            candidate_articles.append(article)
            logger.info(f"    ✓ 入选: {article['title'][:60]} (score={score:.1f})")
        else:
            logger.info(f"    ✗ 过滤: {article['title'][:60]} (score={score:.1f})")

    logger.info(f"  初筛通过: {len(candidate_articles)} 篇")

    # ── Step 4: 第二阶段 - 对入选文章读取全文 ────────────────
    logger.info("Step 4: 获取相关文章全文")
    use_fulltext = config.get("fetcher", {}).get("use_fulltext", True)

    if use_fulltext and candidate_articles:
        # 筛选需要抓取全文的文章
        articles_to_fetch = [
            (i, a) for i, a in enumerate(candidate_articles)
            if a.get("publisher", "DEFAULT") not in RSS_ONLY_PUBLISHERS
        ]

        # 并发批量获取全文
        if articles_to_fetch:
            only_articles = [a for _, a in articles_to_fetch]
            perf_cfg = config.get("performance", {})
            concurrency = perf_cfg.get("concurrency", 5)

            logger.info(f"  并发全文获取（并发度={concurrency}，共 {len(only_articles)} 篇）")
            fetch_results = fetcher.fetch_fulltext_batch(only_articles)

            for (orig_idx, article), fetch_result in zip(articles_to_fetch, fetch_results):
                logger.info(
                    f"  [{orig_idx + 1}/{len(candidate_articles)}] 读取全文: "
                    f"{article['title'][:55]}..."
                )
                if fetch_result is None:
                    fetch_result = FetchResult()

                article["fetch_status"]          = fetch_result.fetch_status
                article["best_available_format"] = fetch_result.best_available_format
                article["network_mode"] = fetch_result.network_mode
                article["access_path"] = fetch_result.access_path

                if fetch_result.text and len(fetch_result.text) > len(article.get("abstract", "")):
                    article["abstract"]     = fetch_result.text
                    article["has_fulltext"] = True
                    article["evidence_level"] = fetch_result.evidence_level
                    logger.info(
                        f"    ✓ 全文 {len(fetch_result.text)} 字"
                        f" [{fetch_result.best_available_format},"
                        f" {fetch_result.network_mode}/{fetch_result.access_path}]"
                    )
                else:
                    article["has_fulltext"] = False
                    article["evidence_level"] = "ABSTRACT_ONLY"
                    logger.info(
                        f"    ⚠ 保持摘要 ({len(article.get('abstract',''))} 字)"
                        f" [status={fetch_result.fetch_status}]"
                    )

        # 记录 RSS_ONLY 期刊
        for i, article in enumerate(candidate_articles):
            if article.get("publisher", "DEFAULT") in RSS_ONLY_PUBLISHERS:
                logger.info(
                    f"  [{i+1}/{len(candidate_articles)}] {article['journal']}: "
                    f"RSS摘要已足够，跳过全文抓取"
                )

        # 打印请求统计
        fetcher._request_manager.log_stats()
    else:
        logger.info("  全文抓取已关闭或无候选文章，跳过。")

    # ── Step 5: LLM 深度解读 ──────────────────────────────────
    logger.info("Step 5: LLM 深度解读")
    relevant_articles = candidate_articles

    # 统计需要解读的文章数
    analyze_abstract_only = config.get("analyzer", {}).get("analyze_abstract_only", True)
    fulltext_count = sum(1 for a in relevant_articles if a.get("has_fulltext", False))
    abstract_only_count = len(relevant_articles) - fulltext_count
    logger.info(
        f"  待解读文章: 全文 {fulltext_count} 篇，"
        f"仅摘要 {abstract_only_count} 篇 (仅摘要解读开关={analyze_abstract_only})"
    )

    def _analyze_one(idx_article):
        idx, article = idx_article
        has_ft = article.get("has_fulltext", False)

        if not has_ft and not analyze_abstract_only:
            logger.info(
                f"  [{idx+1}/{len(relevant_articles)}] 跳过解读(仅摘要且开关未开启): "
                f"{article['title'][:55]}..."
            )
            article["analysis"] = None
            return

        mode_str = "全文" if has_ft else "仅摘要"
        logger.info(
            f"  [{idx+1}/{len(relevant_articles)}] 解读({mode_str}): "
            f"{article['title'][:55]}..."
        )
        try:
            result = analyzer.analyze_article(article)
            article["analysis"] = result.get("analysis", "解读失败")
        except Exception as e:
            logger.error(f"  解读失败 (idx={idx}): {e}")
            article["analysis"] = f"解读失败: {e}"

    # 并发 LLM 解读（受 API 速率限制，建议并发度 2-3）
    llm_concurrency = config.get("performance", {}).get("llm_concurrency", 3)
    with concurrent.futures.ThreadPoolExecutor(max_workers=llm_concurrency) as executor:
        list(executor.map(_analyze_one, enumerate(relevant_articles)))

    # ── Step 6: 保存数据库记录 ────────────────────────────────
    logger.info("Step 6: 保存数据库记录")
    # 把 analysis 挂到 new_articles 里对应的相关文章上，并提前计算 topic 分类
    relevant_map = {a.get("doi") or a.get("url"): a for a in relevant_articles}
    for article in new_articles:
        key = article.get("doi") or article.get("url")
        if key and key in relevant_map:
            article["analysis"] = relevant_map[key].get("analysis")
        if not article.get("topic"):
            article["topic"] = classify_article(article)

    # 批量入库，单事务提升写入性能
    saved_count = db.save_articles_batch(new_articles)
    logger.info(f"  已批量保存 {saved_count} 篇新文章到数据库")


    # ── Step 7: 先更新 HTML 索引（邮件需要附加最新版本）───────
    logger.info("Step 7: 更新数据库 HTML 索引")
    # 输出目录统一由 config.output.output_dir 解析（与日报、附件一致）
    html_index_path = notifier.output_dir / "paper_index.html"
    logger.info(f"  HTML 索引路径: {html_index_path}")
    try:
        from utils.stat_db import build_html_index
        _threshold = config.get("relevance_threshold", 5)
        with db.get_connection() as conn:
            # 查询数据库中有多少篇文章
            cur = conn.execute("SELECT COUNT(*) FROM articles")
            total_in_db = cur.fetchone()[0]
            logger.info(f"  数据库中共有 {total_in_db} 篇文章")
            build_html_index(conn, _threshold, str(html_index_path))
        # 验证文件是否真的被更新了
        if html_index_path.exists():
            mtime = datetime.fromtimestamp(html_index_path.stat().st_mtime)
            size = html_index_path.stat().st_size
            logger.info(f"✅ HTML 索引已更新: {html_index_path}")
            logger.info(f"   修改时间: {mtime.strftime('%Y-%m-%d %H:%M:%S')}, 大小: {size} bytes")
        else:
            logger.warning(f"⚠️ HTML 索引文件不存在: {html_index_path}")
    except Exception as e:
        logger.exception(f"HTML 索引生成失败: {e}")

    # ── Step 8: 生成报告并推送 ─────────────────────────────────
    logger.info("Step 8: 生成报告并推送")
    md_path = notifier.notify(
        relevant_articles,
        all_articles=new_articles,
        date_str=date_str
    )

    db.save_report(
        report_date=date_str,
        file_path=md_path,
        total_found=len(new_articles),
        total_pushed=len(relevant_articles),
    )

    logger.info(f"========== 完成！报告: {md_path} ==========")
    logger.info(
        f"  抓取: {len(raw_articles)} → 去重: {len(new_articles)} "
        f"→ 相关: {len(relevant_articles)}"
    )

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

def main():
    setup_logging()
    parser = argparse.ArgumentParser(description="化学文献日报工具")
    parser.add_argument("--config",   default="config/config.yaml", help="配置文件路径")
    parser.add_argument("--schedule", action="store_true",   help="开启每日定时模式")
    parser.add_argument("--date",     default=None,          help="指定报告日期 (YYYY-MM-DD)")
    args = parser.parse_args()

    config = load_config(args.config)
    validate_config(config)

    if args.schedule:
        run_scheduler(config)
    else:
        run_once(config, date_str=args.date)


if __name__ == "__main__":
    main()
