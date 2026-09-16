"""来源预设导入/导出：把可审阅的订阅来源清单与工作台数据库互相转换。

设计约束：
- 只支持来源方公开提供的订阅/检索接口（rss / arxiv / openalex / crossref），
  不凭空拼接地址；每条记录带核验日期，导入前可整文件审阅；
- 导入是幂等的：以规范化 URL 为去重键，重复导入逐条报"已存在"；
- 部分失败必须逐条报告并继续处理后续条目，不得中断、不得全部显示成功；
- 预览与导入使用同一套逐条校验，坏条目（null、非对象、字段类型错误）
  记为该条失败，不影响合法条目；
- 导入只写 journals 表，不触发任何采集、评分或推送；
- 导出以数据库现有订阅为准生成同一格式，只输出来源元数据
  （名称/类型/公开地址/检索式），不写入任何密钥或个人邮箱；
  已有说明文字按规范化 URL 与原文件对齐保留，缺失的写上真实来源，
  不虚构核验记录。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote, unquote

VALID_SOURCE_TYPES = ("rss", "arxiv", "openalex", "crossref")

# 导出时新条目的说明文字：只描述该类型的接口事实，不含未经核实的结论。
_TYPE_NOTES: dict[str, tuple[str, str]] = {
    "rss": (
        "期刊/站点官方 RSS 提供的最近文章列表（条目数与更新频率由发布方决定）",
        "出版商站点可能限流或改版；抓取日期以 RSS 发布日期为准",
    ),
    "arxiv": (
        "arXiv 检索式命中的新预印本；抓取优先走 rss.arxiv.org 分类 RSS，"
        "不可用时回退查询 API",
        "预印本未经同行评审；arXiv 使用条款限制请求频率（约 3 秒 1 次），"
        "故按分类 RSS 合并抓取，宽分类会带入与计算无关的论文，依赖相关性打分过滤",
    ),
    "openalex": (
        "OpenAlex 收录的正式发表文献（含 DOI 与摘要），按 last_run 水位线增量拉取",
        "依赖 OpenAlex 收录覆盖；接口按额度计费（列表查询 10 credits/次，"
        "配置 api_key 后每天 10000 credits），无 key 时额度极低",
    ),
    "crossref": (
        "Crossref 该期刊最新登记的文献元数据（含 DOI），按期刊 ISSN 拉取",
        "Crossref 只提供元数据，通常没有摘要，需靠相关性打分与后续补全过滤",
    ),
}



def _str_field(item: dict[str, Any], key: str) -> str:
    """字段必须是字符串时取 strip 值；缺失或类型错误一律视为空。"""
    value = item.get(key)
    return value.strip() if isinstance(value, str) else ""


def canonical_url(item: dict[str, Any]) -> Optional[str]:
    """返回该来源的规范化真实 URL（同时是数据库去重键）。

    rss 用清单给出的官方订阅地址；arxiv/openalex/crossref 由检索式构造其
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
        # filter: 前缀为原生 OpenAlex filter 语法（如期刊 ISSN 订阅），直接作为 filter 参数
        if query.startswith("filter:"):
            return "https://api.openalex.org/works?" + query
        return "https://api.openalex.org/works?search=" + quote(query)
    if source_type == "crossref":
        # 检索式为期刊 ISSN，落库地址与 fetcher 使用的一致
        return "https://api.crossref.org/journals/" + quote(query, safe="") + "/works"
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
        "publisher": _str_field(src, "publisher"),
        "max_articles": 100,
        "reason": "",
    }
    try:
        entry["max_articles"] = max(1, min(500, int(src.get("max_articles") or 100)))
    except (TypeError, ValueError):
        pass
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
                             publisher=entry.get("publisher") or "DEFAULT",
                             max_articles=entry.get("max_articles") or 100,
                             source=source,
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


def _query_from_url(source_type: str, url: str) -> str:
    """从落库的真实抓取地址还原检索式（query 为空时的兜底）。"""
    if not url.lower().startswith(("http://", "https://")):
        return ""
    if source_type == "arxiv":
        from urllib.parse import parse_qs, urlparse
        qs = parse_qs(urlparse(url).query)
        return (qs.get("search_query") or [""])[0].strip()
    if source_type == "openalex":
        prefix = "https://api.openalex.org/works?"
        if url.startswith(prefix):
            return unquote(url[len(prefix):]).strip()
        return ""
    if source_type == "crossref":
        prefix = "https://api.crossref.org/journals/"
        suffix = "/works"
        if url.startswith(prefix) and url.endswith(suffix):
            return unquote(url[len(prefix):-len(suffix)]).strip()
        return ""
    return ""


