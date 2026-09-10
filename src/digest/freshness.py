"""新鲜度评分（确定性，0–10）。"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional


def _parse_date(value: Any) -> Optional[datetime]:
    if not value:
        return None
    s = str(value).strip()
    if len(s) >= 10:
        try:
            return datetime.strptime(s[:10], "%Y-%m-%d")
        except ValueError:
            pass
    for fmt in ("%Y/%m/%d", "%Y%m%d"):
        try:
            return datetime.strptime(s[:10], fmt)
        except ValueError:
            continue
    return None


def freshness_score(article: dict[str, Any], now: Optional[datetime] = None) -> float:
    """发表越新分越高。缺日期按中性 5.0。

    0 天=10，线性衰减，30 天及以后=0。
    """
    now = now or datetime.now()
    dt = _parse_date(article.get("pub_date")) or _parse_date(article.get("created_at"))
    if dt is None:
        return 5.0
    age_days = max(0.0, (now - dt).total_seconds() / 86400.0)
    score = 10.0 - age_days * 0.5
    return max(0.0, min(10.0, score))
