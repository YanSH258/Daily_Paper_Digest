"""Crossref journal metadata discovery and DOI lookup; abstracts are never full text."""
from datetime import date, timedelta
import re
from urllib.parse import quote

import requests
from bs4 import BeautifulSoup

_USER_AGENT = "Daily-Paper-Digest/0.1 (journal metadata reader)"


def _normalize_item(item: dict) -> dict:
    """把一条 Crossref work 规范化为工作台文章字段；摘要只做去标签，不是全文。"""
    doi = str(item.get("DOI") or "").strip().lower()
    title = " ".join(item.get("title") or [])
    published = None
    date_source = "missing"
    for field in ("published-online", "published-print", "published", "issued"):
        parts = (item.get(field) or {}).get("date-parts") or []
        if parts and len(parts[0]) == 3:
            try:
                published = date(*parts[0])
                date_source = "crossref_" + field
                break
            except (TypeError, ValueError):
                pass
    abstract = BeautifulSoup(item.get("abstract") or "", "html.parser").get_text(" ", strip=True)
    # JATS 摘要常带 <jats:title>Abstract</jats:title> 标签，去标签后会留下字面的 "Abstract"
    abstract = re.sub(r"^Abstract[:\s]+", "", abstract, flags=re.I).strip()
    return {
        "doi": doi,
        "title": BeautifulSoup(title, "html.parser").get_text(" ", strip=True),
        "journal": " ".join(item.get("container-title") or []),
        "url": "https://doi.org/" + doi if doi else "",
        "authors": ", ".join(" ".join(filter(None, (a.get("given"), a.get("family"))))
                             or a.get("name", "") for a in item.get("author", [])),
        "abstract": abstract,
        "pub_date": published.isoformat() if published else "",
        "date_source": date_source,
        "has_fulltext": False,
    }


def work_by_doi(doi, *, timeout=15):
    """按 DOI 取单篇元数据（标题/期刊/日期/作者/摘要）；Crossref 未收录返回 None。

    新发表的文献常常已进入 Crossref 但还没进 OpenAlex，所以这是按 DOI
    补全的首选回退来源。
    """
    doi = (doi or "").strip()
    if not doi:
        return None
    response = requests.get(
        "https://api.crossref.org/works/" + quote(doi, safe="/"),
        headers={"User-Agent": _USER_AGENT},
        timeout=timeout,
    )
    try:
        if response.status_code == 404:
            return None
        response.raise_for_status()
        message = (response.json() or {}).get("message") or {}
    finally:
        response.close()
    article = _normalize_item(message)
    if not article["title"]:
        return None
    article["discovered_via"] = "crossref_doi"
    return article


def journal_works_page(issn, *, limit=100, days=3, timeout=15, today=None):
    if not re.fullmatch(r"\d{4}-\d{3}[\dXx]", issn):
        raise ValueError("Crossref source query must be an ISSN")
    today = today or date.today()
    filters = ["type:journal-article", f"until-pub-date:{today.isoformat()}"]
    if days > 0:
        filters.append(f"from-pub-date:{(today - timedelta(days=max(0, days - 1))).isoformat()}")
    response = requests.get(
        f"https://api.crossref.org/journals/{issn}/works",
        params={"rows": max(1, min(int(limit), 1000)), "sort": "published",
                "order": "desc", "filter": ",".join(filters)},
        headers={"User-Agent": _USER_AGENT},
        timeout=timeout,
    )
    try:
        response.raise_for_status()
        payload = response.json()
    finally:
        response.close()
    items = payload.get("message", {}).get("items")
    if not isinstance(items, list):
        raise ValueError("Crossref response missing items")
    articles = []
    seen = set()
    for item in items:
        article = _normalize_item(item)
        if not article["doi"] or not article["title"] or article["doi"] in seen:
            continue
        seen.add(article["doi"])
        article["discovered_via"] = "crossref_journal"
        articles.append(article)
    total = (payload.get("message", {}).get("total-results"))
    return articles, total


def journal_works(issn, *, limit=100, days=3, timeout=15, today=None):
    """Backward-compatible list API."""
    articles, _ = journal_works_page(issn, limit=limit, days=days, timeout=timeout, today=today)
    return articles
