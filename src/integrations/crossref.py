"""Crossref journal metadata discovery; abstracts are never full text."""
from datetime import date, timedelta
import re

import requests
from bs4 import BeautifulSoup


def journal_works(issn, *, limit=100, days=3, timeout=15, today=None):
    if not re.fullmatch(r"\d{4}-\d{3}[\dXx]", issn):
        raise ValueError("Crossref source query must be an ISSN")
    today = today or date.today()
    filters = ["type:journal-article", f"until-pub-date:{today.isoformat()}"]
    if days > 0:
        filters.append(f"from-pub-date:{(today - timedelta(days=days)).isoformat()}")
    response = requests.get(
        f"https://api.crossref.org/journals/{issn}/works",
        params={"rows": max(1, min(int(limit), 1000)), "sort": "published",
                "order": "desc", "filter": ",".join(filters)},
        headers={"User-Agent": "Daily-Paper-Digest/0.1 (journal metadata reader)"},
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
        doi = str(item.get("DOI") or "").strip().lower()
        title = " ".join(item.get("title") or [])
        if not doi or not title or doi in seen:
            continue
        dates = []
        for field in ("published-online", "published-print", "published", "issued"):
            parts = (item.get(field) or {}).get("date-parts") or []
            if parts and len(parts[0]) == 3:
                try:
                    dates.append(date(*parts[0]))
                except (TypeError, ValueError):
                    pass
        # Prefer the earliest actual publication (online often precedes issue date).
        published = min(dates) if dates else None
        if published and (published > today or (days > 0 and published < today - timedelta(days=days))):
            continue
        seen.add(doi)
        articles.append({
            "doi": doi, "title": BeautifulSoup(title, "html.parser").get_text(" ", strip=True),
            "journal": " ".join(item.get("container-title") or []),
            "url": "https://doi.org/" + doi,
            "authors": ", ".join(" ".join(filter(None, (a.get("given"), a.get("family"))))
                                 or a.get("name", "") for a in item.get("author", [])),
            "abstract": BeautifulSoup(item.get("abstract") or "", "html.parser").get_text(" ", strip=True),
            "pub_date": published.isoformat() if published else "",
            "has_fulltext": False, "discovered_via": "crossref_journal",
        })
    return articles
