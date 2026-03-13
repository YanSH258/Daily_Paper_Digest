"""
main.py - 主入口
两阶段流程：
  1. RSS摘要快速过滤相关性
  2. 只对通过门槛的文章读取全文
  3. 用全文做深度解读

用法：
  python main.py              # 立即运行一次
  python main.py --schedule   # 按 config.yaml 中的时间每日定时运行
  python main.py --date 2024-01-15  # 指定报告日期
"""
import sys
import time
import logging
import argparse
from datetime import datetime
from pathlib import Path

import yaml

from db       import Database
from fetcher  import JournalFetcher, RSS_ONLY_PUBLISHERS
from analyzer import LLMAnalyzer
from notifier import Notifier
from fetchers.models import FetchStatus

# ── 日志配置 ──────────────────────────────────────────────────
LOG_DIR = Path("logs")
LOG_DIR.mkdir(exist_ok=True)

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
logger = logging.getLogger("main")


# ── 核心流程 ──────────────────────────────────────────────────

def load_config(path: str = "config.yaml") -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def run_once(config: dict, date_str: str = None):
    date_str  = date_str or datetime.now().strftime("%Y-%m-%d")
    threshold = config.get("relevance_threshold", 5)

    logger.info(f"========== 开始运行: {date_str} ==========")

    db       = Database(config["database"]["path"])
    fetcher  = JournalFetcher(config)
    analyzer = LLMAnalyzer(config)
    notifier = Notifier(config)

    # ── Step 1: 抓取 RSS ──────────────────────────────────────
    logger.info("Step 1: 抓取期刊 RSS")
    raw_articles = fetcher.fetch_all()
    logger.info(f"  共抓取: {len(raw_articles)} 篇原始文章")

    # ── Step 2: 数据库去重 ────────────────────────────────────
    logger.info("Step 2: 数据库去重")
    new_articles = [a for a in raw_articles if not db.is_processed(a.get("doi", ""))]
    logger.info(f"  去重后: {len(new_articles)} 篇新文章")

    if not new_articles:
        logger.info("没有新文章，流程结束。")
        # 仍然生成空报告
        notifier.notify([], all_articles=[], date_str=date_str)
        return

    # ── Step 3: 第一阶段 - 用摘要快速过滤相关性 ──────────────
    logger.info(f"Step 3: LLM 相关性过滤-第一阶段 (阈值={threshold}, 使用摘要)")
    candidate_articles = []
    for i, article in enumerate(new_articles):
        logger.info(f"  [{i+1}/{len(new_articles)}] 评分: {article['title'][:60]}...")
        score = analyzer.filter_relevance(article)
        article["relevance"] = score
        if score >= threshold:
            candidate_articles.append(article)
            logger.info(f"    ✓ 入选 (score={score:.1f})")
        else:
            logger.info(f"    ✗ 过滤 (score={score:.1f})")
        time.sleep(0.5)

    logger.info(f"  初筛通过: {len(candidate_articles)} 篇")

    # ── Step 4: 第二阶段 - 对入选文章读取全文 ────────────────
    logger.info("Step 4: 获取相关文章全文")
    use_fulltext = config.get("fetcher", {}).get("use_fulltext", True)

    if use_fulltext and candidate_articles:
        for i, article in enumerate(candidate_articles):
            publisher = article.get("publisher", "DEFAULT")
            if publisher in RSS_ONLY_PUBLISHERS:
                logger.info(
                    f"  [{i+1}/{len(candidate_articles)}] {article['journal']}: "
                    f"RSS摘要已足够，跳过全文抓取"
                )
                continue

            logger.info(
                f"  [{i+1}/{len(candidate_articles)}] 读取全文: "
                f"{article['title'][:55]}..."
            )
            fetch_result = fetcher.fetch_fulltext_with_status(article)
            article["fetch_status"] = fetch_result.fetch_status
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
                if fetch_result.fetch_status == FetchStatus.WAITING_USER_UPLOAD:
                    logger.warning(
                        f"    ⚠ 全文获取失败 ({fetch_result.error_code})，"
                        f"可运行: python -m fetchers.manual_upload "
                        f"--file <文件路径> --doi {article.get('doi', '')}"
                    )
                else:
                    logger.info(
                        f"    ⚠ 保持摘要 ({len(article.get('abstract',''))} 字)"
                        f" [status={fetch_result.fetch_status}]"
                    )
            time.sleep(1.5)
    else:
        logger.info("  全文抓取已关闭或无候选文章，跳过。")

    # ── Step 5: LLM 深度解读 ──────────────────────────────────
    logger.info("Step 5: LLM 深度解读")
    relevant_articles = candidate_articles
    for i, article in enumerate(relevant_articles):
        src = "全文" if article.get("has_fulltext") else "摘要"
        logger.info(
            f"  [{i+1}/{len(relevant_articles)}] 解读({src}): "
            f"{article['title'][:55]}..."
        )
        result = analyzer.analyze_article(article)
        article["analysis"] = result.get("analysis", "解读失败")
        time.sleep(1)

    # ── Step 6: 保存数据库记录 ────────────────────────────────
    logger.info("Step 6: 保存数据库记录")
    for article in new_articles:
        db.save_article(article)

    # ── Step 7: 生成报告并推送 ────────────────────────────────
    logger.info("Step 7: 生成报告")
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
    parser = argparse.ArgumentParser(description="化学文献日报工具")
    parser.add_argument("--config",   default="config.yaml", help="配置文件路径")
    parser.add_argument("--schedule", action="store_true",   help="开启每日定时模式")
    parser.add_argument("--date",     default=None,          help="指定报告日期 (YYYY-MM-DD)")
    args = parser.parse_args()

    config = load_config(args.config)

    if args.schedule:
        run_scheduler(config)
    else:
        run_once(config, date_str=args.date)


if __name__ == "__main__":
    main()
