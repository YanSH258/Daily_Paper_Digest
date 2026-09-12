"""
mcp_server.py - Daily Paper Digest 的 MCP 服务器

把文献工作台的 HTTP API 包装成语义化工具，供 AI agent（ZCode / Codex /
Claude Desktop / Cursor 等任何 MCP 客户端）调用。

设计原则：
- 薄层：只做协议转换与上下文裁剪，业务规则全部留在 web_server/流水线
- 上下文卫生：大字段（全文）默认不返回；分析文本截断，避免撑爆模型上下文
- 成本诚实：LLM 高成本工具在 docstring 中声明"仅在用户明确要求时调用"

配置（环境变量）：
- DPD_API   工作台地址，默认 http://127.0.0.1:8080
- DPD_TOKEN 写操作 Token（服务端开启 web.api_token / protect_read 时必填）
- DPD_TIMEOUT 单请求超时秒数，默认 120（LLM 生成类工具耗时较长）

运行：`daily-paper-mcp`（stdio 传输，由 MCP 客户端拉起）
"""
from __future__ import annotations

import json
import os
import sys
from typing import Any, Optional

import requests

API_BASE = ""
TOKEN = ""
TIMEOUT = 120


def _api() -> str:
    return (os.environ.get("DPD_API") or "http://127.0.0.1:8080").rstrip("/")


def _headers() -> dict[str, str]:
    h = {"Content-Type": "application/json"}
    token = os.environ.get("DPD_TOKEN") or ""
    if token:
        h["X-API-Token"] = token
    return h


class ApiError(RuntimeError):
    """工作台 API 调用失败（含状态码与消息）。"""


def _call(method: str, path: str, body: Optional[dict] = None,
          params: Optional[dict] = None, timeout: Optional[int] = None) -> Any:
    url = _api() + path
    try:
        resp = requests.request(method, url, json=body, params=params,
                                headers=_headers(), timeout=timeout or int(os.environ.get("DPD_TIMEOUT") or TIMEOUT))
    except requests.exceptions.ConnectionError as e:
        raise ApiError(
            f"无法连接文献工作台 {_api()}（{e.__class__.__name__}）。"
            f"请先启动服务：daily-paper-web --config config/config.yaml"
        ) from e
    except requests.exceptions.Timeout as e:
        raise ApiError(f"工作台请求超时: {path}") from e
    if resp.status_code >= 400:
        detail = ""
        try:
            detail = resp.json().get("error") or resp.json().get("message") or ""
        except Exception:  # noqa: BLE001
            detail = resp.text[:200]
        raise ApiError(f"HTTP {resp.status_code} {path}: {detail}")
    if not resp.content:
        return {}
    return resp.json()


def _trim_analysis(item: dict, keep: int = 400) -> dict:
    """搜索结果里的 analysis/abstract 只保留开头（真实摘要可达 3000 字），
    完整内容用 get_paper 获取，避免批量检索撑爆模型上下文。"""
    a = item.get("analysis")
    if a and len(a) > keep:
        item["analysis_preview"] = a[:keep] + "…"
    item.pop("analysis", None)
    abstract = item.get("abstract")
    if abstract and len(abstract) > 240:
        item["abstract"] = abstract[:240] + "…"
    return item


