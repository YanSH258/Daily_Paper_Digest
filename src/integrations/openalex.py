"""
openalex.py - OpenAlex API 封装（免费、无需 key）

职责：
- DOI → OpenAlex work 详情
- 引文追踪：某文献的新引用列表
- 作者检索与作者新文章
统一返回与库内 article dict 兼容的规范化结构，供追踪流水线直接入库。
"""
import email.utils
import logging
import random
import re
import threading
import time
from datetime import datetime, timezone
from typing import Any, Optional

import requests

from core.rate_limiter import get_domain_limiter

logger = logging.getLogger(__name__)

OPENALEX_BASE = "https://api.openalex.org"
OPENALEX_URL = f"{OPENALEX_BASE}/works"
_MAILTO = "your@email.com"
_API_KEY = ""
_session = requests.Session()
_request_config: dict[str, Any] = {}
_openalex_lock = threading.Lock()
_cooldown_lock = threading.Lock()
_cooldown_until = 0.0
# A throttled client may receive hours-long Retry-After values; sleeping the raw
# value serializes the whole pipeline. Shared cooldown caps are always applied.
MAX_COOLDOWN_SECONDS = 60.0
# Actual sleeps stay short: if the server demands more, fail fast instead of
# stalling the serial source group (cooldown still gates sibling sources).
_SLEEP_CAP_SECONDS = 60.0


def set_polite_email(email: str) -> None:
    """OpenAlex 礼貌池：带 mailto 可获得更高速率限制。"""
    global _MAILTO
    if email and email != "your@email.com":
        _MAILTO = email


def set_api_key(api_key: str) -> None:
    """OpenAlex 可选 API key：官方文档称非必需，但付费/更高额度账号需要。"""
    global _API_KEY
    _API_KEY = (api_key or "").strip()


def configure(config: Optional[dict[str, Any]] = None) -> None:
    """设置 OpenAlex 共享限速与认证所需的运行时配置。"""
    global _request_config
    _request_config = config or {}
    set_api_key((_request_config.get("openalex") or {}).get("api_key") or "")


def _retry_after(value: Optional[str]) -> Optional[float]:
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        try:
            when = email.utils.parsedate_to_datetime(value)
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
            return max(0.0, (when - datetime.now(timezone.utc)).total_seconds())
        except (TypeError, ValueError, OverflowError):
            return None


def _set_cooldown(seconds: float) -> None:
    """Record the earliest next-allowed request, capped to stay responsive."""
    global _cooldown_until
    with _cooldown_lock:
        _cooldown_until = max(_cooldown_until, time.monotonic() + min(seconds, MAX_COOLDOWN_SECONDS))


def _wait_cooldown() -> None:
    with _cooldown_lock:
        wait = max(0.0, _cooldown_until - time.monotonic())
    if wait:
        time.sleep(wait)


def retry_wait_seconds(seconds: float) -> float:
    """The wait any caller may sleep for, always capped like the cooldown."""
    return max(0.0, min(float(seconds), _SLEEP_CAP_SECONDS))


def is_cooling_down() -> bool:
    """True while another caller's 429 cooldown still applies (batch fast-fail)."""
    with _cooldown_lock:
        return time.monotonic() < _cooldown_until


