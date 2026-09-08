"""
citation.py - 文献引用导出（BibTeX / RIS）

输入为数据库文章记录（dict）：title / authors（逗号分隔字符串或列表）/
journal / pub_date / doi / url / relevance。
"""
import re
from typing import Any


def _authors_list(a: dict[str, Any]) -> list[str]:
    authors = a.get("authors") or ""
    if isinstance(authors, list):
        return [x.strip() for x in authors if str(x).strip()]
    return [x.strip() for x in str(authors).split(",") if x.strip()]


def _year(a: dict[str, Any]) -> str:
    for src in (a.get("pub_date"), a.get("created_at")):
        m = re.search(r"(\d{4})", str(src or ""))
        if m:
            return m.group(1)
    return "n.d."


def _cite_key(a: dict[str, Any]) -> str:
    authors = _authors_list(a)
    first = re.sub(r"[^A-Za-z]", "", (authors[0].split()[-1] if authors else "anon")) or "anon"
    year = _year(a)
    title_words = re.findall(r"[A-Za-z]+", a.get("title") or "")[:2]
    return f"{first.lower()}{year}{''.join(w.lower() for w in title_words)}"


def format_bibtex(a: dict[str, Any]) -> str:
    key = _cite_key(a)
    authors = " and ".join(_authors_list(a)) or "Unknown"
    lines = [
        f"@article{{{key},",
        f"  title = {{{a.get('title') or 'Untitled'}}},",
        f"  author = {{{authors}}},",
        f"  journal = {{{a.get('journal') or ''}}},",
        f"  year = {{{_year(a)}}},",
    ]
    if a.get("doi"):
        lines.append(f"  doi = {{{a['doi']}}},")
    if a.get("url"):
        lines.append(f"  url = {{{a['url']}}},")
    lines.append("}")
    return "\n".join(lines)


def format_ris(a: dict[str, Any]) -> str:
    lines = ["TY  - JOUR", f"TI  - {a.get('title') or 'Untitled'}"]
    for author in _authors_list(a):
        lines.append(f"AU  - {author}")
    if a.get("journal"):
        lines.append(f"JO  - {a['journal']}")
    year = _year(a)
    if year != "n.d.":
        lines.append(f"PY  - {year}")
    if a.get("doi"):
        lines.append(f"DO  - {a['doi']}")
    if a.get("url"):
        lines.append(f"UR  - {a['url']}")
    lines.append("ER  - ")
    return "\n".join(lines)


def format_article_citation(a: dict[str, Any], fmt: str = "bibtex") -> str:
    fmt = (fmt or "bibtex").lower()
    if fmt == "ris":
        return format_ris(a)
    if fmt == "bibtex":
        return format_bibtex(a)
    raise ValueError(f"不支持的引用格式: {fmt}")