def _row_to_entry(row: dict[str, Any]) -> Optional[dict[str, Any]]:
    """把 journals 表一行转成可导入的预设条目；无法表达时返回 None。"""
    source_type = _str_field(row, "source_type").lower() or "rss"
    if source_type not in VALID_SOURCE_TYPES:
        return None
    entry: dict[str, Any] = {
        "name": _str_field(row, "name"),
        "source_type": source_type,
        "publisher": _str_field(row, "publisher") or "DEFAULT",
    }
    try:
        entry["max_articles"] = max(1, min(500, int(row.get("max_articles") or 100)))
    except (TypeError, ValueError):
        entry["max_articles"] = 100
    if not entry["name"]:
        return None
    if source_type == "rss":
        url = _str_field(row, "rss")
        if not url.lower().startswith(("http://", "https://")):
            return None
        entry["url"] = url
    else:
        # query 列为准；历史记录可能只写了地址，再从地址还原检索式
        query = _str_field(row, "query") or _query_from_url(source_type, _str_field(row, "rss"))
        if not query:
            return None
        entry["query"] = query
    return entry if canonical_url(entry) else None


def build_preset(
    db,
    *,
    previous: Optional[dict[str, Any]] = None,
    include_disabled: bool = False,
    exported_at: str = "",
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """以数据库现有订阅为准生成预设结构。

    返回 (preset, skipped)：skipped 是无法表达为可导入地址而被略过的来源。
    说明文字按规范化 URL 与原文件对齐保留，缺失的写该类型的接口事实；
    不虚构核验记录——没有历史核验的来源写明导出出处。
    """
    rows = db.list_journals(enabled_only=not include_disabled)
    prev_by_url: dict[str, dict[str, Any]] = {}
    if isinstance(previous, dict):
        for src in previous.get("sources") or []:
            url = canonical_url(src)
            if url:
                prev_by_url.setdefault(url, src)

    sources: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for row in rows:
        entry = _row_to_entry(row)
        if entry is None:
            skipped.append({"name": _str_field(row, "name") or f"id={row.get('id')}",
                            "reason": "没有可导入的公开地址或检索式"})
            continue
        old = prev_by_url.get(canonical_url(entry) or "", {})
        default_coverage, default_limitations = _TYPE_NOTES.get(entry["source_type"], ("", ""))
        sources.append({
            **entry,
            "coverage": _str_field(old, "coverage") or default_coverage,
            "limitations": _str_field(old, "limitations") or default_limitations,
            "verified": _str_field(old, "verified")
            or f"未经单独核验：{exported_at or '本次导出'}取自工作台已启用订阅（该源在采集流程中实际使用）",
        })

    disabled_count = 0
    if not include_disabled:
        disabled_count = sum(1 for j in db.list_journals() if not j.get("enabled"))

    notes = [
        "本文件由工作台数据库的订阅清单导出（daily-paper-digest --export-sources），"
        "只包含来源元数据：名称、类型、公开订阅地址或检索式，不含任何密钥与个人邮箱。",
        "抓取方式：rss 直连官方订阅地址；arxiv 优先走 rss.arxiv.org 分类 RSS，"
        "不可用时回退查询 API（arXiv 使用条款限制约 3 秒 1 次请求）；"
        "openalex 走期刊 ISSN 过滤或关键词检索，按额度计费；crossref 按期刊 ISSN 取最新登记文献。",
        f"默认只导出已启用来源；本次未导出停用来源 {disabled_count} 条。"
        "导入命令：daily-paper-digest --import-sources <本文件>（加 --dry-run 只预览不导入）；"
        "去重键是规范化抓取地址，地址完全相同的来源不会重复导入。",
        "每条 verified 是该来源最近一次实际核验的记录与日期；"
        "未做过单独核验的来源会明确标注“未经单独核验”并写明导出出处，不会伪装成已核验。",
    ]
    preset = {
        "name": _str_field(previous or {}, "name") or "computational_materials",
        "title": "计算材料/化学方向来源清单（工作台导出）",
        "description": (
            f"由工作台订阅清单导出的来源集，共 {len(sources)} 条："
            "期刊 RSS、OpenAlex 期刊/关键词检索、arXiv 分类与检索式、Crossref 期刊订阅。"
            "每条都带抓取方式与已知限制说明。"
        ),
        "version": "2.0",
        "exported_at": exported_at,
        "notes": notes,
        "sources": sources,
    }
    return preset, skipped


def export_preset(
    db,
    path: str | Path,
    *,
    previous_path: Optional[str | Path] = None,
    include_disabled: bool = False,
    exported_at: str = "",
) -> dict[str, Any]:
    """把数据库订阅导出为预设文件（写入前先读旧文件保留说明文字）。

    只写来源元数据，不写密钥、邮箱或抓取结果；不触发采集/评分/推送。
    """
    target = Path(path)
    src = Path(previous_path) if previous_path else target
    previous: Optional[dict[str, Any]] = None
    if src.exists():
        try:
            previous = load_preset(src)
        except (ValueError, json.JSONDecodeError):
            previous = None
    preset, skipped = build_preset(db, previous=previous,
                                   include_disabled=include_disabled,
                                   exported_at=exported_at)
    target.write_text(json.dumps(preset, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {"preset": preset["name"], "path": str(target),
            "count": len(preset["sources"]), "skipped": skipped}