def _get(path: str, params: dict[str, Any], *, timeout: int = 20) -> Optional[dict[str, Any]]:
    """请求 OpenAlex；共享限速、串行门控并尊重 429 Retry-After（封顶等待）。"""
    params = {**params, "mailto": _MAILTO}
    if _API_KEY:
        params["api_key"] = _API_KEY
    last_error: Exception | None = None
    limiter = get_domain_limiter(_request_config)
    for attempt in range(3):
        retry_wait = 0.0
        with _openalex_lock:
            _wait_cooldown()
            if not limiter.acquire(OPENALEX_URL, timeout=60):
                raise RuntimeError(f"OpenAlex rate_limited: limiter timeout ({path})")
            try:
                resp = _session.get(f"{OPENALEX_BASE}{path}", params=params, timeout=timeout)
                if resp.status_code == 404:
                    return None
                if resp.status_code == 429:
                    retry_wait = _retry_after(resp.headers.get("Retry-After")) or (2 ** (attempt + 1) + random.uniform(0, 1))
                    _set_cooldown(retry_wait)
                    last_error = RuntimeError(f"HTTP 429 rate_limited attempt {attempt + 1}")
                else:
                    resp.raise_for_status()
                    return resp.json()
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                if not isinstance(exc, RuntimeError) or "rate_limited" not in str(exc):
                    logger.warning("OpenAlex 请求失败 (%s/%s): %s", attempt + 1, 3, exc)
                    retry_wait = 1.5 * (attempt + 1)
        if attempt < 2 and retry_wait:
            time.sleep(retry_wait_seconds(retry_wait) + random.uniform(0, 1))
    raise RuntimeError(f"OpenAlex 请求重试耗尽 ({path}, rate_limited={isinstance(last_error, RuntimeError) and '429' in str(last_error)})") from last_error


def _clean_abstract(inverted_index: Optional[dict]) -> str:
    """OpenAlex 倒排索引摘要还原为纯文本。"""
    if not inverted_index:
        return ""
    positions: list[tuple[int, str]] = []
    for word, idxs in inverted_index.items():
        for i in idxs:
            positions.append((i, word))
    positions.sort()
    return re.sub(r"\s+", " ", " ".join(w for _, w in positions)).strip()


def _normalize_work(work: dict[str, Any]) -> dict[str, Any]:
    authorships = work.get("authorships") or []
    authors = [a.get("author", {}).get("display_name", "") for a in authorships]
    authors = [a for a in authors if a]
    primary = work.get("primary_location") or {}
    source = (primary.get("source") or {}) or {}
    pub = work.get("publication_date") or ""
    doi_url = work.get("doi") or ""
    doi = doi_url.replace("https://doi.org/", "").strip() if doi_url else ""
    return {
        "doi": doi,
        "title": re.sub(r"\s+", " ", work.get("title") or "").strip(),
        "journal": source.get("display_name") or "",
        "authors": authors,
        "pub_date": pub,
        "url": f"https://doi.org/{doi}" if doi else (primary.get("landing_page_url") or ""),
        "abstract": _clean_abstract(work.get("abstract_inverted_index")),
        "cited_count": work.get("cited_by_count"),
        "openalex_id": (work.get("id") or "").rsplit("/", 1)[-1],
    }


def _normalize_from_date(value: str) -> str:
    """OpenAlex from_publication_date 仅接受 yyyy-mm-dd。

    last_run 等水位线常带时间（2026-09-13T13:02:38），必须截断，
    否则 API 返回 400 Invalid date，整刊抓取失败。
    """
    s = (value or "").strip()
    if len(s) >= 10 and s[4] == "-" and s[7] == "-":
        return s[:10]
    return ""


def get_work_by_doi(doi: str) -> Optional[dict[str, Any]]:
    if not doi:
        return None
    data = _get(f"/works/doi:{doi.strip()}", {})
    return _normalize_work(data) if data else None


class WorkList(list):
    """List-compatible work results with a conservative completeness marker."""
    def __init__(self, rows, *, complete=True, next_cursor=None):
        super().__init__(rows)
        self.complete = complete
        self.next_cursor = next_cursor


def _work_list(data, limit):
    raw = data.get("results") or []
    meta = data.get("meta") or {}
    count = meta.get("count")
    complete = len(raw) < min(limit, 200) and not meta.get("next_cursor")
    if isinstance(count, int):
        complete = count <= len(raw)
    return WorkList([w for w in (_normalize_work(r) for r in raw) if w["title"]],
                    complete=complete, next_cursor=meta.get("next_cursor"))


