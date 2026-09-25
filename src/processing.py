"""Pure admission and deterministic processing queue policy."""
from __future__ import annotations
from datetime import datetime, date, timezone, timedelta
from zoneinfo import ZoneInfo
from collections import defaultdict
from typing import Any
DEFAULT_TIMEZONE = "Asia/Shanghai"

def _tz(config=None, timezone_name=None):
    name = timezone_name or (config or {}).get("scheduler", {}).get("timezone", DEFAULT_TIMEZONE)
    try: return ZoneInfo(name or DEFAULT_TIMEZONE)
    except Exception as exc: raise ValueError(f"invalid timezone: {name}") from exc

def _publication(value, tz):
    if value is None or str(value).strip() == "": return None
    text = str(value).strip()
    if len(text) in (4, 7): return "partial"
    try:
        if len(text) == 10: return date.fromisoformat(text)
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if dt.tzinfo is None: dt = dt.replace(tzinfo=tz)
        return dt.astimezone(tz).date()
    except (ValueError, TypeError): return "invalid"


def task_status_and_error(stats):
    """由运行统计计算任务状态与面向用户的可读错误摘要。

    状态口径：failed = 日报失败 / 批次熔断 / 全部评分尝试失败；
    partial = 存在真实步骤失败（评分、解读、来源、库写入、投递或日报部分失败）；
    success = 其余情况，包括"来源达到单源条数上限"这类常态截断（下轮续采）。
    错误摘要给出可操作的失败类型与样例，不再落到无信息的兜底文案。
    """
    stats = stats or {}
    hard_failure = bool(
        stats.get("digest_overall_status") == "failed"
        or stats.get("score_abort_error")
        or (stats.get("score_attempted", 0) > 0
            and stats.get("scored_ok", 0) == 0
            and stats.get("scored_failed", 0) >= stats.get("score_attempted", 0))
    )
    push_results = stats.get("push_results") or {}
    push_failed = any(not bool(v) for v in push_results.values())
    partial = bool(not hard_failure and (
        stats.get("db_errors") or stats.get("scored_failed")
        or stats.get("analyzed_failed") or stats.get("source_failed")
        or push_failed or stats.get("digest_overall_status") == "partial"
    ))
    status = "failed" if hard_failure else "partial" if partial else "success"
    error_msg = ""
    if status != "success":
        reasons = []
        if stats.get("score_abort_error"):
            reasons.append(str(stats["score_abort_error"]))
        if stats.get("score_errors"):
            summaries = []
            for kind, detail in stats["score_errors"].items():
                count = detail.get("count", 0) if isinstance(detail, dict) else detail
                samples = detail.get("samples", []) if isinstance(detail, dict) else []
                sample_text = f"（如：{'；'.join(samples)}）" if samples else ""
                summaries.append(f"{kind}×{count}{sample_text}")
            reasons.append("；".join(summaries))
        if stats.get("source_failed"):
            failed_sources = [s for s in stats.get("sources", []) if not s.get("success")]
            names = [s.get("name") or f"来源#{s.get('source_id')}" for s in failed_sources][:3]
            reasons.append(f"来源抓取失败×{stats['source_failed']}（如：{'；'.join(names)}）")
        if stats.get("source_incomplete"):
            incomplete = [s for s in stats.get("sources", [])
                          if s.get("success") and not s.get("complete")]
            names = [s.get("name") or f"来源#{s.get('source_id')}" for s in incomplete][:3]
            reasons.append(
                f"来源未抓全×{stats['source_incomplete']}"
                f"（如：{'；'.join(names)}，窗口内条目可能超过单源上限，下轮自动续采）")
        if stats.get("db_errors"):
            reasons.append(f"数据库写入失败×{stats['db_errors']}")
        if stats.get("analyzed_failed"):
            reasons.append(f"解读失败×{stats['analyzed_failed']}")
        if stats.get("scored_failed") and not stats.get("score_errors"):
            reasons.append(f"评分失败×{stats['scored_failed']}")
        if push_failed:
            reasons.append("部分渠道投递失败")
        reasons.extend(str(e.get("message", "日报失败"))
                       for e in stats.get("digest_errors", []))
        error_msg = "；".join(str(r) for r in reasons if r) or "任务部分步骤失败"
    return status, error_msg

