"""digest - Phase 0：从已评分文献中选出每日 Top-N。

设计原则：
- 选择器为纯函数，便于单元测试
- 不新增 LLM 调用；分类/新鲜度/期刊加分为确定性规则
- digest_entries 记录历史，实现 N 日防重
"""
from .categories import DIGEST_CATEGORIES, classify_digest_category
from .freshness import freshness_score
from .journal_bonus import journal_bonus
from .scorer import score_article
from .selector import DEFAULT_CATEGORY_LIMITS, SelectionResult, select_daily_top
from .builder import build_daily_digest, format_dry_run_report

__all__ = [
    "DIGEST_CATEGORIES",
    "classify_digest_category",
    "freshness_score",
    "journal_bonus",
    "score_article",
    "DEFAULT_CATEGORY_LIMITS",
    "SelectionResult",
    "select_daily_top",
    "build_daily_digest",
    "format_dry_run_report",
]