def get_citing_works(doi: str, from_date: str = "", limit: int = 50) -> list[dict[str, Any]]:
    """获取引用了 doi 的文献列表（按发表日期倒序）。

    from_date: 只返回该日期（含）之后发表的引用，用于增量追踪。
    """
    seed = get_work_by_doi(doi)
    if not seed or not seed.get("openalex_id"):
        return []
    work_id = seed["openalex_id"]
    from_day = _normalize_from_date(from_date)
    params: dict[str, Any] = {
        "filter": f"cites:{work_id}" + (f",from_publication_date:{from_day}" if from_day else ""),
        "sort": "publication_date:desc",
        "per-page": min(limit, 200),
    }
    data = _get("/works", params)
    if not data:
        return []
    return _work_list(data, limit)


def search_authors(name: str, limit: int = 5) -> list[dict[str, Any]]:
    data = _get("/authors", {"search": name, "per-page": limit})
    if not data:
        return []
    out = []
    for a in (data.get("results") or []):
        last = (a.get("last_known_institutions") or [None])[0] or {}
        affil = last.get("display_name") if isinstance(last, dict) else None
        out.append({
            "openalex_id": (a.get("id") or "").rsplit("/", 1)[-1],
            "name": a.get("display_name") or "",
            "affiliation": affil or "",
            "works_count": a.get("works_count"),
            "cited_count": a.get("cited_by_count"),
        })
    return out


def get_author_recent_works(openalex_id: str, from_date: str, limit: int = 25) -> list[dict[str, Any]]:
    from_day = _normalize_from_date(from_date)
    params: dict[str, Any] = {
        "filter": f"author.id:{openalex_id}" + (f",from_publication_date:{from_day}" if from_day else ""),
        "sort": "publication_date:desc",
        "per-page": min(limit, 200),
    }
    data = _get("/works", params)
    if not data:
        return []
    return _work_list(data, limit)


def search_works_page(query: str, from_date: str = "", limit: int = 50, *, to_date: str = "",
                      cursor: str = "*", timeout: int = 20) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Return one OpenAlex page and its completeness metadata."""
    if not query:
        return [], {"complete": True, "truncated": False, "next_cursor": None, "raw_count": 0}
    filt = query.removeprefix("filter:") if query.startswith("filter:") else f"default.search:{query}"
    from_day = _normalize_from_date(from_date)
    to_day = _normalize_from_date(to_date)
    if from_day: filt += f",from_publication_date:{from_day}"
    if to_day: filt += f",to_publication_date:{to_day}"
    data = _get("/works", {"filter": filt, "sort": "publication_date:desc",
                           "per-page": min(limit, 200), "cursor": cursor}, timeout=timeout) or {}
    works = [_normalize_work(w) for w in (data.get("results") or [])]
    meta = data.get("meta") or {}
    nxt = meta.get("next_cursor")
    raw = len(data.get("results") or [])
    return [w for w in works if w["title"]], {"complete": not bool(nxt) and raw < min(limit, 200), "truncated": bool(nxt) or raw >= min(limit, 200), "next_cursor": nxt, "raw_count": raw}


def search_works(query: str, from_date: str = "", limit: int = 50) -> list[dict[str, Any]]:
    """按检索式订阅新文献（OpenAlex default.search）。

    query 以 ``filter:`` 开头时作为原生 OpenAlex filter 使用（例如期刊订阅：
    ``filter:primary_location.source.id:S111155417``），不再包裹 default.search。
    """
    if not query:
        return []
    if query.startswith("filter:"):
        filt = query.removeprefix("filter:")
    else:
        filt = f"default.search:{query}"
    from_day = _normalize_from_date(from_date)
    if from_day:
        filt += f",from_publication_date:{from_day}"
    data = _get("/works", {"filter": filt, "sort": "publication_date:desc",
                           "per-page": min(limit, 200)})
    if not data:
        return []
    return _work_list(data, limit)