def _build_server():
    from mcp.server.fastmcp import FastMCP

    mcp = FastMCP(
        "daily-papers",
        instructions=(
            "个人文献工作台（化学/材料方向）：RSS/引文/作者追踪 → LLM 评分 → 全文 → "
            "AI 解读 → 版本化日报。阅读工作流：preview_daily_digest 看某日 Top-N → "
            "get_paper 深入 → set_reading_status 入清单 → push_to_zotero 归档。"
            "正式日报：get_digest_history 选版本 → get_digest 看快照；"
            "发布用 publish_daily_digest（写操作，当日已有版本会幂等复用）。"
        ),
    )

    # ── 检索与阅读 ──────────────────────────────────────────────

    @mcp.tool()
    def search_papers(
        query: str = "",
        topic: str = "",
        journal: str = "",
        min_score: float = 0,
        read_status: str = "",
        starred: bool = False,
        analyzed_only: bool = False,
        limit: int = 20,
        offset: int = 0,
    ) -> dict:
        """检索文献库。

        Args:
            query: 关键词（匹配标题/摘要/作者/期刊）
            topic: 按研究方向筛选（如 "机器学习势函数 / MLIP"）
            read_status: 阅读状态筛选：queued(待读)/reading(在读)/read(已读)，空为全部
            starred: 仅看收藏
            analyzed_only: 仅看有 AI 解读的
        Returns:
            {total, items: [{id, title, journal, relevance, relevance_reason, topic,
                             read_status, evidence_level, starred, analysis_preview}]}
        """
        data = _call("GET", "/api/articles", params={
            "q": query, "topic": topic, "journal": journal,
            "min_score": min_score, "read_status": read_status,
            "starred": "true" if starred else "false",
            "analyzed_only": "true" if analyzed_only else "false",
            "limit": max(1, min(int(limit), 50)), "offset": int(offset),
        })
        for it in data.get("items", []):
            _trim_analysis(it)
        return data

    @mcp.tool()
    def get_paper(paper_id: int, include_fulltext: bool = False) -> dict:
        """获取单篇文献完整详情（元数据、评分理由、AI 解读、我的笔记/标签）。

        Args:
            include_fulltext: 是否包含全文文本（可能很长，默认不返回；
                              仅摘要文献无全文）
        """
        item = _call("GET", f"/api/articles/{int(paper_id)}")
        if not include_fulltext:
            item.pop("fulltext_text", None)
        else:
            ft = item.get("fulltext_text") or ""
            if len(ft) > 30000:
                item["fulltext_text"] = ft[:30000] + f"\n…[截断，原文 {len(ft)} 字]"
        return item

    @mcp.tool()
    def today_top_n(date: str = "", commit: bool = False) -> dict:
        """今日 Top-N 精选：综合相关性/新鲜度/类别/期刊加权的选文结果。

        Args:
            date: 日期（YYYY-MM-DD，默认今天）
            commit: False=只预览不落盘（推荐）；True=仅落盘旧版选择历史
                （legacy，不创建版本、不发送；正式发布请用 publish_daily_digest）
        Returns:
            {date, articles_above_threshold, selected: [{rank, id, title,
             scores, relevance_reason, title_zh}], report_text, deprecated?}
        """
        if commit:
            return _call("POST", "/api/digest", body={"date": date} if date else {})
        return _call("GET", "/api/digest", params={"date": date} if date else None)

    # ── 版本化日报（只读）──────────────────────────────────────

    @mcp.tool()
    def preview_daily_digest(date: str = "") -> dict:
        """按版本化规则只读预览某日 Top-N（不写库、不发布、不发送）。

        Args:
            date: 报告日期（YYYY-MM-DD，默认今天）
        Returns:
            {date, articles_above_threshold, excluded_repeat, selected_count,
             by_category, items: [{rank, article_id, title, category, final}]}
        """
        params = {"date": date} if date else None
        return _call("GET", "/api/digests/preview", params=params)

    @mcp.tool()
    def get_digest(version_id: int) -> dict:
        """读取已发布日报版本的固定快照（含产物与渠道状态；条目文本已截断）。"""
        result = _call("GET", f"/api/digests/{int(version_id)}")
        for it in result.get("items") or []:
            snap = it.get("snapshot") or {}
            if isinstance(snap, dict):
                if snap.get("abstract"):
                    snap["abstract"] = str(snap["abstract"])[:300]
                if snap.get("analysis"):
                    snap["analysis"] = str(snap["analysis"])[:800]
        return result

    @mcp.tool()
    def get_digest_history(date_from: str = "", date_to: str = "",
                           limit: int = 20) -> dict:
        """列出已发布日报版本（不含条目，用于选择要读取的版本）。"""
        params: dict[str, Any] = {"limit": max(1, min(int(limit), 50))}
        if date_from:
            params["date_from"] = date_from
        if date_to:
            params["date_to"] = date_to
        return _call("GET", "/api/digests", params=params)

    # ── 版本化日报（写操作）────────────────────────────────────

    @mcp.tool()
    def publish_daily_digest(date: str, request_key: str = "",
                             channels: Optional[list[str]] = None) -> dict:
        """【写操作】发布当日正式日报：固定快照 → 渲染文件 → 投递指定渠道。

        当日已有版本时直接复用（不重算、不重发）。
        Args:
            date: 报告日期（YYYY-MM-DD）
            request_key: 可选幂等键；regenerate 必须换新键
            channels: 要投递的渠道，仅允许 "email" / "feishu"；省略 = 只发布不发送
        """
        body: dict[str, Any] = {"date": date}
        if request_key:
            body["request_key"] = request_key
        if channels is not None:
            body["channels"] = channels
        return _call("POST", "/api/digests/publish", body=body)

    @mcp.tool()
    def trends(months: int = 6) -> dict:
        """趋势统计：月度入库/相关数、期刊相关率、发现来源、阅读状态分布。"""
        return _call("GET", "/api/stats/trends", params={"months": months})

    # ── 阅读管理（写操作）──────────────────────────────────────

    @mcp.tool()
    def set_reading_status(paper_id: int, status: str) -> dict:
        """设置阅读状态。status: queued(待读) / reading(在读) / read(已读) / ''(移出清单)。"""
        return _call("POST", f"/api/articles/{int(paper_id)}/status",
                     body={"read_status": status})

    @mcp.tool()
    def star_paper(paper_id: int, starred: bool = True) -> dict:
        """收藏 / 取消收藏文献。"""
        return _call("POST", f"/api/articles/{int(paper_id)}/star",
                     body={"starred": starred})

    @mcp.tool()
    def add_note(paper_id: int, note: str) -> dict:
        """写入/覆盖我的阅读笔记（上限 20000 字符）。"""
        return _call("POST", f"/api/articles/{int(paper_id)}/note", body={"note": note})

    @mcp.tool()
    def add_tags(paper_id: int, tags: list[str]) -> dict:
        """设置文献标签（覆盖式；传空列表清空）。"""
        return _call("POST", f"/api/articles/{int(paper_id)}/tags", body={"tags": tags})

    # ── 研究专题 ────────────────────────────────────────────────

    @mcp.tool()
    def list_topics() -> dict:
        """列出全部研究专题及论文数。"""
        return _call("GET", "/api/topics")

    @mcp.tool()
    def get_topic(topic_id: int) -> dict:
        """专题详情：研究问题、备注与论文集合。"""
        return _call("GET", f"/api/topics/{int(topic_id)}")

    @mcp.tool()
    def create_topic(name: str, research_question: str = "", notes: str = "") -> dict:
        """新建研究专题。research_question 写"我具体想解决什么"。"""
        return _call("POST", "/api/topics",
                     body={"name": name, "research_question": research_question, "notes": notes})

    @mcp.tool()
    def add_papers_to_topic(topic_id: int, paper_ids: list[int]) -> dict:
        """把文献加入专题。"""
        return _call("POST", f"/api/topics/{int(topic_id)}/papers", body={"ids": paper_ids})

    # ── 外部集成 ────────────────────────────────────────────────

    @mcp.tool()
    def push_to_zotero(paper_ids: list[int]) -> dict:
        """批量推送到 Zotero（按 DOI 幂等；可附带阅读笔记与 OA PDF 附件）。"""
        return _call("POST", "/api/zotero/batch", body={"ids": paper_ids})

    @mcp.tool()
    def watch_paper(paper_id: int, active: bool = True) -> dict:
        """关注/取消关注某文献的新引用（新引用会自动进入评分流水线）。"""
        return _call("POST", f"/api/articles/{int(paper_id)}/watch", body={"active": active})

    @mcp.tool()
    def watch_author(name: str, openalex_id: str = "") -> dict:
        """关注作者（其新文章自动入库评分）。

        Args:
            openalex_id: OpenAlex 作者 ID（A 开头）。留空时自动检索，
                         唯一命中自动采用，多候选时返回列表由用户选择。
        """
        if openalex_id:
            return _call("POST", "/api/watch/authors", body={"name": name, "openalex_id": openalex_id})
        data = _call("POST", "/api/watch/authors/search", body={"name": name})
        items = data.get("items") or []
        if not items:
            return {"ok": False, "error": f"OpenAlex 未找到作者: {name}"}
        if len(items) == 1:
            best = items[0]
        else:
            # 选 works_count 最高的作为默认候选；多个命中时交由用户确认
            best = max(items, key=lambda x: x.get("works_count") or 0)
            if (items[0].get("works_count") or 0) - (best.get("works_count") or 0) < 5:
                best = items[0]
            return {
                "ok": False,
                "needs_choice": True,
                "candidates": items[:5],
                "hint": "找到多个候选作者，请向用户确认后用 openalex_id 重试",
            }
        return _call("POST", "/api/watch/authors",
                     body={"name": best.get("name") or name, "openalex_id": best.get("openalex_id")})

    # ── 任务与报告 ──────────────────────────────────────────────

    @mcp.tool()
    def run_pipeline(mode: str = "default") -> dict:
        """触发一次抓取流水线（后台运行）。mode: default / abstract / fulltext / weekly。

        成本提示：会抓取全部订阅源并对新文献调用 LLM 评分。仅在用户明确
        要求运行/更新时调用；调用后用 task_status 轮询进度。
        """
        resp = _call("POST", "/api/run", body={"mode": mode})
        status = _call("GET", "/api/status")
        return {**resp, "task": status.get("task")}

    @mcp.tool()
    def task_status() -> dict:
        """查询当前任务状态、数据库概览与最近一次任务的 LLM token 用量。"""
        return _call("GET", "/api/status")

    @mcp.tool()
    def list_reports(limit: int = 20) -> dict:
        """列出历史日报与周报（含各渠道推送状态）。"""
        return _call("GET", "/api/reports", params={"limit": limit})

    # ── LLM 生成类（高成本，须用户明确要求）────────────────────

    @mcp.tool()
    def compare_papers(paper_ids: list[int]) -> dict:
        """AI 对比 2-4 篇论文（对象/方法/数据/结果/局限，含来源标注）。

        成本提示：调用 LLM，耗时约 30-90 秒。仅在用户明确要求对比时调用；
        结果页 /results/{id} 可供用户查看。
        """
        resp = _call("POST", "/api/compare", body={"ids": paper_ids}, timeout=180)
        result = _call("GET", f"/api/results/{resp['id']}")
        return {**resp, "kind": result.get("kind"), "content": result.get("content")}

    @mcp.tool()
    def related_work_draft(paper_ids: list[int], focus: str = "") -> dict:
        """生成带编号引用的 Related Work 草稿段落（≥2 篇）。

        成本提示：调用 LLM，耗时约 30-90 秒。仅在用户明确要求写草稿时调用。
        """
        resp = _call("POST", "/api/related-work",
                     body={"ids": paper_ids, "focus": focus}, timeout=180)
        result = _call("GET", f"/api/results/{resp['id']}")
        return {**resp, "kind": result.get("kind"), "content": result.get("content")}

    @mcp.tool()
    def chat_with_paper(paper_id: int, question: str) -> dict:
        """就单篇文献向 AI 提问（多轮，基于标题/摘要/全文/AI 解读）。

        成本提示：调用 LLM。仅在用户明确要求与文献对话时调用。
        """
        text = _call_chat(f"/api/articles/{int(paper_id)}/chat",
                          {"question": question[:8000]})
        history = _call("GET", f"/api/articles/{int(paper_id)}/chat")
        return {"answer": text, "messages_total": history.get("count")}

    return mcp


