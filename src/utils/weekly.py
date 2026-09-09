"""
weekly.py - 周报生成

汇总一周文献数据：方向分布对比、已读/待读盘点、引文追踪摘要、
推荐质量、新热词（英文词频差分）。输出 markdown + HTML。
"""
import logging
import re
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

_TOKEN_RE = re.compile(r"[a-zA-Z][a-zA-Z\-]{3,}")
_STOP = {"the", "and", "for", "with", "that", "this", "from", "are", "was", "were",
         "have", "has", "had", "not", "but", "can", "our", "their", "which", "into",
         "such", "also", "than", "then", "these", "those", "using", "based", "between",
         "via", "using", "results", "show", "shown", "propose", "proposed", "method",
         "methods", "paper", "study", "novel", "high", "low", "new", "approach",
         "based", "model", "models", "using", "based", "toward", "towards"}


def _week_bounds(date_str: str) -> tuple[str, str, str]:
    """返回 (周一, 下周一, ISO 周标签)。"""
    d = datetime.strptime(date_str, "%Y-%m-%d")
    monday = d - timedelta(days=d.weekday())
    next_monday = monday + timedelta(days=7)
    label = f"{monday.isocalendar().year}-W{monday.isocalendar().week:02d}"
    return monday.strftime("%Y-%m-%d"), next_monday.strftime("%Y-%m-%d"), label


def _topic_dist(db, start: str, end: str) -> dict[str, int]:
    rows = db.list_articles_created_between(start, end)
    dist: dict[str, int] = {}
    for a in rows:
        topic = a.get("topic") or "未分类"
        dist[topic] = dist.get(topic, 0) + 1
    return dist


def _hot_words(db, start: str, end: str, ref_start: str, ref_end: str, top: int = 8) -> list[tuple[str, int]]:
    """本周 vs 之前四周的英文词频差分，找新热词。"""

    def tokens(rows):
        counter: Counter = Counter()
        for a in rows:
            text = (a.get("title") or "") + " " + (a.get("abstract") or "")
            counter.update(t.lower() for t in _TOKEN_RE.findall(text) if t.lower() not in _STOP)
        return counter

    cur = tokens(db.list_articles_created_between(start, end))
    prev = tokens(db.list_articles_created_between(ref_start, ref_end))
    scored = []
    for word, count in cur.items():
        if count < 3 or len(word) < 5:
            continue
        rise = count - prev.get(word, 0)
        if rise >= 2:
            scored.append((word, count))
    scored.sort(key=lambda x: -x[1])
    return scored[:top]


def build_weekly(config: dict, db, date_str: Optional[str] = None) -> tuple[str, str, str]:
    """生成周报，返回 (md_path, html_path, label)。"""
    date_str = date_str or datetime.now().strftime("%Y-%m-%d")
    start, end, label = _week_bounds(date_str)
    prev_end = start  # 上周区间 [prev_start, prev_end)
    prev_start = (datetime.strptime(start, "%Y-%m-%d") - timedelta(days=7)).strftime("%Y-%m-%d")

    articles = db.list_articles_created_between(start, end)
    for a in articles:
        if not a.get("topic"):
            from core.notifier import classify_article
            a["topic"] = classify_article(a)

    cur_dist = _topic_dist(db, start, end)
    prev_dist = _topic_dist(db, prev_start, prev_end)
    read_done = sum(1 for a in articles if a.get("read_status") == "read")
    reading = sum(1 for a in articles if a.get("read_status") == "reading")
    queued = sum(1 for a in articles if a.get("read_status") == "queued")
    relevant = [a for a in articles if (a.get("relevance") or 0) >= config.get("relevance_threshold", 5)]
    edges = db.get_citation_edges_since(days=14)

    lines: list[str] = [
        f"# 📅 文献周报 {label}",
        "",
        f"> 统计范围：{start} ~ {end}（按入库日期，本地时区）",
        f"> 本周入库 **{len(articles)}** 篇 ｜ 相关（≥阈值）**{len(relevant)}** 篇",
        f"> 阅读进度：已读 {read_done} · 在读 {reading} · 待读 {queued}",
        "",
    ]

    lines += ["## 📊 方向分布（对比上周）", "", "| 方向 | 本周 | 上周 | 变化 |", "|---|---|---|---|"]
    for topic in sorted(set(cur_dist) | set(prev_dist), key=lambda t: -cur_dist.get(t, 0)):
        c, p = cur_dist.get(topic, 0), prev_dist.get(topic, 0)
        delta = "↑" if c > p else ("↓" if c < p else "—")
        if c or p:
            lines.append(f"| {topic} | {c} | {p} | {delta} |")
    lines.append("")

    if edges:
        lines += ["## 🔗 引文追踪（近两周）", ""]
        for e in edges[:8]:
            lines.append(f"- 《{e['citing_title'][:70]}》引用了你关注的《{e['seed_title'][:50]}》"
                         + (f"（评分 {e['citing_relevance']:.0f}）" if e.get("citing_relevance") else ""))
        lines.append("")

    words = _hot_words(db, start, end, prev_start, end)
    if words:
        lines += ["## 🔥 本周新热词", "", " · ".join(f"**{w}**({n})" for w, n in words), ""]

    if relevant:
        lines += ["## ⭐ 本周重点文献", ""]
        for a in sorted(relevant, key=lambda x: -(x.get("relevance") or 0))[:10]:
            lines.append(f"- ⭐{float(a['relevance']):.1f} [{a['journal'] or '?'}] "
                         f"[{a['title'][:70]}](/article/{a['id']})")
        lines.append("")

    md = "\n".join(lines)

    from core.notifier import Notifier
    notifier = Notifier(config)
    md_path = notifier.output_dir / f"weekly-{label}.md"
    md_path.write_text(md, encoding="utf-8")
    html = notifier._build_html(md, f"文献周报 {label}")
    html_path = notifier.output_dir / f"weekly-{label}.html"
    html_path.write_text(html, encoding="utf-8")
    logger.info("周报已生成: %s", md_path)
    return str(md_path), str(html_path), label
