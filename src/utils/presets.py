"""来源预设导入：把可审阅的订阅来源清单导入工作台数据库。

设计约束：
- 只支持来源方公开提供的订阅/检索接口（rss / arxiv / openalex），
  不凭空拼接地址；每条记录带核验日期，导入前可整文件审阅；
- 导入是幂等的：以规范化 URL 为去重键，重复导入逐条报"已存在"；
- 部分失败必须逐条报告并继续处理后续条目，不得中断、不得全部显示成功；
- 预览与导入使用同一套逐条校验，坏条目（null、非对象、字段类型错误）
  记为该条失败，不影响合法条目；
- 导入只写 journals 表，不触发任何采集、评分或推送。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote

VALID_SOURCE_TYPES = ("rss", "arxiv", "openalex")


def _str_field(item: dict[str, Any], key: str) -> str:
    """字段必须是字符串时取 strip 值；缺失或类型错误一律视为空。"""
    value = item.get(key)
    return value.strip() if isinstance(value, str) else ""


def canonical_url(item: dict[str, Any]) -> Optional[str]:
    """返回该来源的规范化真实 URL（同时是数据库去重键）。

    rss 用清单给出的官方订阅地址；arxiv/openalex 由检索式构造其
    公开 API 请求地址——fetcher 实际抓取的就是这个地址。
    结构非法（非对象、类型错误、缺检索式）返回 None，由调用方逐条报告。
    """
    if not isinstance(item, dict):
        return None
    source_type = _str_field(item, "source_type").lower() or "rss"
    if source_type == "rss":
        url = _str_field(item, "url")
        return url if url.lower().startswith(("http://", "https://")) else None
    query = _str_field(item, "query")
    if not query:
        return None
    if source_type == "arxiv":
        return "https://export.arxiv.org/api/query?search_query=" + quote(query)
    if source_type == "openalex":
        return "https://api.openalex.org/works?search=" + quote(query)
    return None


def load_preset(path: str | Path) -> dict[str, Any]:
    """读取预设文件；JSON 或顶层结构非法抛 ValueError（整文件级错误）。"""
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or not isinstance(raw.get("sources"), list):
        raise ValueError("预设文件缺少 sources 列表")
    return raw


def _entry_label(src: Any, index: int) -> str:
    """坏条目的可读标识：优先用其中的字符串 name，否则按位置报。"""
    if isinstance(src, dict):
        name = _str_field(src, "name")
        if name:
            return name
    return f"(第 {index + 1} 条)"


def _normalize_entry(src: Any, index: int) -> dict[str, Any]:
    """把一条来源记录规范化为统一条目；任何结构问题记为 ok=False，
    绝不抛异常中断整批处理。"""
    if not isinstance(src, dict):
        return {"ok": False, "name": _entry_label(src, index),
                "source_type": "", "query": "", "canonical_url": None,
                "coverage": "", "limitations": "", "verified": "",
                "reason": "条目不是对象（null 或类型错误）"}
    name = _str_field(src, "name")
    source_type = _str_field(src, "source_type").lower() or "rss"
    query = _str_field(src, "query")
    url = canonical_url(src)
    entry = {
        "ok": True,
        "name": name,
        "source_type": source_type,
        "query": query,
        "canonical_url": url,
        "coverage": _str_field(src, "coverage"),
        "limitations": _str_field(src, "limitations"),
        "verified": _str_field(src, "verified"),
        "reason": "",
    }
    if not name:
        entry.update(ok=False, name=_entry_label(src, index),
                     reason="缺少来源名称")
    elif source_type not in VALID_SOURCE_TYPES:
        entry.update(ok=False, reason=f"不支持的来源类型: {source_type}")
    elif not url:
        entry.update(ok=False, reason="缺少有效地址或检索式")
    return entry


def _entries(path: str | Path) -> list[dict[str, Any]]:
    preset = load_preset(path)
    return [_normalize_entry(src, i) for i, src in enumerate(preset["sources"])]


def preview_preset(path: str | Path) -> list[dict[str, Any]]:
    """返回导入前的人类可读预览（不触碰数据库）。

    坏条目同样出现在结果里（ok=False + reason），导入前即可发现。
    """
    return _entries(path)


def import_preset(path: str | Path, db, source: str = "preset") -> dict[str, Any]:
    """把预设导入数据库，逐条报告结果并继续处理后续条目；不触发采集。

    返回 {preset, added: [...], skipped: [...]}，skipped 带原因。
    """
    preset = load_preset(path)
    added: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for entry in _entries(path):
        if not entry["ok"]:
            skipped.append({"name": entry["name"], "reason": entry["reason"]})
            continue
        if db.get_journal_by_rss(entry["canonical_url"]) is not None:
            skipped.append({"name": entry["name"], "reason": "已存在",
                            "url": entry["canonical_url"]})
            continue
        jid = db.add_journal(name=entry["name"], rss=entry["canonical_url"],
                             publisher="DEFAULT", source=source,
                             source_type=entry["source_type"], query=entry["query"])
        if jid is None:
            skipped.append({"name": entry["name"], "reason": "保存失败",
                            "url": entry["canonical_url"]})
        else:
            added.append({"id": jid, "name": entry["name"],
                          "source_type": entry["source_type"],
                          "url": entry["canonical_url"], "query": entry["query"]})
    return {"preset": preset.get("name") if isinstance(preset.get("name"), str)
            else Path(path).stem, "added": added, "skipped": skipped}
