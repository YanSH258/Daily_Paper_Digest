"""digest 配置归一化与校验：唯一出口，供构建器与校验器共同使用。

设计约束（docs/DIGEST_RELEASE_SPEC.md §4）：
- 0 是合法值（repeat_window_days=0 表示关闭防重），禁止 `value or default` 吞 0；
- 严格整数：布尔、非整浮点、数字字符串一律拒绝（bool 是 int 子类，int(True)=1
  会被静默放行；int(3.7)=3 会被截断）；
- 类别配额校验针对**合并默认配额后**的最终结果——选择器实际使用的就是合并值；
  旧 quotas 写法（{cat: max}）会给部分类别带默认 min，可能与用户 max 冲突；
- 未知类别直接拒绝（没有文章会落入该类别，min 永远无法满足）；
- 缺失键取默认值；显式非法值报错，不静默改默认。
"""
from __future__ import annotations

import math
from typing import Any

from .selector import DEFAULT_CATEGORY_LIMITS, normalize_limits

# digest.daily 各字段的默认值与下限（唯一出处：构建器与校验器都从这里取）
DAILY_DEFAULTS: dict[str, int] = {
    "limit": 10,
    "repeat_window_days": 30,
    "pool_window_days": 60,
}
DAILY_MIN: dict[str, int] = {
    "limit": 1,
    "repeat_window_days": 0,
    "pool_window_days": 1,
}

DEFAULT_MIN_SCORE = 5.0


def _strict_int(errors: list[str], path: str, value: Any) -> int | None:
    """严格整数：int 直接接受；整值浮点（3.0）接受；
    布尔、非整浮点（3.5）、无穷/NaN、字符串一律拒绝。
    """
    if isinstance(value, bool):
        errors.append(f"{path} 必须是整数，不接受布尔值（当前为 {value!r}）")
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        # 有限性保护：inf/nan 一律拒绝。inf.is_integer()/nan.is_integer() 返回
        # False（不抛异常），真正会抛 OverflowError 的是后续 int(inf) 之类的
        # 转换——在此显式拒绝，语义更清晰也免去对下游转换的依赖
        if not math.isfinite(value) or not value.is_integer():
            errors.append(f"{path} 必须是整数，当前为 {value!r}")
            return None
        return int(value)
    errors.append(f"{path} 必须是整数，当前为 {value!r}")
    return None


