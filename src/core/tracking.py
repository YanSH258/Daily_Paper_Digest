"""
tracking.py - 引文追踪与作者追踪

collect_tracking_articles() 产生的文章 dict 与 RSS 条目结构兼容，
由主流水线统一去重、评分、入库；发现的关联关系（citation_edges）
在文章入库后由 record_tracking_edges() 落库。
"""
import logging
from datetime import datetime, timedelta
from typing import Any, Optional

logger = logging.getLogger(__name__)


def collect_tracking_articles(config: dict, db) -> tuple[list[dict], dict[str, int]]:
    """采集引文追踪与作者追踪的新文章。

    返回 (articles, meta)：meta 为辅助信息 {"citing_doi -> seed_id", "doi -> author_name"}，
    供入库后补 citation_edges 与日志使用。
    """
    from integrations import openalex

    openalex.set_polite_email(config.get("unpaywall_email", "your@email.com"))
    tracking_cfg = config.get("tracking", {}) or {}
    if not tracking_cfg.get("enabled", True):
        return [], {}

    articles: list[dict] = []
    # citing_seed: doi -> [seed_id, ...]  多对多：一篇引用可关联多个关注种子
    meta: dict[str, Any] = {"citing_seed": {}, "author": {}}
    seen_dois: set[str] = set()

    # ── 引文追踪：收藏/关注的文献有了新引用 ──────────────────────
    check_days = int(tracking_cfg.get("citation_check_days", 3))
    seeds = db.get_watched_seeds()
    for seed in seeds:
        doi = (seed.get("doi") or "").strip()
        if not doi:
            continue
        since = seed.get("last_checked_at") or _default_since(seed.get("created_at"), check_days)
        try:
            citing = openalex.get_citing_works(doi, from_date=since)
        except Exception as e:  # noqa: BLE001 - 单篇失败不阻断，且不推进游标
            logger.warning("引文追踪失败 (%s): %s", doi, e)
            continue
        # 游标推迟到文章成功入库之后（见函数末尾 mark_successful_seeds），
        # 避免"采集返回但入库前进程中断"造成该窗口文献永久漏采
        seed["__window_since"] = since
        for work in citing:
            w_doi = (work.get("doi") or "").strip().lower()
            if w_doi:
                seeds_for_doi = meta["citing_seed"].setdefault(w_doi, [])
                if seed["id"] not in seeds_for_doi:
                    seeds_for_doi.append(seed["id"])
            if w_doi and w_doi in seen_dois:
                # 文章已收集，但仍记录与当前 seed 的边
                continue
            if w_doi:
                seen_dois.add(w_doi)
            work["discovered_via"] = "citation_watch"
            work["publisher"] = "DEFAULT"
            articles.append(work)
        meta.setdefault("seed_windows", {})[seed["id"]] = since
        if citing:
            logger.info("引文追踪: %s 新增 %d 篇引用", seed["title"][:50], len(citing))

    # ── 作者追踪：关注作者有新文章 ──────────────────────────────
    from_date = (datetime.now() - timedelta(days=int(tracking_cfg.get("author_check_days", 3))
                                            )).strftime("%Y-%m-%d")
    for author in db.list_watch_authors(enabled_only=True):
        oid = author.get("openalex_id")
        if not oid:
            continue
        try:
            works = openalex.get_author_recent_works(oid, from_date=from_date)
        except Exception as e:  # noqa: BLE001
            logger.warning("作者追踪失败 (%s): %s", author["name"], e)
            continue
        # 同引文追踪：游标推迟到入库成功后推进（见 mark_tracking_cursor）
        meta.setdefault("seed_windows", {})
        meta.setdefault("author_windows", {})[author["id"]] = True
        count = 0
        for work in works:
            w_doi = (work.get("doi") or "").strip()
            if w_doi and w_doi.lower() in seen_dois:
                continue
            if w_doi:
                seen_dois.add(w_doi.lower())
            work["discovered_via"] = "author_watch"
            work["publisher"] = "DEFAULT"
            articles.append(work)
            meta["author"][w_doi.lower()] = author["name"]
            count += 1
        if count:
            logger.info("作者追踪: %s 新增 %d 篇", author["name"], count)

    return articles, meta


def mark_tracking_cursor(db, meta: dict[str, Any]) -> None:
    """在追踪文章全部入库成功后调用：推进引文/作者游标。

    collect_tracking_articles 不再提前推进游标；只有调用方确认入库完成后
    才调用本函数，保证"失败窗口可重试、成功窗口不重复采集"。
    """
    if not meta:
        return
    for seed_id in (meta.get("seed_windows") or {}):
        db.mark_seed_checked(seed_id)
    for author_id in (meta.get("author_windows") or {}):
        db.mark_author_run(author_id)


def record_tracking_edges(db, inserted: list[tuple[Optional[int], dict]],
                          meta: dict[str, Any]) -> int:
    """文章入库后补 citation_edges / discovered_via 元数据。

    inserted: [(article_id, article_dict), ...]
    citing_seed 支持 doi -> seed_id 或 doi -> [seed_id, ...]
    """
    edges = 0
    citing_seed: dict = meta.get("citing_seed", {})
    author_meta: dict = meta.get("author", {})
    for aid, article in inserted:
        if aid is None:
            continue
        doi = (article.get("doi") or "").strip().lower()
        seed_ref = citing_seed.get(doi)
        seed_ids: list = []
        if isinstance(seed_ref, (list, tuple, set)):
            seed_ids = list(seed_ref)
        elif seed_ref is not None:
            seed_ids = [seed_ref]
        for seed_id in seed_ids:
            if db.add_citation_edge(int(seed_id), aid):
                edges += 1
        author_name = author_meta.get(doi)
        if author_name and not article.get("relevance_reason"):
            article["relevance_reason"] = f"来自关注作者 {author_name} 的新文章"
    return edges


def _default_since(created_at: Optional[str], check_days: int) -> str:
    if created_at:
        return str(created_at)[:10]
    return (datetime.now() - timedelta(days=check_days)).strftime("%Y-%m-%d")
