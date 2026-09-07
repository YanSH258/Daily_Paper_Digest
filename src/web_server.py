"""
web_server.py - 轻量网页端（只读看板 + API + 手动触发任务）
"""
from __future__ import annotations

import copy
import json
import logging
import os
import sqlite3
import threading
import traceback
import uuid
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional
from urllib.parse import parse_qs, urlparse

from core.notifier import classify_article
from main import load_config, run_once, validate_config

logger = logging.getLogger("web")


class TaskRunner:
    def __init__(self, base_config: dict[str, Any]) -> None:
        self._base_config = base_config
        self._lock = threading.Lock()
        self._last_scheduler_date: Optional[str] = None
        self.state: dict[str, Any] = {
            "running": False,
            "task_id": None,
            "trigger": None,
            "mode": None,
            "started_at": None,
            "ended_at": None,
            "last_success_at": None,
            "last_error": None,
            "last_report": None,
            "run_count": 0,
            "success_count": 0,
            "failure_count": 0,
        }

    def _make_run_cfg(self, mode: str) -> dict[str, Any]:
        cfg = copy.deepcopy(self._base_config)
        fetcher_cfg = cfg.setdefault("fetcher", {})
        if mode == "abstract":
            fetcher_cfg["use_fulltext"] = False
        elif mode == "fulltext":
            fetcher_cfg["use_fulltext"] = True
        return cfg

    def start_run(self, trigger: str, mode: str = "default", date_str: Optional[str] = None) -> tuple[bool, str]:
        with self._lock:
            if self.state["running"]:
                return False, "任务正在运行"

            task_id = str(uuid.uuid4())
            self.state.update({
                "running": True,
                "task_id": task_id,
                "trigger": trigger,
                "mode": mode,
                "started_at": datetime.now().isoformat(timespec="seconds"),
                "ended_at": None,
                "last_error": None,
            })

        thread = threading.Thread(
            target=self._run_task,
            args=(task_id, trigger, mode, date_str),
            daemon=True,
        )
        thread.start()
        return True, task_id

    def _run_task(self, task_id: str, trigger: str, mode: str, date_str: Optional[str]) -> None:
        cfg = self._make_run_cfg(mode)
        success = False
        error_msg = None

        try:
            logger.info("任务启动: task_id=%s trigger=%s mode=%s", task_id, trigger, mode)
            run_once(cfg, date_str=date_str)
            success = True
            logger.info("任务完成: task_id=%s", task_id)
        except Exception as e:  # pragma: no cover - 运行期保护
            error_msg = f"{type(e).__name__}: {e}\n{traceback.format_exc()}"
            logger.error("任务失败: task_id=%s, error=%s", task_id, e, exc_info=True)

        with self._lock:
            self.state["running"] = False
            self.state["ended_at"] = datetime.now().isoformat(timespec="seconds")
            self.state["run_count"] += 1
            if success:
                self.state["success_count"] += 1
                self.state["last_success_at"] = self.state["ended_at"]
            else:
                self.state["failure_count"] += 1
                self.state["last_error"] = error_msg

    def get_state(self) -> dict[str, Any]:
        with self._lock:
            return dict(self.state)

    def start_scheduler(self) -> None:
        scheduler_cfg = self._base_config.get("scheduler", {})
        run_time = scheduler_cfg.get("run_time", "08:00")
        thread = threading.Thread(target=self._scheduler_loop, args=(run_time,), daemon=True)
        thread.start()
        logger.info("已启动网页端调度线程: run_time=%s", run_time)

    def _scheduler_loop(self, run_time: str) -> None:
        try:
            hour, minute = map(int, run_time.split(":"))
        except ValueError:
            logger.error("scheduler.run_time 格式错误，应为 HH:MM，当前=%s", run_time)
            return

        while True:
            now = datetime.now()
            today = now.strftime("%Y-%m-%d")
            if now.hour == hour and now.minute == minute and self._last_scheduler_date != today:
                ok, msg = self.start_run(trigger="schedule", mode="default")
                if ok:
                    self._last_scheduler_date = today
                    logger.info("调度触发成功: %s", msg)
                else:
                    logger.warning("调度触发跳过: %s", msg)
            threading.Event().wait(30)


