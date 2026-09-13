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
            row=groups[source].pop(0); status=row.get("score_status")
            if status=="failed" and failures>=failure_budget: continue
            if status=="failed": failures+=1
            out.append(row)
            if len(out)>=budget: break
    return out