def admission_decision(article, run_date, config=None):
    cfg=config or {}; tz=_tz(cfg); target=date.fromisoformat(str(run_date)) if not isinstance(run_date,date) else run_date
    days=int(cfg.get("fetcher",{}).get("date_filter_days",cfg.get("scheduler",{}).get("days",3)))
    if days<1: raise ValueError("days must be >= 1")
    start=target-timedelta(days=days-1); end=target
    source=article.get("date_source") or article.get("pub_date_source") or "unknown"
    raw=article.get("pub_date") or article.get("published_at") or article.get("date")
    pub=_publication(raw,tz)
    base={"date_source":source,"window_start":start.isoformat(),"window_end":end.isoformat()}
    if raw is None or str(raw).strip()=="" or pub=="partial": return {**base,"decision":"needs_date","reason":"missing_or_partial_date","publication_date":None}
    if pub=="invalid": return {**base,"decision":"invalid_date","reason":"invalid_date","publication_date":None}
    base["publication_date"]=pub.isoformat()
    if pub>end: return {**base,"decision":"invalid_date","reason":"future_date"}
    if pub<start: return {**base,"decision":"outside_window","reason":"outside_window"}
    return {**base,"decision":"eligible","reason":"within_window"}

def validate_budget(config=None, trial=False):
    p=(config or {}).get("processing",{})
    key="trial_max_score_articles" if trial else "max_score_articles_per_run"
    default=30 if trial else 100; value=p.get(key,default)
    if isinstance(value, bool) or isinstance(value, float) and not value.is_integer():
        raise ValueError(f"{key} must be a positive integer")
    try: value=int(value)
    except (TypeError,ValueError): raise ValueError(f"{key} must be a positive integer")
    if value<1: raise ValueError(f"{key} must be a positive integer")
    return value

ABSTRACT_RESCORE_ERROR = "abstract_rescore_failed: "


def is_manual_score(article):
    return (article.get("score_model") == "manual" or article.get("score_basis") == "manual"
            or article.get("discovered_via") == "manual")


def needs_abstract_rescore(article):
    return (not is_manual_score(article)
            and article.get("score_status") in ("ok", "success")
            and article.get("score_basis") == "title"
            and bool(str(article.get("abstract") or "").strip()))


def is_score_retry(article):
    return (article.get("score_status") == "failed"
            or str(article.get("score_error") or "").startswith(ABSTRACT_RESCORE_ERROR))


def scoring_candidates(articles, run_date, config=None):
    eligible = []
    for article in articles:
        if is_manual_score(article):
            continue
        if article.get("processing_status") not in (None, "", "unreviewed", "eligible"):
            continue
        rescore = needs_abstract_rescore(article)
        if not rescore and (article.get("score_status") in ("ok", "success") or article.get("processed")):
            continue
        row = dict(article)
        # Already-admitted unscored backlog may drain after its original date window;
        # a successful title score only reopens while the paper is still current.
        if rescore or row.get("processing_status") != "eligible":
            decision = admission_decision(row, run_date, config)
            if decision["decision"] != "eligible":
                continue
            row.update(processing_reason=decision["reason"], date_source=decision["date_source"])
        row["processing_status"] = "eligible"
        eligible.append(row)
    return eligible


def discovered_metadata_updates(existing, discovery):
    """Fill missing source metadata after an exact identity match; keep user fields intact."""
    if is_manual_score(existing):
        return {}
    updates = {}
    abstract = str(discovery.get("abstract") or "").strip()
    if not str(existing.get("abstract") or "").strip() and abstract:
        updates["abstract"] = abstract
    decision = discovery.get("_admission") or {}
    old_date = str(existing.get("pub_date") or "").strip()
    if (existing.get("processing_status") == "needs_date" and (not old_date or len(old_date) in (4, 7))
            and decision.get("decision") == "eligible" and decision.get("publication_date")
            and decision.get("date_source") not in (None, "", "unknown", "missing")):
        updates.update(pub_date=decision["publication_date"], date_source=decision["date_source"],
                       processing_status="eligible", processing_reason="source_date_completed")
    return updates


def build_queue(articles, config=None, budget=None):
    budget=validate_budget(config,False) if budget is None else int(budget)
    groups=defaultdict(list); seen=set()
    rows=sorted(articles,key=lambda x:(x.get("queued_at") or x.get("created_at") or "",x.get("id",0)))
    for row in rows:
        aid=row.get("id")
        if aid in seen: continue
        if row.get("processing_status") not in {"eligible","queued","processing","failed"}: continue
        seen.add(aid); source=str(row.get("journal") or row.get("journal_id") or row.get("source_id") or row.get("discovered_via") or "unknown")
        groups[source].append(row)
    out=[]; failures=0; failure_budget=max(1,budget//4)
    while groups and len(out)<budget:
        for source in sorted(list(groups)):
            if not groups[source]: del groups[source]; continue
            row=groups[source].pop(0); retry=is_score_retry(row)
            if retry and failures>=failure_budget: continue
            if retry: failures+=1
            out.append(row)
            if len(out)>=budget: break
    return out
