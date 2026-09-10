"""软配额选择器（min/max 两阶段）：纯函数。

Stage A：按 final 降序满足各 category 的 minimum 覆盖。
Stage B：剩余位置全局按 final 补位，且不超过 category maximum。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

# 第一阶段默认：保底核心领域，other 不设 min、最多 2 篇
DEFAULT_CATEGORY_LIMITS = {
    "mlip": {"min": 2, "max": 4},
    "ai_materials": {"min": 1, "max": 3},
    "dft": {"min": 1, "max": 3},
    "llm_science": {"min": 0, "max": 2},
    "top_chemistry": {"min": 0, "max": 3},
    "other": {"min": 0, "max": 2},
}

# 兼容旧 quotas 写法：mlip: 3 → 视为 max=3, min=min(3, 默认 min)
_LEGACY_DEFAULT_MIN = {
    "mlip": 2,
    "ai_materials": 1,
    "dft": 1,
    "llm_science": 0,
    "top_chemistry": 0,
    "other": 0,
}


def normalize_limits(raw: Any) -> dict[str, dict[str, int]]:
    """接受 {cat: {min,max}} 或旧 {cat: max_n}。"""
    limits: dict[str, dict[str, int]] = {k: dict(v) for k, v in DEFAULT_CATEGORY_LIMITS.items()}
    if not raw:
        return limits
    if isinstance(raw, dict):
        for cat, val in raw.items():
            if isinstance(val, dict):
                limits[cat] = {
                    "min": int(val.get("min", 0)),
                    "max": int(val.get("max", 10)),
                }
            else:
                n = int(val)
                limits[cat] = {"min": _LEGACY_DEFAULT_MIN.get(cat, 0), "max": n}
    return limits


@dataclass
class SelectionResult:
    selected: list[dict] = field(default_factory=list)
    rejected: list[dict] = field(default_factory=list)  # {article, scores, reason}
    stats: dict = field(default_factory=dict)


def select_daily_top(
    scored: list[dict[str, Any]],
    *,
    limit: int = 10,
    quotas: Optional[dict] = None,
    category_limits: Optional[dict] = None,
    excluded_ids: Optional[set] = None,
) -> SelectionResult:
    """scored: [{id, title, scores:{category, final, ...}}, ...]

    quotas 为兼容旧接口：映射为 max；min 用默认保底。
    """
    limits = normalize_limits(category_limits if category_limits is not None else quotas)
    excluded = excluded_ids or set()

    pool: list[dict] = []
    rejected: list[dict] = []
    for row in scored:
        rid = row.get("id")
        if rid is not None and rid in excluded:
            rejected.append({
                "article": row,
                "scores": row.get("scores") or {},
                "reason": "appeared in daily digest within repeat window",
            })
            continue
        pool.append(row)

    pool.sort(key=lambda x: -float((x.get("scores") or {}).get("final") or 0))

    selected_ids: set = set()
    selected: list[dict] = []
    cat_count: dict[str, int] = {k: 0 for k in limits}

    def _cat(row: dict) -> str:
        c = (row.get("scores") or {}).get("category") or "other"
        return c if c in limits else "other"

    def _try_add(row: dict, *, phase: str) -> bool:
        if len(selected) >= limit:
            return False
        rid = row.get("id")
        if rid in selected_ids:
            return False
        cat = _cat(row)
        if cat_count.get(cat, 0) >= limits[cat]["max"]:
            return False
        selected.append(row)
        selected_ids.add(rid)
        cat_count[cat] = cat_count.get(cat, 0) + 1
        return True

    # Stage A：minimum coverage（按 final 降序，每类取到 min 为止）
    for cat, cfg in limits.items():
        need = int(cfg.get("min") or 0)
        if need <= 0:
            continue
        for row in pool:
            if cat_count.get(cat, 0) >= need:
                break
            if _cat(row) != cat:
                continue
            _try_add(row, phase="min")

    # Stage B：全局补位，受 max 约束
    for row in pool:
        if len(selected) >= limit:
            break
        if not _try_add(row, phase="fill"):
            continue

    # 按 final_score 降序展示/入库
    selected.sort(key=lambda x: -float((x.get("scores") or {}).get("final") or 0))

    # 落选说明
    for row in pool:
        if row.get("id") in selected_ids:
            continue
        cat = _cat(row)
        if cat_count.get(cat, 0) >= limits[cat]["max"]:
            reason = f"{cat} max={limits[cat]['max']} already filled"
        elif len(selected) >= limit:
            reason = "ranked below global top-N cutoff"
        else:
            reason = "not selected"
        rejected.append({
            "article": row,
            "scores": row.get("scores") or {},
            "reason": reason,
        })

    stats = {
        "pool": len(pool),
        "selected": len(selected),
        "excluded_repeat": sum(
            1 for r in rejected if "repeat window" in str(r.get("reason", ""))
        ),
        "by_category": cat_count,
        "limits": limits,
    }
    return SelectionResult(selected=selected, rejected=rejected, stats=stats)
