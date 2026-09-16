"""arXiv API access: proactive pacing, shared cooldown, capped backoff.

arXiv's Terms of Use ask clients to "make no more than one request every three
seconds, and limit requests to a single connection at a time"; exceeding that
gets the client throttled or blocked rather than handed a usable Retry-After.
So this module paces requests *before* they are sent (like lukasschwab/arxiv.py
does with delay_seconds) instead of only reacting to 429, reuses one Session,
and still caps every wait so a throttled source cannot stall collection.
"""
import email.utils
import logging
import random
import re
import threading
import time
from datetime import datetime, timezone
from typing import Any, Optional

import feedparser
import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

# 现代编号 2601.12984（可选版本号）与旧式编号 math.GT/0309136、cond-mat/0309136
_NEW_ID_RE = re.compile(r"(?:arxiv[:\s]*)?(?:abs/|pdf/)?(\d{4}\.\d{4,5})(?:v\d+)?", re.I)
_OLD_ID_RE = re.compile(r"(?:arxiv[:\s]*)?(?:abs/|pdf/)?([a-z-]+(?:\.[A-Za-z]{2})?/\d{7})(?:v\d+)?", re.I)
_ARXIV_QUERY_URL = "https://export.arxiv.org/api/query?id_list="

_lock = threading.Lock()
_cooldown_lock = threading.Lock()
_cooldown_until = 0.0
_last_request_at: Optional[float] = None
_session = requests.Session()
# arXiv ToU: no more than one request every three seconds, one connection.
MIN_INTERVAL_SECONDS = 3.0
# Raw Retry-After values can be enormous; waiting the raw value would serialize
# the whole pipeline, so every sleep and the shared cooldown share this cap.
MAX_COOLDOWN_SECONDS = 60.0
# Actual sleeps stay short: if the server demands more, fail fast instead of
# stalling the serial source group (cooldown still gates sibling sources).
_SLEEP_CAP_SECONDS = 60.0


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


def configure(config: Optional[dict[str, Any]] = None) -> None:
    """Read the optional pacing override (`fetcher.arxiv_min_interval_seconds`)."""
    global MIN_INTERVAL_SECONDS
    value = ((config or {}).get("fetcher") or {}).get("arxiv_min_interval_seconds")
    try:
        MIN_INTERVAL_SECONDS = max(0.0, float(value)) if value is not None else 3.0
    except (TypeError, ValueError):
        MIN_INTERVAL_SECONDS = 3.0


def _wait_pacing() -> None:
    """Keep at least MIN_INTERVAL_SECONDS between requests (arXiv ToU)."""
    global _last_request_at
    with _cooldown_lock:
        now = time.monotonic()
        wait = 0.0
        if _last_request_at is not None and MIN_INTERVAL_SECONDS > 0:
            wait = max(0.0, MIN_INTERVAL_SECONDS - (now - _last_request_at))
    if wait:
        logger.debug("arXiv 节流等待 %.2fs（ToU 最小间隔）", wait)
        time.sleep(wait)
    with _cooldown_lock:
        _last_request_at = time.monotonic()


def _set_cooldown(seconds: float) -> None:
    global _cooldown_until
    with _cooldown_lock:
        _cooldown_until = max(_cooldown_until, time.monotonic() + min(seconds, MAX_COOLDOWN_SECONDS))


def _wait_cooldown() -> None:
    with _cooldown_lock:
        wait = max(0.0, _cooldown_until - time.monotonic())
    if wait:
        time.sleep(wait)


def retry_wait_seconds(seconds: float) -> float:
    """Any caller sleeps this capped value, never the raw Retry-After."""
    return max(0.0, min(float(seconds), _SLEEP_CAP_SECONDS))


def is_cooling_down() -> bool:
    """True while a sibling request's 429 cooldown still applies."""
    with _cooldown_lock:
        return time.monotonic() < _cooldown_until


def fetch_rss(category: str, *, timeout: int = 30) -> Any:
    """Fetch one category's RSS announcement feed.

    rss.arxiv.org serves a static per-category file (new/replace/cross-listed
    announcements), so it is not subject to the query API's 3-second rule and
    keeps working while the query API returns 429.
    """
    url = f"https://rss.arxiv.org/rss/{category}"
    last_error: Optional[Exception] = None
    for attempt in range(2):
        try:
            with _lock:
                with _session.get(url, timeout=timeout) as response:
                    response.raise_for_status()
                    return feedparser.parse(response.content)
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            logger.warning("arXiv RSS 请求失败 (%s/%s): %s", attempt + 1, 2, exc)
            if attempt == 0:
                time.sleep(1.0)
    raise RuntimeError(f"arXiv RSS 请求失败 ({category})") from last_error


