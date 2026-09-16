"""
wos_client.py - Web of Science Starter API 封装（元数据检索与被引数）

用途：按 DOI/标题补全缺失元数据、拉取被引次数。
凭据：环境变量 WOS_API_KEY 或 config.wos.api_key。
说明：Starter API 支持的字段标签有限（TI/TS/AU/DO/PY/SO 等），
实测 DO 直查覆盖率不稳定，因此采用 DO 直查 + 标题回退双策略。
"""
import logging
import os
import re
from typing import Any, Optional

import requests

logger = logging.getLogger(__name__)

WOS_BASE = "https://api.clarivate.com/apis/wos-starter/v1/documents"
_TIMEOUT = 20


class WOSClient:
    def __init__(self, config: Optional[dict] = None) -> None:
        cfg = (config or {}).get("wos", {}) or {}
        self.api_key = (os.environ.get("WOS_API_KEY") or str(cfg.get("api_key") or "")).strip()
        if not self.api_key:
            raise ValueError("WOS 未配置 api_key（环境变量 WOS_API_KEY 或 config.wos.api_key）")

    def _search(self, q: str, limit: int = 5) -> list[dict[str, Any]]:
        resp = requests.get(
            WOS_BASE, params={"q": q, "limit": limit},
            headers={"X-ApiKey": self.api_key, "accept": "application/json"},
            timeout=_TIMEOUT,
        )
        if resp.status_code != 200:
            logger.warning("WOS 查询失败 HTTP %s: %s", resp.status_code, resp.text[:200])
            return []
        return resp.json().get("hits", []) or []

    @staticmethod
    def _normalize(hit: dict[str, Any]) -> dict[str, Any]:
        source = hit.get("source") or {}
        identifiers = hit.get("identifiers") or {}
        names = (hit.get("names") or {}).get("authors") or []
        citations = hit.get("citations") or []
        wos_cited = next((c.get("count") for c in citations if c.get("db") == "WOS"), None)
        return {
            "uid": hit.get("uid"),
            "doi": identifiers.get("doi") or "",
            "title": (hit.get("title") or "").strip(),
            "journal": source.get("sourceTitle") or "",
            "pub_year": source.get("publishYear") or "",
            "pub_date": source.get("publishMonth") or "",
            "volume": source.get("volume") or "",
            "issue": source.get("issue") or "",
            "pages": (source.get("pages") or {}).get("range") or "",
            "authors": [n.get("displayName", "") for n in names if n.get("displayName")],
            "cited_count": wos_cited,
        }

    def lookup_by_doi(self, doi: str) -> Optional[dict[str, Any]]:
        """按 DOI 检索；DO 直查为空时回退标题检索再按 DOI 匹配。"""
        doi = (doi or "").strip()
        if not doi:
            return None
        for q in (f'DO="{doi}"', f"DO={doi}"):
            hits = self._search(q, limit=3)
            for hit in hits:
                normalized = self._normalize(hit)
                if normalized["doi"].lower() == doi.lower():
                    return normalized
        return None

    def lookup_by_title(self, title: str) -> Optional[dict[str, Any]]:
        title = re.sub(r"\s+", " ", (title or "").strip())
        if len(title) < 15:
            return None
        hits = self._search(f'TI="{title}"', limit=3)
        return self._normalize(hits[0]) if hits else None

    def enrich_article(self, article: dict) -> dict[str, Any]:
        """补全缺失元数据 + 被引数；返回 {字段: 值}，只包含需要补的字段。"""
        doi = (article.get("doi") or "").strip()
        record = self.lookup_by_doi(doi) if doi else None
        if record is None and article.get("title"):
            record = self.lookup_by_title(article["title"])
            if record and doi and record.get("doi", "").lower() != doi.lower():
                return {}  # 标题命中但 DOI 不一致：宁缺毋滥
        if not record:
            return {}

        updates: dict[str, Any] = {}
        if record.get("cited_count") is not None:
            updates["cited_count"] = record["cited_count"]
        if not (article.get("journal") or "").strip() and record.get("journal"):
            updates["journal"] = record["journal"]
        if not (article.get("pub_date") or "").strip() and record.get("pub_year"):
            updates["pub_date"] = str(record["pub_year"])
        if not article.get("authors") and record.get("authors"):
            updates["authors"] = ", ".join(record["authors"][:20])
        return {k: v for k, v in updates.items() if v not in (None, "")}