def _call_chat(path: str, body: dict) -> str:
    """SSE 流式对话聚合：收集全部 delta，遇 error 事件抛 ApiError。"""
    url = _api() + path
    try:
        resp = requests.post(url, json=body, headers=_headers(),
                             timeout=int(os.environ.get("DPD_TIMEOUT") or TIMEOUT), stream=True)
    except requests.exceptions.ConnectionError as e:
        raise ApiError(f"无法连接文献工作台 {_api()}") from e
    if resp.status_code >= 400:
        try:
            detail = resp.json().get("error") or ""
        except Exception:  # noqa: BLE001
            detail = resp.text[:200]
        raise ApiError(f"HTTP {resp.status_code}: {detail}")
    pieces: list[str] = []
    error: Optional[str] = None
    context = ""
    for line in resp.iter_lines(decode_unicode=True):
        if not line or not line.startswith("data:"):
            continue
        try:
            payload = json.loads(line[5:].strip())
        except json.JSONDecodeError:
            continue
        if payload.get("context"):
            context = payload["context"]
        if payload.get("error"):
            error = payload["error"]
            break
        if payload.get("done"):
            break
        if payload.get("delta"):
            pieces.append(payload["delta"])
    if error and not pieces:
        raise ApiError(f"对话失败: {error}")
    answer = "".join(pieces)
    out: dict[str, Any] = {"answer": answer or "（模型未返回内容）"}
    if context:
        out["context"] = context
    if error:
        out["partial_error"] = error
    return out


def main() -> None:
    try:
        from mcp.server.fastmcp import FastMCP  # noqa: F401
    except ImportError:
        sys.stderr.write(
            "daily-paper-mcp 需要 MCP SDK：pip install 'mcp>=1.2,<2'\n"
        )
        sys.exit(1)
    mcp = _build_server()
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