def fetch_feed(url: str, *, timeout: int = 30) -> Any:
    """GET an arXiv API URL and return a parsed feed, honoring 429 backoff."""
    last_error: Optional[Exception] = None
    for attempt in range(3):
        retry_wait = 0.0
        with _lock:
            _wait_cooldown()
            _wait_pacing()
            try:
                with _session.get(url, timeout=timeout) as response:
                    if response.status_code == 429:
                        retry_wait = (_retry_after(response.headers.get("Retry-After"))
                                      or 2 ** (attempt + 1) + random.uniform(0, 1))
                        _set_cooldown(retry_wait)
                        last_error = RuntimeError(f"arXiv HTTP 429 rate_limited attempt {attempt + 1}")
                    else:
                        response.raise_for_status()
                        return feedparser.parse(response.content)
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                if not isinstance(exc, RuntimeError) or "rate_limited" not in str(exc):
                    retry_wait = 2 * (attempt + 1)
        if attempt < 2 and retry_wait:
            time.sleep(retry_wait_seconds(retry_wait) + random.uniform(0, 1))
    raise RuntimeError(f"arXiv 请求重试耗尽") from last_error


def parse_id(text: str) -> str:
    """从 URL / `arXiv:编号` / 裸编号中取出 arXiv 编号；不是 arXiv 引用返回空串。

    只保留编号本身（丢弃版本号），查询时取该论文的最新版本；
    旧式编号（math.GT/0309136）同样支持。
    """
    text = (text or "").strip()
    if not text:
        return ""
    # arXiv 的 DataCite DOI（10.48550/arXiv.2601.12984）等价于 arXiv 引用
    datacite = re.search(r"10\.48550/arxiv\.([A-Za-z0-9._/-]+)", text, re.I)
    if datacite:
        return parse_id(datacite.group(1))
    # 其他 DOI 优先判定：10.xxxx/yyyy.zzzz 里的数字片段会被编号正则误匹配
    if re.match(r"^(?:https?://(?:dx\.)?doi\.org/)?10\.\d{4,}/", text, re.I):
        return ""
    for pattern in (_NEW_ID_RE, _OLD_ID_RE):
        match = pattern.search(text)
        if match:
            return match.group(1)
    return ""


def _clean(text: Any) -> str:
    return re.sub(r"\s+", " ", BeautifulSoup(str(text or ""), "html.parser").get_text()).strip()


def _normalize_entry(entry: Any) -> dict[str, Any]:
    """把 arXiv Atom 条目规范化为工作台文章字段（摘要不是全文）。"""
    title = _clean(entry.get("title", ""))
    if not title or title.lower().startswith("error"):
        return {}
    published = (entry.get("published") or entry.get("updated") or "").strip()
    pub_date = published[:10] if len(published) >= 10 else ""
    primary = ((entry.get("arxiv_primary_category") or {}).get("term") or "").strip()
    return {
        "title": title,
        # 与采集流程一致：arXiv 没有期刊名，用主分类占位
        "journal": primary or "arXiv",
        "url": entry.get("link") or "",
        "doi": (entry.get("arxiv_doi") or "").strip(),
        "abstract": _clean(entry.get("summary", "")),
        "authors": [a.get("name", "") for a in entry.get("authors", []) if a.get("name")],
        "pub_date": pub_date,
        "date_source": "arxiv_id_lookup",
        "arxiv_id": parse_id(entry.get("id", "")) or "",
        "has_fulltext": False,
    }


def lookup_by_id(arxiv_id: str, *, timeout: int = 30) -> Optional[dict[str, Any]]:
    """按 arXiv 编号取单篇元数据（标题/作者/摘要/分类/日期）；查不到返回 None。

    arXiv 预印本通常没有 DOI，按 DOI 查 OpenAlex/Crossref 一律落空，
    这里走 arXiv 官方 API 的 id_list 查询（沿用同一套节流与冷却）。
    """
    identifier = parse_id(arxiv_id)
    if not identifier:
        return None
    feed = fetch_feed(_ARXIV_QUERY_URL + identifier, timeout=timeout)
    entries = list(getattr(feed, "entries", []) or [])
    if not entries:
        return None
    article = _normalize_entry(entries[0])
    return article or None

