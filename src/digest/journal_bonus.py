"""期刊加分（0–10，轻权重）。优先中科院分区，其次 IF 近似。"""
from __future__ import annotations

import math
from typing import Any, Optional

_ZONE_MAP = {1: 10.0, 2: 7.0, 3: 4.0, 4: 2.0}


def journal_bonus(article: dict[str, Any],
                  metrics_by_name: Optional[dict[str, dict]] = None) -> float:
    """metrics_by_name: journal name -> {if_value, cas_zone}。"""
    journal = (article.get("journal") or "").strip()
    if not journal:
        return 3.0

    zone = article.get("cas_zone")
    if_value = article.get("impact_factor")

    if metrics_by_name and journal in metrics_by_name:
        m = metrics_by_name[journal] or {}
        if zone is None:
            zone = m.get("cas_zone")
        if if_value is None:
            if_value = m.get("if_value")

    if zone in _ZONE_MAP:
        return _ZONE_MAP[int(zone)]
    try:
        if_val = float(if_value) if if_value is not None else None
    except (TypeError, ValueError):
        if_val = None
    if if_val is not None and if_val > 0:
        # IF 1→约3，10→约7，50→约10
        return max(1.0, min(10.0, 3.0 + math.log10(if_val + 1.0) * 3.0))
    return 3.0
