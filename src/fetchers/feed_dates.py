"""Publication dates from feeds, without guessing dates from update timestamps."""
from datetime import date, datetime, timedelta
from email.utils import parsedate_to_datetime
import re
from urllib.parse import urlparse

from lxml import etree


_RDF = "http://www.w3.org/1999/02/22-rdf-syntax-ns#"
_RSS = "http://purl.org/rss/1.0/"
_DC = "http://purl.org/dc/elements/1.1/"
# An XML element cannot forge this provenance marker on a feedparser entry.
_NATURE_DC_DATE = object()
_CALENDAR_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}(?:$|[Tt ])")
_RFC_DATE = re.compile(
    r"^(?:(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)[,\s]+)?"
    r"\d{1,2}\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d{4}\s+"
    r"\d{2}:\d{2}(?::\d{2})?\s+(?:[+-]\d{4}|UT|UTC|GMT|[ECMP][SD]T)"
    r"(?:\s+\([^()]*\))?$", re.IGNORECASE,
)


def _official_nature_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
        return (parsed.scheme in {"http", "https"}
                and parsed.hostname in {"nature.com", "www.nature.com", "feeds.nature.com"}
                and parsed.username is None and parsed.password is None)
    except (TypeError, ValueError):
        return False


def annotate_nature_dates(feed, content: bytes, rss_url: str, response_url: str) -> None:
    """Attach exact item dc:date values only for official Nature RSS 1.0/RDF.

    feedparser merges dc:date, Atom updated and dcterms:modified into updated.
    Neither that field nor a feed-wide namespace declaration proves provenance.
    Match XML items by identity, not position, and decline ambiguous matches.
    """
    if (feed.get("version") != "rss10" or not _official_nature_url(rss_url)
            or not _official_nature_url(response_url)):
        return
    try:
        parser = etree.XMLParser(resolve_entities=False, load_dtd=False, no_network=True)
        root = etree.fromstring(content, parser=parser)
    except (etree.XMLSyntaxError, ValueError, TypeError):
        return
    if root.tag != f"{{{_RDF}}}RDF" or root.getroottree().docinfo.doctype:
        return

    identities: dict[str, set[int]] = {}
    dates: dict[int, str] = {}
    for index, item in enumerate(root.findall(f"{{{_RSS}}}item")):
        keys = {item.get(f"{{{_RDF}}}about", "").strip(),
                (item.findtext(f"{{{_RSS}}}link") or "").strip()}
        for key in keys - {""}:
            identities.setdefault(key, set()).add(index)
        nodes = item.findall(f"{{{_DC}}}date")
        if len(nodes) == 1 and len(nodes[0]) == 0:
            dates[index] = (nodes[0].text or "").strip()

    for entry in feed.entries:
        matches: set[int] = set()
        for field in ("id", "link"):
            matches.update(identities.get(entry.get(field, ""), set()))
        if len(matches) == 1:
            index = next(iter(matches))
            if index in dates:
                entry[_NATURE_DC_DATE] = dates[index]


def _raw_date(value: str) -> str:
    """Validate before conversion; retain bad/partial input for admission_decision.

    In particular, never use feedparser's repaired day for a month-only or
    impossible date. Date-only values stay date-only, and timezone-free times
    stay local to the configured admission timezone.
    """
    raw = str(value).strip()
    try:
        if _CALENDAR_DATE.match(raw):
            date.fromisoformat(raw[:10])
            if len(raw) == 10:
                return raw
            return datetime.fromisoformat(raw.replace("Z", "+00:00")).isoformat()
        if _RFC_DATE.fullmatch(raw):
            return parsedate_to_datetime(raw).isoformat()
    except (ValueError, TypeError, OverflowError):
        pass
    return raw


def publication_date(entry) -> tuple[str, str]:
    """Select publication evidence; genuine published always has precedence.

    Keep the parsed-only compatibility path for existing callers/fixtures, but
    never let it repair a present raw value. Untagged updated dates are ignored.
    An empty published field still counts as present evidence (do not fall back
    to parsed/updated); admission will mark it needs_date.
    The caller uses admission_decision for timezone, future and window policy.
    """
    if "published" in entry:
        return _raw_date(entry.get("published") or ""), "rss_published"
    parsed = entry.get("published_parsed")
    if parsed:
        try:
            # feedparser's parsed timestamps are UTC.
            return datetime(*parsed[:6]).isoformat() + "Z", "rss_published"
        except (TypeError, ValueError, OverflowError):
            return "", "rss_published"
    if _NATURE_DC_DATE in entry:
        return _raw_date(entry[_NATURE_DC_DATE]), "rss_dc_date"
    return "", "missing"


def unfetched_entries_may_be_in_window(entries, per_max: int, window_date: date,
                                       date_filter_days: int = 3) -> bool:
    """顶层 RSS 只取最新 N 条；是否有条目落在准入窗口内但未被抓取。

    用"超出单源上限的条目里是否存在窗口内条目"判断是否截断，而不是拿
    讯息流的全部历史存量（含窗口外旧条目）去比上限——大存量的 "most-recent"
    流会因此被误报为截断，让每次任务都显示"来源不完整"。
    日期缺失或无法解析的条目保守视为可能落在窗口内（无法证明完整）。
    """
    if len(entries) <= per_max:
        return False
    window_start = window_date - timedelta(days=max(0, date_filter_days - 1))
    for entry in entries[per_max:]:
        raw, _source = publication_date(entry)
        if not raw:
            return True
        try:
            entry_date = date.fromisoformat(raw[:10])
        except ValueError:
            return True
        if window_start <= entry_date <= window_date:
            return True
    return False
