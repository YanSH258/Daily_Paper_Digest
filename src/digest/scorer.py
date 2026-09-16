"""Phase 0 综合评分：0.55 relevance + 0.25 freshness + 0.15 category + 0.05 journal。"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from .categories import classify_digest_category
from .freshness import freshness_score
from .journal_bonus import journal_bonus

# 类别加分：与 soft target 相关方向给更高 bonus
CATEGORY_BONUS = {
    "mlip": 10.0,
    "ai_materials": 9.0,
    "dft": 8.0,
    "llm_science": 8.0,
    "top_chemistry": 7.5,
    "other": 4.0,
}

WEIGHTS = {
    "relevance": 0.55,
    "freshness": 0.25,
    "category": 0.15,
    "journal": 0.05,
}


def score_article(
    article: dict[str, Any],
    *,
    now: Optional[datetime] = None,
    metrics_by_name: Optional[dict[str, dict]] = None,
    top_journals: Optional[list] = None,
) -> dict[str, Any]:
    """返回打分明细，不修改入参。"""
    relevance = float(article.get("relevance") or 0.0)
    relevance = max(0.0, min(10.0, relevance))
    cat = classify_digest_category(article, top_journals=top_journals)
    fresh = freshness_score(article, now=now)
    jbonus = journal_bonus(article, metrics_by_name=metrics_by_name)
    cbonus = CATEGORY_BONUS.get(cat, 4.0)
    final = (
        WEIGHTS["relevance"] * relevance
        + WEIGHTS["freshness"] * fresh
        + WEIGHTS["category"] * cbonus
        + WEIGHTS["journal"] * jbonus
    )
    return {
        "category": cat,
        "relevance": round(relevance, 3),
        "freshness": round(fresh, 3),
        "category_bonus": cbonus,
        "journal_bonus": round(jbonus, 3),
        "final": round(final, 4),
    }
