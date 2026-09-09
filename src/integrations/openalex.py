"""
openalex.py - OpenAlex API 封装（免费、无需 key）

职责：
- DOI → OpenAlex work 详情
- 引文追踪：某文献的新引用列表
- 作者检索与作者新文章
统一返回与库内 article dict 兼容的规范化结构，供追踪流水线直接入库。
"""
import logging
import re
import time
from typing import Any, Optional

import requests

logger = logging.getLogger(__name__)

OPENALEX_BASE = "https://api.openalex.org"
_MAILTO = "your@email.com"
_session = requests.Session()


def set_polite_email(email: str) -> None:
    """OpenAlex 礼貌池：带 mailto 可获得更高速率限制。"""
    global _MAILTO
    if email and email != "your@email.com":
        _MAILTO = email


def _get(path: str, params: dict[str, Any]) -> Optional[dict[str, Any]]:
    params = {**params, "mailto": _MAILTO}
    for attempt in range(3):
        try:
            resp = _session.get(f"{OPENALEX_BASE}{path}", params=params, timeout=20)
            if resp.status_code == 404:
                return None  # 确定性不存在，不重试
            if resp.status_code == 429:
                time.sleep(2 * (attempt + 1))
                continue
            resp.raise_for_status()
            return resp.json()
        except Exception as e:  # noqa: BLE001
            logger.warning("OpenAlex 请求失败 (%s/%s): %s", attempt + 1, 3, e)
            time.sleep(1.5 * (attempt + 1))
    return None


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


def get_work_by_doi(doi: str) -> Optional[dict[str, Any]]:
    if not doi:
        return None
    data = _get(f"/works/doi:{doi.strip()}", {})
    return _normalize_work(data) if data else None


def get_citing_works(doi: str, from_date: str = "", limit: int = 50) -> list[dict[str, Any]]:
    """获取引用了 doi 的文献列表（按发表日期倒序）。

    from_date: 只返回该日期（含）之后发表的引用，用于增量追踪。
    """
    seed = get_work_by_doi(doi)
    if not seed or not seed.get("openalex_id"):
        return []
    work_id = seed["openalex_id"]
    params: dict[str, Any] = {
        "filter": f"cites:{work_id}" + (f",from_publication_date:{from_date}" if from_date else ""),
        "sort": "publication_date:desc",
        "per-page": min(limit, 200),
    }
    data = _get("/works", params)
    if not data:
        return []
    works = [_normalize_work(w) for w in (data.get("results") or [])]
    return [w for w in works if w["title"]]


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
    params: dict[str, Any] = {
        "filter": f"author.id:{openalex_id}" + (f",from_publication_date:{from_date}" if from_date else ""),
        "sort": "publication_date:desc",
        "per-page": min(limit, 200),
    }
    data = _get("/works", params)
    if not data:
        return []
    works = [_normalize_work(w) for w in (data.get("results") or [])]
    return [w for w in works if w["title"]]


def search_works(query: str, from_date: str = "", limit: int = 50) -> list[dict[str, Any]]:
    """按检索式订阅新文献（OpenAlex default.search）。"""
    if not query:
        return []
    filt = f"default.search:{query}" + (f",from_publication_date:{from_date}" if from_date else "")
    data = _get("/works", {"filter": filt, "sort": "publication_date:desc",
                           "per-page": min(limit, 200)})
    if not data:
        return []
    works = [_normalize_work(w) for w in (data.get("results") or [])]
    return [w for w in works if w["title"]]