class WebContext:
    def __init__(self, config_path: str) -> None:
        self.config_path = config_path
        self.config = load_config(config_path)
        validate_config(self.config)
        self.runner = TaskRunner(self.config)

        output_cfg = self.config.get("output", {})
        self.output_dir = Path(output_cfg.get("output_dir", "data/output")).resolve()
        self.output_dir.mkdir(parents=True, exist_ok=True)

        db_path = self.config.get("database", {}).get("path", "data/db/chem_daily.db")
        self.db_path = Path(db_path).resolve()

        web_cfg = self.config.get("web", {})
        self.api_token = os.environ.get("WEB_API_TOKEN") or web_cfg.get("api_token")
        self.enable_scheduler = bool(web_cfg.get("enable_scheduler", False))

    def connect_db(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        return conn


def _to_int(value: Optional[str], default: int, min_v: int, max_v: int) -> int:
    if value is None:
        return default
    try:
        v = int(value)
    except ValueError:
        return default
    return max(min_v, min(max_v, v))


def _parse_bool(value: Optional[str], default: bool = False) -> bool:
    if value is None:
        return default
    return value.lower() in {"1", "true", "yes", "on"}


def _list_articles(ctx: WebContext, query: dict[str, list[str]]) -> dict[str, Any]:
    q = (query.get("q", [""])[0] or "").strip()
    journal = (query.get("journal", [""])[0] or "").strip()
    topic = (query.get("topic", [""])[0] or "").strip()
    min_score = float(query.get("min_score", ["0"])[0] or 0)
    analyzed_only = _parse_bool((query.get("analyzed_only", ["false"])[0] or "false"))
    limit = _to_int(query.get("limit", ["50"])[0], 50, 1, 200)
    offset = _to_int(query.get("offset", ["0"])[0], 0, 0, 100000)

    where = ["1=1"]
    params: list[Any] = []

    if q:
        where.append("(title LIKE ? OR abstract LIKE ? OR authors LIKE ? OR journal LIKE ?)")
        like = f"%{q}%"
        params.extend([like, like, like, like])
    if journal:
        where.append("journal = ?")
        params.append(journal)

    where.append("COALESCE(relevance, 0) >= ?")
    params.append(min_score)

    if analyzed_only:
        where.append("analysis IS NOT NULL AND analysis != ''")

    sql = (
        "SELECT id, doi, title, journal, authors, pub_date, url, relevance, analysis, created_at "
        "FROM articles WHERE " + " AND ".join(where) +
        " ORDER BY relevance DESC, created_at DESC LIMIT ? OFFSET ?"
    )
    params.extend([limit, offset])

    conn = ctx.connect_db()
    try:
        rows = conn.execute(sql, params).fetchall()
        data = []
        for row in rows:
            item = dict(row)
            item["topic"] = classify_article(item)
            item["has_analysis"] = bool(item.get("analysis"))
            if topic and item["topic"] != topic:
                continue
            data.append(item)

        journal_rows = conn.execute(
            "SELECT DISTINCT journal FROM articles WHERE journal IS NOT NULL AND journal != '' ORDER BY journal"
        ).fetchall()
        journals = [r[0] for r in journal_rows]

        report_rows = conn.execute(
            "SELECT report_date FROM daily_reports ORDER BY report_date DESC LIMIT 14"
        ).fetchall()
        recent_dates = [r[0] for r in report_rows]

        return {
            "items": data,
            "count": len(data),
            "journals": journals,
            "recent_report_dates": recent_dates,
        }
    finally:
        conn.close()


def _get_article_detail(ctx: WebContext, article_id: int) -> Optional[dict[str, Any]]:
    conn = ctx.connect_db()
    try:
        row = conn.execute(
            "SELECT * FROM articles WHERE id = ?",
            (article_id,),
        ).fetchone()
        if not row:
            return None
        item = dict(row)
        item["topic"] = classify_article(item)
        item["has_analysis"] = bool(item.get("analysis"))
        return item
    finally:
        conn.close()


def _list_reports(ctx: WebContext, limit: int = 30) -> list[dict[str, Any]]:
    conn = ctx.connect_db()
    try:
        rows = conn.execute(
            "SELECT report_date, file_path, total_found, total_pushed, created_at "
            "FROM daily_reports ORDER BY report_date DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def _db_summary(ctx: WebContext) -> dict[str, Any]:
    conn = ctx.connect_db()
    try:
        total = conn.execute("SELECT COUNT(*) FROM articles").fetchone()[0]
        analyzed = conn.execute(
            "SELECT COUNT(*) FROM articles WHERE analysis IS NOT NULL AND analysis != ''"
        ).fetchone()[0]
        avg_score = conn.execute("SELECT ROUND(AVG(COALESCE(relevance, 0)), 2) FROM articles").fetchone()[0]
        return {
            "total_articles": total,
            "analyzed_articles": analyzed,
            "avg_relevance": avg_score or 0,
        }
    finally:
        conn.close()


DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Daily Paper Digest 控制台</title>
  <style>
    body { font-family: -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; background:#f6f8fa; margin:0; color:#1f2328; }
    header { background:#24292f; color:#fff; padding:14px 20px; display:flex; justify-content:space-between; gap:12px; flex-wrap:wrap; }
    main { max-width:1200px; margin:16px auto; padding:0 14px; }
    .card { background:#fff; border:1px solid #d0d7de; border-radius:8px; padding:14px; margin-bottom:12px; }
    .row { display:flex; gap:10px; flex-wrap:wrap; align-items:center; }
    input, select, button { padding:8px 10px; border:1px solid #d0d7de; border-radius:6px; }
    button { cursor:pointer; background:#0969da; color:#fff; border:none; }
    button.secondary { background:#57606a; }
    table { width:100%; border-collapse:collapse; font-size:14px; }
    th, td { border-bottom:1px solid #d8dee4; padding:8px; text-align:left; vertical-align:top; }
    th { background:#f6f8fa; }
    .mono { font-family: ui-monospace,SFMono-Regular,Menlo,Consolas,monospace; }
    .small { font-size:12px; color:#57606a; }
    .pill { display:inline-block; padding:2px 8px; border-radius:999px; font-size:12px; background:#ddf4ff; color:#0969da; }
    .err { color:#cf222e; white-space:pre-wrap; max-height:200px; overflow:auto; }
    .ok { color:#1a7f37; }
    a { color:#0969da; }
  </style>
</head>
<body>
<header>
  <strong>📚 Daily Paper Digest 控制台</strong>
  <div><a href="/paper-index" target="_blank" style="color:#9ecbff;">打开只读大盘 paper_index.html</a></div>
</header>
<main>
  <section class="card">
    <h3>任务控制</h3>
    <div class="row" style="margin-top:8px;">
      <button onclick="triggerRun('default')">运行（默认模式）</button>
      <button onclick="triggerRun('abstract')" class="secondary">运行（仅摘要模式）</button>
      <button onclick="triggerRun('fulltext')" class="secondary">运行（强制全文模式）</button>
      <input id="tokenInput" placeholder="可选：API Token" style="min-width:220px;" />
      <span id="runResult" class="small"></span>
    </div>
    <div id="statusBox" class="small" style="margin-top:10px;"></div>
  </section>

  <section class="card">
    <h3>筛选检索</h3>
    <div class="row" style="margin-top:8px;">
      <input id="q" placeholder="关键词（标题/摘要/作者/期刊）" style="min-width:260px; flex:1;" />
      <select id="journal"><option value="">全部期刊</option></select>
      <select id="topic">
        <option value="">全部方向</option>
        <option>机器学习势函数 / MLIP</option>
        <option>分子动力学 / MD</option>
        <option>金属有机框架 / MOF</option>
        <option>DFT / 第一性原理</option>
        <option>催化 / 反应机理</option>
        <option>材料性质预测</option>
        <option>大模型 / AI for Science</option>
        <option>量子化学 / 电子结构</option>
        <option>纳米材料 / 表面</option>
        <option>其他</option>
      </select>
      <input id="minScore" type="number" min="0" max="10" step="0.1" value="0" style="width:100px;" />
      <label class="small"><input id="analyzedOnly" type="checkbox" /> 仅看有解读</label>
      <button onclick="loadArticles()">查询</button>
    </div>
    <div id="summary" class="small" style="margin-top:10px;"></div>
    <div style="overflow:auto; margin-top:10px;">
      <table id="articleTable">
        <thead>
          <tr>
            <th>ID</th><th>评分</th><th>方向</th><th>期刊</th><th>标题</th><th>日期</th><th>解读</th>
          </tr>
        </thead>
        <tbody></tbody>
      </table>
    </div>
  </section>

  <section class="card">
    <h3>文章详情</h3>
    <div id="detail" class="small">点击上方表格标题查看详情</div>
  </section>

  <section class="card">
    <h3>历史日报</h3>
    <div id="reports" class="small"></div>
  </section>
</main>

<script>
async function fetchJSON(url, options = {}) {
  const res = await fetch(url, options);
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
}

function esc(s) {
  return (s ?? '').toString().replace(/[&<>"']/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
}

async function loadStatus() {
  try {
    const data = await fetchJSON('/api/status');
    const s = data.task || {};
    const running = s.running ? '<span class="pill">运行中</span>' : '<span class="pill">空闲</span>';
    const err = s.last_error ? `<div class="err">${esc(s.last_error)}</div>` : '';
    document.getElementById('statusBox').innerHTML = `
      ${running} task_id=<span class="mono">${esc(s.task_id || '-')}</span> trigger=${esc(s.trigger || '-')}
      mode=${esc(s.mode || '-')}<br>
      started=${esc(s.started_at || '-')} ended=${esc(s.ended_at || '-')}<br>
      runs=${esc(s.run_count)} success=${esc(s.success_count)} failure=${esc(s.failure_count)}
      ${err}
    `;
  } catch (e) {
    document.getElementById('statusBox').innerText = '状态获取失败: ' + e.message;
  }
}

async function triggerRun(mode) {
  const token = document.getElementById('tokenInput').value.trim();
  const headers = {'Content-Type': 'application/json'};
  if (token) headers['X-API-Token'] = token;
  const resultEl = document.getElementById('runResult');
  resultEl.innerText = '提交中...';
  try {
    const data = await fetchJSON('/api/run', {
      method: 'POST',
      headers,
      body: JSON.stringify({mode})
    });
    resultEl.innerHTML = `<span class="ok">已触发，task_id=${esc(data.task_id)}</span>`;
    await loadStatus();
  } catch (e) {
    resultEl.innerText = '触发失败: ' + e.message;
  }
}

async function loadArticles() {
  const q = document.getElementById('q').value.trim();
  const journal = document.getElementById('journal').value;
  const topic = document.getElementById('topic').value;
  const minScore = document.getElementById('minScore').value || '0';
  const analyzedOnly = document.getElementById('analyzedOnly').checked ? 'true' : 'false';

  const params = new URLSearchParams({q, journal, topic, min_score: minScore, analyzed_only: analyzedOnly, limit: '100'});

  try {
    const data = await fetchJSON('/api/articles?' + params.toString());
    const tbody = document.querySelector('#articleTable tbody');
    tbody.innerHTML = '';
    for (const a of data.items) {
      const tr = document.createElement('tr');
      tr.innerHTML = `
        <td>${esc(a.id)}</td>
        <td>${Number(a.relevance || 0).toFixed(1)}</td>
        <td>${esc(a.topic)}</td>
        <td>${esc(a.journal || '')}</td>
        <td><a href="#" data-id="${esc(a.id)}">${esc(a.title || '')}</a></td>
        <td>${esc(a.pub_date || '')}</td>
        <td>${a.has_analysis ? '✅' : '—'}</td>
      `;
      tbody.appendChild(tr);
    }

    document.querySelectorAll('a[data-id]').forEach(a => {
      a.addEventListener('click', (e) => {
        e.preventDefault();
        loadDetail(a.dataset.id);
      });
    });

    if (data.journals) {
      const journalSel = document.getElementById('journal');
      const current = journalSel.value;
      journalSel.innerHTML = '<option value="">全部期刊</option>';
      for (const j of data.journals) {
        const opt = document.createElement('option');
        opt.value = j;
        opt.textContent = j;
        if (j === current) opt.selected = true;
        journalSel.appendChild(opt);
      }
    }

    document.getElementById('summary').innerText = `结果 ${data.count} 篇`;
  } catch (e) {
    document.getElementById('summary').innerText = '查询失败: ' + e.message;
  }
}

async function loadDetail(id) {
  try {
    const a = await fetchJSON('/api/articles/' + id);
    document.getElementById('detail').innerHTML = `
      <div><strong>${esc(a.title || '')}</strong></div>
      <div class="small">期刊：${esc(a.journal || '')} | 日期：${esc(a.pub_date || '')} | 评分：${Number(a.relevance || 0).toFixed(1)} | 方向：${esc(a.topic || '')}</div>
      <div style="margin-top:8px;"><strong>链接：</strong><a href="${esc(a.url || '#')}" target="_blank">${esc(a.url || '')}</a></div>
      <div style="margin-top:8px;"><strong>摘要：</strong><pre style="white-space:pre-wrap;">${esc(a.abstract || '')}</pre></div>
      <div style="margin-top:8px;"><strong>AI解读：</strong><pre style="white-space:pre-wrap;">${esc(a.analysis || '无')}</pre></div>
    `;
  } catch (e) {
    document.getElementById('detail').innerText = '详情获取失败: ' + e.message;
  }
}

async function loadReports() {
  try {
    const data = await fetchJSON('/api/reports?limit=30');
    if (!data.items || data.items.length === 0) {
      document.getElementById('reports').innerText = '暂无历史日报';
      return;
    }
    const links = data.items.map(r => {
      const date = esc(r.report_date);
      return `<li>${date} | found=${esc(r.total_found)} pushed=${esc(r.total_pushed)} | <a href="/reports/${date}.md" target="_blank">Markdown</a> <a href="/reports/${date}.html" target="_blank">HTML</a></li>`;
    }).join('');
    document.getElementById('reports').innerHTML = `<ul>${links}</ul>`;
  } catch (e) {
    document.getElementById('reports').innerText = '历史日报获取失败: ' + e.message;
  }
}

async function init() {
  await loadStatus();
  await loadArticles();
  await loadReports();
  setInterval(loadStatus, 5000);
}
init();
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    server_version = "DailyPaperWeb/0.1"

    @property
    def ctx(self) -> WebContext:
        return self.server.context  # type: ignore[attr-defined]

    def _json_response(self, payload: dict[str, Any], code: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _text_response(self, text: str, code: int = 200, content_type: str = "text/plain; charset=utf-8") -> None:
        body = text.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_file(self, path: Path, content_type: str) -> None:
        if not path.exists() or not path.is_file():
            self._json_response({"error": "file not found", "path": str(path)}, code=404)
            return
        body = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _require_token(self) -> bool:
        token = self.ctx.api_token
        if not token:
            return True
        req_token = self.headers.get("X-API-Token", "")
        if req_token == token:
            return True
        self._json_response({"error": "unauthorized"}, code=401)
        return False

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)

        try:
            if path == "/" or path == "/dashboard":
                self._text_response(DASHBOARD_HTML, content_type="text/html; charset=utf-8")
                return

            if path == "/healthz":
                self._json_response({"ok": True, "time": datetime.now().isoformat(timespec="seconds")})
                return

            if path == "/paper-index":
                self._serve_file(self.ctx.output_dir / "paper_index.html", "text/html; charset=utf-8")
                return

            if path.startswith("/reports/"):
                filename = path.removeprefix("/reports/")
                safe_name = Path(filename).name
                if safe_name != filename:
                    self._json_response({"error": "bad filename"}, code=400)
                    return
                suffix = Path(safe_name).suffix.lower()
                ctype = "text/plain; charset=utf-8"
                if suffix == ".html":
                    ctype = "text/html; charset=utf-8"
                elif suffix == ".md":
                    ctype = "text/markdown; charset=utf-8"
                self._serve_file(self.ctx.output_dir / safe_name, ctype)
                return

            if path == "/api/status":
                self._json_response({
                    "task": self.ctx.runner.get_state(),
                    "db": _db_summary(self.ctx),
                    "output_dir": str(self.ctx.output_dir),
                    "db_path": str(self.ctx.db_path),
                })
                return

            if path == "/api/articles":
                self._json_response(_list_articles(self.ctx, query))
                return

            if path.startswith("/api/articles/"):
                article_id_raw = path.removeprefix("/api/articles/")
                if not article_id_raw.isdigit():
                    self._json_response({"error": "invalid article id"}, code=400)
                    return
                item = _get_article_detail(self.ctx, int(article_id_raw))
                if not item:
                    self._json_response({"error": "article not found"}, code=404)
                    return
                self._json_response(item)
                return

            if path == "/api/reports":
                limit = _to_int(query.get("limit", ["30"])[0], 30, 1, 365)
                self._json_response({"items": _list_reports(self.ctx, limit=limit)})
                return

            self._json_response({"error": "not found"}, code=404)
        except Exception as e:  # pragma: no cover - 运行期保护
            logger.error("GET 处理失败: %s", e, exc_info=True)
            self._json_response({"error": str(e)}, code=500)

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path

        try:
            if path == "/api/run":
                if not self._require_token():
                    return

                length = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(length) if length > 0 else b"{}"
                data = json.loads(body.decode("utf-8"))

                mode = str(data.get("mode", "default"))
                date_str = data.get("date")
                if mode not in {"default", "abstract", "fulltext"}:
                    self._json_response({"error": "mode must be one of default/abstract/fulltext"}, code=400)
                    return

                ok, msg = self.ctx.runner.start_run(trigger="manual", mode=mode, date_str=date_str)
                if not ok:
                    self._json_response({"ok": False, "message": msg}, code=409)
                    return
                self._json_response({"ok": True, "task_id": msg})
                return

            self._json_response({"error": "not found"}, code=404)
        except json.JSONDecodeError:
            self._json_response({"error": "invalid json"}, code=400)
        except Exception as e:  # pragma: no cover - 运行期保护
            logger.error("POST 处理失败: %s", e, exc_info=True)
            self._json_response({"error": str(e)}, code=500)

    def log_message(self, fmt: str, *args: Any) -> None:
        logger.info("%s - %s", self.address_string(), fmt % args)


def run_web_server(config_path: str = "config/config.yaml", host: str = "0.0.0.0", port: int = 8080) -> None:
    ctx = WebContext(config_path=config_path)

    server = ThreadingHTTPServer((host, port), Handler)
    server.context = ctx  # type: ignore[attr-defined]

    web_cfg = ctx.config.get("web", {})
    if ctx.enable_scheduler:
        ctx.runner.start_scheduler()

    logger.info("Web 控制台已启动: http://%s:%s", host, port)
    logger.info("数据库: %s", ctx.db_path)
    logger.info("输出目录: %s", ctx.output_dir)
    logger.info("调度线程: %s", "启用" if ctx.enable_scheduler else "关闭")
    if ctx.api_token:
        logger.info("POST /api/run 已启用 Token 校验（X-API-Token）")
    if web_cfg.get("readonly", False):
        logger.info("readonly=true: 请在反向代理层禁用 POST /api/run")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("收到中断信号，正在退出...")
    finally:
        server.server_close()


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Daily Paper Digest 网页控制台")
    parser.add_argument("--config", default="config/config.yaml", help="配置文件路径")
    parser.add_argument("--host", default="0.0.0.0", help="监听地址")
    parser.add_argument("--port", type=int, default=8080, help="监听端口")
    args = parser.parse_args()

    run_web_server(config_path=args.config, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
