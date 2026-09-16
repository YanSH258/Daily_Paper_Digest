"""从数据库构建每日 Top-N，并支持 dry-run 解释输出。"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Optional

from .config import collect_daily_config
from .scorer import score_article
from .selector import SelectionResult, select_daily_top


# 候选池保护上限：防止极端大库拖慢评分；窗口内按 (pool_date desc, relevance desc,
# id asc) 截取——id 唯一，保证截断结果确定（docs/DIGEST_RELEASE_SPEC.md §3.6）
POOL_HARD_LIMIT = 2000


def _pool_date_expr() -> str:
    """文章的候选池日期：ISO 格式的 pub_date 优先，否则回退 created_at（入库日）。"""
    return (
        "COALESCE("
        "CASE WHEN pub_date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9]*' "
        "THEN substr(pub_date, 1, 10) END, "
        "substr(COALESCE(created_at, ''), 1, 10), '')"
    )


def _load_metrics(db) -> dict[str, dict]:
    out: dict[str, dict] = {}
    try:
        for m in db.list_journal_metrics():
            out[m.get("name") or ""] = m
    except Exception:  # noqa: BLE001
        pass
    return out


def build_daily_digest(
    db,
    config: dict[str, Any],
    *,
    date_str: Optional[str] = None,
    dry_run: bool = True,
) -> dict[str, Any]:
    """返回 {date, selection: SelectionResult, pool_stats, config}。

    dry_run=True 时只读库，不写 digest_entries。
    """
    date_str = date_str or datetime.now().strftime("%Y-%m-%d")
    # 配置归一化与校验唯一出口（digest.config）：非法配置直接抛错，不静默改默认
    values, config_errors = collect_daily_config(config)
    if config_errors:
        raise ValueError("digest 配置无效: " + "; ".join(config_errors))
    limit = values["limit"]
    window_days = values["repeat_window_days"]
    pool_window_days = values["pool_window_days"]
    min_score = values["min_score"]
    category_limits = values["category_limits"]
    top_journals = ((config.get("digest") or {}).get("tracks") or {}).get("top_chemistry", {}).get("journals")

    metrics = _load_metrics(db)
    now = datetime.strptime(date_str, "%Y-%m-%d") + timedelta(hours=12)

    # 候选池：已评分达阈值 + 候选日期在 [报告日期−pool_window, 报告日期] 内。
    # 上限即报告日期：未来发表的论文与历史重放日期之后才入库的文章不进池；
    # created_at 独立限制（不经 COALESCE 回退）——发表日期早于报告日、
    # 但入库日期晚于报告日的文章，在历史重放中同样排除（规范 §3.6/§5）
    since_date = (
        datetime.strptime(date_str, "%Y-%m-%d") - timedelta(days=pool_window_days)
    ).strftime("%Y-%m-%d")
    conn = db._conn() if db._memory_conn is None else db._memory_conn
    rows = conn.execute(
        f"SELECT id, doi, title, journal, authors, pub_date, url, abstract, "
        f"relevance, relevance_reason, topic, analysis, evidence_level, created_at, title_zh "
        f"FROM (SELECT *, {_pool_date_expr()} AS _pool_date FROM articles "
        f"WHERE COALESCE(relevance, 0) >= ? AND title IS NOT NULL AND trim(title) != '') "
        f"WHERE _pool_date >= ? AND _pool_date <= ? AND _pool_date != '' "
        f"AND substr(COALESCE(created_at, ''), 1, 10) <= ? "
        f"ORDER BY _pool_date DESC, relevance DESC, id ASC "
        f"LIMIT {POOL_HARD_LIMIT}",
        (min_score, since_date, date_str, date_str),
    ).fetchall()
    cols = [
        "id", "doi", "title", "journal", "authors", "pub_date", "url", "abstract",
        "relevance", "relevance_reason", "topic", "analysis", "evidence_level",
        "created_at", "title_zh",
    ]
    articles = [dict(zip(cols, r)) for r in rows]

    for a in articles:
        m = metrics.get((a.get("journal") or "").strip()) or {}
        a["cas_zone"] = m.get("cas_zone")
        a["impact_factor"] = m.get("if_value")

    scored = []
    for a in articles:
        s = score_article(a, now=now, metrics_by_name=metrics, top_journals=top_journals)
        item = dict(a)
        item["scores"] = s
        scored.append(item)

    excluded: set = set()
    if window_days > 0:
        since = (datetime.strptime(date_str, "%Y-%m-%d") - timedelta(days=window_days)).strftime("%Y-%m-%d")
        # 防重集合 = 旧 digest_entries ∪ 新已发布版本条目（去重）；
        # 读取失败向上抛出——历史读取失败不得默认当成空历史（规范 §4.2）
        excluded.update(
            db.list_published_digest_article_ids_since("daily", since, date_str)
        )

    selection = select_daily_top(
        scored, limit=limit, category_limits=category_limits, excluded_ids=excluded
    )

    if not dry_run:
        db.save_digest_entries(
            digest_date=date_str,
            digest_type="daily",
            items=[
                {
                    "article_id": row["id"],
                    "rank": i + 1,
                    "category": (row.get("scores") or {}).get("category"),
                    "relevance_score": (row.get("scores") or {}).get("relevance"),
                    "final_score": (row.get("scores") or {}).get("final"),
                    "selected_reason": _brief_reason(row),
                }
                for i, row in enumerate(selection.selected)
            ],
        )

    return {
        "date": date_str,
        "dry_run": dry_run,
        "min_score": min_score,
        "limit": limit,
        "repeat_window_days": window_days,
        "pool_window_days": pool_window_days,
        "category_limits": category_limits,
        "articles_above_threshold": len(articles),
        "excluded_ids": excluded,
        "selection": selection,
    }


def _brief_reason(row: dict) -> str:
    s = row.get("scores") or {}
    parts = [f"final={s.get('final')}", f"rel={s.get('relevance')}", f"cat={s.get('category')}"]
    if (s.get("freshness") or 0) >= 8:
        parts.append("recent")
    return " ".join(parts)


def format_dry_run_report(result: dict[str, Any], max_reject_notes: int = 12) -> str:
    sel: SelectionResult = result["selection"]
    lines = [
        f"Daily Digest Dry Run — {result['date']}",
        "=" * 48,
        f"Articles above threshold (≥{result['min_score']}):  {result['articles_above_threshold']}",
        f"Candidate pool window:             {result.get('pool_window_days', '?')} days",
        f"Excluded by {result['repeat_window_days']}-day repeat: {sel.stats.get('excluded_repeat', 0)}",
        f"Eligible pool:                     {sel.stats.get('pool', 0)}",
        f"Selected:                          {sel.stats.get('selected', 0)}",
        f"By category:                       {sel.stats.get('by_category')}",
        "",
        "Selected",
        "-" * 48,
    ]
    for i, row in enumerate(sel.selected, 1):
        s = row.get("scores") or {}
        title = (row.get("title") or "")[:70]
        lines.append(f"#{i} [{s.get('category')}] {s.get('final')}")
        lines.append(f"   {title}")
        lines.append(
            f"   relevance={s.get('relevance')} freshness={s.get('freshness')} "
            f"category={s.get('category_bonus')} journal={s.get('journal_bonus')}"
        )
        lines.append(f"   {row.get('journal') or '-'} | {row.get('pub_date') or '-'}")
        lines.append("")

    lines.append("Not selected (sample)")
    lines.append("-" * 48)
    for item in sel.rejected[:max_reject_notes]:
        a = item.get("article") or {}
        s = item.get("scores") or item.get("article", {}).get("scores") or {}
        lines.append(f"- {(a.get('title') or '')[:60]}")
        lines.append(f"  score={s.get('final')} cat={s.get('category')} reason={item.get('reason')}")
    if len(sel.rejected) > max_reject_notes:
        lines.append(f"... and {len(sel.rejected) - max_reject_notes} more")
    return "\n".join(lines)