def collect_daily_config(config: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """归一化 digest 配置：返回 (values, errors)。

    values 键：limit / repeat_window_days / pool_window_days / category_limits /
    min_score。errors 非空表示配置非法，调用方不得使用本次结果继续发布流程。
    """
    errors: list[str] = []
    digest_cfg = config.get("digest")
    if digest_cfg is not None and not isinstance(digest_cfg, dict):
        return {}, [f"digest 必须是映射，当前为 {type(digest_cfg).__name__}"]
    daily = (digest_cfg or {}).get("daily")
    if daily is not None and not isinstance(daily, dict):
        return {}, [f"digest.daily 必须是映射，当前为 {type(daily).__name__}"]
    daily = daily or {}

    values: dict[str, Any] = {}
    invalid: set[str] = set()
    for key, default in DAILY_DEFAULTS.items():
        raw = daily.get(key)
        if raw is None:
            values[key] = default
            continue
        n = _strict_int(errors, f"digest.daily.{key}", raw)
        if n is None or n < DAILY_MIN[key]:
            if n is not None:
                errors.append(f"digest.daily.{key} 必须 ≥ {DAILY_MIN[key]}，当前为 {n}")
            invalid.add(key)
            values[key] = default  # 后续交叉校验跳过该字段
            continue
        values[key] = n

    if not ({"repeat_window_days", "pool_window_days"} & invalid):
        if values["pool_window_days"] < values["repeat_window_days"]:
            errors.append(
                f"digest.daily.pool_window_days({values['pool_window_days']}) 不得小于 "
                f"digest.daily.repeat_window_days({values['repeat_window_days']})，"
                f"否则防重窗口外的文章进不了候选池"
            )

    # 相关性阈值：顶层配置，数字，0..10
    threshold = config.get("relevance_threshold")
    if threshold is None:
        values["min_score"] = DEFAULT_MIN_SCORE
    elif isinstance(threshold, bool):
        errors.append(f"relevance_threshold 必须是数字，不接受布尔值（当前为 {threshold!r}）")
        values["min_score"] = DEFAULT_MIN_SCORE
    elif isinstance(threshold, (int, float)):
        t = float(threshold)
        if not 0 <= t <= 10:
            errors.append(f"relevance_threshold 必须在 0..10，当前为 {t}")
        values["min_score"] = t
    else:
        errors.append(f"relevance_threshold 必须是数字，当前为 {threshold!r}")
        values["min_score"] = DEFAULT_MIN_SCORE

    # 类别配额：键存在性决定 category_limits/quotas 优先级（category_limits 优先），
    # 不用 `or` 选择——否则显式 false / [] 等假值会被静默吞成默认配额。
    # 类型先查、再归一化：所有配额值（含旧标量）一律过 _strict_int。
    if "category_limits" in daily:
        raw_limits = daily.get("category_limits")
        limits_path = "digest.daily.category_limits"
    elif "quotas" in daily:
        raw_limits = daily.get("quotas")
        limits_path = "digest.daily.quotas"
    else:
        raw_limits = None
        limits_path = "digest.daily.category_limits"

    quota_values_ok = True
    if raw_limits is not None and not isinstance(raw_limits, dict):
        errors.append(f"{limits_path} 必须是映射，当前为 {type(raw_limits).__name__}")
        quota_values_ok = False
    elif isinstance(raw_limits, dict):
        for cat, val in raw_limits.items():
            path = f"{limits_path}.{cat}"
            if cat not in DEFAULT_CATEGORY_LIMITS:
                errors.append(
                    f"{path}: 未知类别「{cat}」，可用：{', '.join(sorted(DEFAULT_CATEGORY_LIMITS))}"
                )
            if isinstance(val, dict):
                if _strict_int(errors, f"{path}.min", val.get("min", 0)) is None:
                    quota_values_ok = False
                if _strict_int(errors, f"{path}.max", val.get("max", 10)) is None:
                    quota_values_ok = False
            else:
                # 旧标量写法（{cat: max}）同样走严格整数：浮点截断、无穷外溢都在此拦下
                if _strict_int(errors, path, val) is None:
                    quota_values_ok = False

    # 最终归一化配额校验：合并默认后的结果才是选择器实际使用的。
    # 完整检查 0 ≤ min ≤ max 与 Σmin ≤ limit。
    # 值非法时 normalize_limits 会抛错——此时错误已逐项记录，回退默认配额即可
    if quota_values_ok:
        values["category_limits"] = normalize_limits(raw_limits)
        for cat, m in values["category_limits"].items():
            mn, mx = int(m.get("min", 0)), int(m.get("max", 10))
            if mn < 0:
                errors.append(f"digest.daily.category_limits.{cat}: 合并后 min({mn}) < 0")
            if mx < 0:
                errors.append(f"digest.daily.category_limits.{cat}: 合并后 max({mx}) < 0")
            if mn > mx:
                errors.append(
                    f"digest.daily.category_limits.{cat}: 合并默认配额后 min({mn}) > max({mx})"
                )
        min_sum = sum(int(m.get("min", 0)) for m in values["category_limits"].values())
        if "limit" not in invalid and min_sum > values["limit"]:
            errors.append(
                f"合并默认配额后各类别 min 之和({min_sum}) 超过 "
                f"digest.daily.limit({values['limit']})，保底配额无法满足"
            )
    else:
        values["category_limits"] = normalize_limits(None)

    return values, errors


def validate_digest_config(config: dict[str, Any]) -> list[str]:
    """校验 digest 相关配置，返回错误列表；空列表表示合法。"""
    _, errors = collect_daily_config(config)
    return errors
