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
import threading
import time
from datetime import datetime, timezone
from typing import Any, Optional

import feedparser
import requests

logger = logging.getLogger(__name__)

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
