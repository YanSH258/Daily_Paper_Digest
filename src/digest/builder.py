"""从数据库构建每日 Top-N，并支持 dry-run 解释输出。"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Optional

from .scorer import score_article
from .selector import DEFAULT_CATEGORY_LIMITS, SelectionResult, normalize_limits, select_daily_top


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
    digest_cfg = (config.get("digest") or {}).get("daily") or {}
    limit = int(digest_cfg.get("limit") or 10)
    window_days = int(digest_cfg.get("repeat_window_days") or 30)
    category_limits = normalize_limits(
        digest_cfg.get("category_limits") or digest_cfg.get("quotas")
    )
    min_score = float(config.get("relevance_threshold") or 5)
    top_journals = ((config.get("digest") or {}).get("tracks") or {}).get("top_chemistry", {}).get("journals")

    metrics = _load_metrics(db)
    now = datetime.strptime(date_str, "%Y-%m-%d") + timedelta(hours=12)

    # 候选池：已评分且达到阈值（不限今日，Phase 0 从全库选）
    conn = db._conn() if db._memory_conn is None else db._memory_conn
    rows = conn.execute(
        "SELECT id, doi, title, journal, authors, pub_date, url, abstract, "
        "relevance, relevance_reason, topic, analysis, evidence_level, created_at, title_zh "
        "FROM articles "
        "WHERE COALESCE(relevance, 0) >= ? "
        "AND title IS NOT NULL AND trim(title) != '' "
        "ORDER BY relevance DESC, created_at DESC "
        "LIMIT 2000",
        (min_score,),
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

    excluded = set()
    if window_days > 0:
        try:
            since = (datetime.strptime(date_str, "%Y-%m-%d") - timedelta(days=window_days)).strftime("%Y-%m-%d")
            for r in conn.execute(
                "SELECT article_id FROM digest_entries "
                "WHERE digest_type = 'daily' AND digest_date >= ? AND digest_date < ?",
                (since, date_str),
            ):
                excluded.add(r[0])
        except Exception:  # noqa: BLE001 - 表不存在时视为无历史
            pass

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
