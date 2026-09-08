"""
stat_db.py - 数据库统计 + HTML 索引页生成（含表格视图 + 浏览器导出 Excel）
"""
import sqlite3
import argparse
import csv
import re
import sys
import html as html_module
from pathlib import Path
from collections import Counter
from datetime import datetime

sys.path.insert(0, str(Path(__file__).parent.parent))
from core.notifier import classify_article


# ── HTML 索引页生成 ───────────────────────────────────────────

def build_html_index(conn, threshold: int, output_path: str):
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM articles WHERE relevance >= ? ORDER BY relevance DESC, pub_date DESC",
        (threshold,)
    ).fetchall()

    articles = []
    for row in rows:
        a = dict(row)
        a["topic"] = classify_article(a)
        a["authors_clean"] = " / ".join(
            x.strip() for x in
            (a.get("authors", "") or "").replace("\n", ",").split(",")
            if x.strip()
        )
        # HTML 转义原始文本字段，防止 XSS
        a["title_safe"]   = html_module.escape(a.get("title", "") or "")
        a["journal_safe"] = html_module.escape(a.get("journal", "") or "")
        a["topic_safe"]   = html_module.escape(a["topic"])
        a["authors_safe"] = html_module.escape(a["authors_clean"])
        a["url_safe"]     = html_module.escape(a.get("url", "") or "#")
        a["doi_safe"]     = html_module.escape(a.get("doi", "") or "")
        a["date_safe"]    = html_module.escape(a.get("pub_date", "") or "")
        abstract_raw = (a.get("abstract") or "")[:400]
        if len(a.get("abstract") or "") > 400:
            abstract_raw += "…"
        a["abstract_safe"] = html_module.escape(abstract_raw)

        if a.get("analysis"):
            text = html_module.escape(a["analysis"])
            text = re.sub(r'^### (.+)$', r'<h4>\1</h4>', text, flags=re.MULTILINE)
            text = re.sub(r'^## (.+)$',  r'<h3>\1</h3>', text, flags=re.MULTILINE)
            text = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', text)
            text = text.replace('\n', '<br>')
            a["analysis_html"] = text
        else:
            a["analysis_html"] = "<em>仅获取到摘要，无 AI 解读。</em>"
        articles.append(a)

    journals     = sorted(set(a["journal"] for a in articles if a["journal"]))
    topics       = sorted(set(a["topic"]   for a in articles if a["topic"]))
    total        = len(articles)
    analyzed     = sum(1 for a in articles if a.get("analysis"))
    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # ── 卡片 HTML ────────────────────────────────────────────
    cards_html = ""
    for a in articles:
        score = float(a.get("relevance") or 0)
        stars = "⭐⭐⭐" if score >= 8 else "⭐⭐" if score >= 6 else "⭐"
        analysis_badge = (
            '<span class="badge badge-analyzed">✅ AI解读</span>'
            if a.get("analysis") else
            '<span class="badge badge-abstract">📄 仅摘要</span>'
        )
        doi_link = (
            f'<a href="https://doi.org/{a["doi_safe"]}" target="_blank">{a["doi_safe"]}</a>'
            if a.get("doi") else "—"
        )

        cards_html += f"""
<div class="card" id="article-{a['id']}" data-journal="{a['journal_safe']}"
     data-topic="{a['topic_safe']}" data-score="{score}" data-date="{a['date_safe']}">
  <div class="card-header" onclick="toggleCard(this)">
    <div class="card-title">
      <span class="stars">{stars}</span>
      <a href="{a['url_safe']}" target="_blank" class="title-link">{a['title_safe']}</a>
      {analysis_badge}
    </div>
    <div class="card-meta">
      <span class="journal">{a['journal_safe']}</span>
      <span class="topic">{a['topic_safe']}</span>
      <span class="score">{score:.1f}/10</span>
      <span class="date">{a['date_safe']}</span>
    </div>
    <div class="card-authors">{a['authors_safe'] or '—'}</div>
  </div>
  <div class="card-body" style="display:none">
    <div class="meta-table">
      <div><strong>DOI</strong>{doi_link}</div>
      <div><strong>摘要</strong>{a['abstract_safe']}</div>
    </div>
    <div class="analysis-content">{a['analysis_html']}</div>
  </div>
</div>"""

    # ── 表格行 HTML ──────────────────────────────────────────
    table_rows_html = ""
    for a in articles:
        score = float(a.get("relevance") or 0)
        stars = "⭐⭐⭐" if score >= 8 else "⭐⭐" if score >= 6 else "⭐"

        doi_display = (a["doi_safe"][:20] + "…") if len(a["doi_safe"]) > 20 else a["doi_safe"]
        doi_cell = f'<a href="https://doi.org/{a["doi_safe"]}" target="_blank">{doi_display}</a>' if a.get("doi") else "—"

        article_id = a["id"]
        has_analysis = bool(a.get("analysis"))
        analysis_link = (
            f'<a href="#article-{article_id}" '
            f'onclick="switchTab(\'cards\');setTimeout(function(){{expandById({article_id})}},100)">'
            f'查看解读</a>'
        ) if has_analysis else "—"

        authors_short = a["authors_safe"]
        if len(authors_short) > 40:
            authors_short = authors_short[:40] + "…"

        table_rows_html += (
            f'<tr data-journal="{a["journal_safe"]}" data-topic="{a["topic_safe"]}"'
            f' data-score="{score}" data-date="{a["date_safe"]}"'
            f' data-analysis="{"yes" if has_analysis else "no"}">\n'
            f'  <td>{a["date_safe"]}</td>\n'
            f'  <td class="journal-cell">{a["journal_safe"]}</td>\n'
            f'  <td class="topic-cell">{a["topic_safe"]}</td>\n'
            f'  <td class="score-cell">{stars} {score:.1f}</td>\n'
            f'  <td class="title-cell"><a href="{a["url_safe"]}" target="_blank">{a["title_safe"]}</a></td>\n'
            f'  <td>{authors_short}</td>\n'
            f'  <td>{doi_cell}</td>\n'
            f'  <td class="analysis-cell">{analysis_link}</td>\n'
            f'</tr>\n'
        )


    # ── 完整 HTML ────────────────────────────────────────────
    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>📚 文献数据库 | Daily Paper Digest</title>
<script src="https://cdn.sheetjs.com/xlsx-0.20.1/package/dist/xlsx.full.min.js"></script>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
          background: #f5f7fa; color: #333; }}

  /* 顶部栏 */
  .top-bar {{ background: #2c3e50; color: #fff; padding: 14px 24px;
              display: flex; align-items: center; justify-content: space-between;
              flex-wrap: wrap; gap: 10px; }}
  .top-bar h1 {{ font-size: 1.2em; }}
  .top-bar .stats {{ font-size: 0.82em; opacity: 0.8; }}
  .top-bar .actions {{ display: flex; gap: 8px; }}
  .btn {{ padding: 6px 14px; border: none; border-radius: 6px; cursor: pointer;
          font-size: 0.85em; font-weight: 500; }}
  .btn-export {{ background: #27ae60; color: #fff; }}
  .btn-export:hover {{ background: #219a52; }}
  .btn-tab {{ background: rgba(255,255,255,0.15); color: #fff; }}
  .btn-tab.active {{ background: #3498db; }}
  .btn-tab:hover {{ background: #3498db; }}

  /* 工具栏 */
  .toolbar {{ background: #fff; padding: 10px 24px; border-bottom: 1px solid #e1e4e8;
              display: flex; gap: 10px; flex-wrap: wrap; align-items: center;
              position: sticky; top: 0; z-index: 100;
              box-shadow: 0 1px 4px rgba(0,0,0,.08); }}
  .toolbar input {{ flex: 1; min-width: 180px; padding: 6px 12px;
                    border: 1px solid #ddd; border-radius: 6px; font-size: 0.88em; }}
  .toolbar select {{ padding: 6px 10px; border: 1px solid #ddd;
                     border-radius: 6px; font-size: 0.88em; background: #fff; }}
  .toolbar .btn {{ background: #ecf0f1; color: #333; }}
  .toolbar .btn:hover {{ background: #dde1e3; }}
  .count-bar {{ padding: 6px 24px; font-size: 0.82em; color: #666;
                background: #f8f9fa; border-bottom: 1px solid #eee; }}

  /* 卡片视图 */
  .container {{ max-width: 1100px; margin: 0 auto; padding: 14px 24px; }}
  .card {{ background: #fff; border: 1px solid #e1e4e8; border-radius: 8px;
           margin-bottom: 8px; overflow: hidden;
           box-shadow: 0 1px 3px rgba(0,0,0,.05); }}
  .card:target {{ border-color: #3498db; box-shadow: 0 0 0 3px rgba(52,152,219,.2); }}
  .card-header {{ padding: 12px 16px; cursor: pointer; }}
  .card-header:hover {{ background: #f8f9fa; }}
  .card-title {{ display: flex; align-items: flex-start; gap: 8px; margin-bottom: 5px; }}
  .stars {{ flex-shrink: 0; font-size: 0.82em; }}
  .title-link {{ color: #0366d6; text-decoration: none; font-weight: 600;
                 font-size: 0.92em; line-height: 1.4; }}
  .title-link:hover {{ text-decoration: underline; }}
  .badge {{ font-size: 0.72em; padding: 2px 6px; border-radius: 10px;
            flex-shrink: 0; white-space: nowrap; }}
  .badge-analyzed {{ background: #e6f4ea; color: #1a7f37; }}
  .badge-abstract  {{ background: #f0f0f0; color: #666; }}
  .card-meta {{ display: flex; gap: 10px; font-size: 0.8em; color: #666;
                flex-wrap: wrap; margin-bottom: 3px; }}
  .journal {{ font-weight: 600; color: #2c3e50; }}
  .topic   {{ background: #eaf3fb; color: #1a6fa8; padding: 1px 6px; border-radius: 10px; }}
  .score   {{ color: #e67e22; font-weight: 600; }}
  .card-authors {{ font-size: 0.78em; color: #888; }}
  .card-body {{ padding: 0 16px 14px; border-top: 1px solid #f0f0f0; }}
  .meta-table {{ margin: 10px 0; font-size: 0.83em; color: #555; }}
  .meta-table div {{ margin-bottom: 5px; }}
  .meta-table strong {{ display: inline-block; width: 48px; color: #333; }}
  .analysis-content {{ font-size: 0.86em; line-height: 1.7; color: #333;
                        background: #f8f9fa; padding: 12px; border-radius: 6px;
                        border-left: 3px solid #3498db; margin-top: 8px; }}
  .analysis-content h3 {{ margin: 12px 0 5px; color: #2c3e50; font-size: 0.98em; }}
  .analysis-content h4 {{ margin: 8px 0 3px; color: #34495e; font-size: 0.93em; }}

  /* 表格视图 */
  .table-container {{ max-width: 1400px; margin: 0 auto; padding: 14px 24px;
                      overflow-x: auto; }}
  table {{ width: 100%; border-collapse: collapse; background: #fff;
           box-shadow: 0 1px 3px rgba(0,0,0,.08); border-radius: 8px;
           overflow: hidden; font-size: 0.85em; }}
  thead tr {{ background: #2c3e50; color: #fff; }}
  th {{ padding: 10px 12px; text-align: left; font-weight: 600;
        cursor: pointer; white-space: nowrap; user-select: none; }}
  th:hover {{ background: #34495e; }}
  th.sorted-asc::after  {{ content: " ▲"; font-size: 0.75em; }}
  th.sorted-desc::after {{ content: " ▼"; font-size: 0.75em; }}
  td {{ padding: 9px 12px; border-bottom: 1px solid #f0f0f0;
        vertical-align: top; }}
  tr:hover td {{ background: #f8f9fa; }}
  tr:last-child td {{ border-bottom: none; }}
  .title-cell {{ max-width: 320px; }}
  .title-cell a {{ color: #0366d6; text-decoration: none; font-weight: 500; }}
  .title-cell a:hover {{ text-decoration: underline; }}
  .journal-cell {{ white-space: nowrap; font-weight: 600; color: #2c3e50; }}
  .topic-cell {{ white-space: nowrap; }}
  .score-cell {{ white-space: nowrap; color: #e67e22; font-weight: 600; }}
  .analysis-cell a {{ color: #1a7f37; text-decoration: none; font-weight: 500; }}
  .analysis-cell a:hover {{ text-decoration: underline; }}

  .no-results {{ text-align: center; padding: 60px; color: #999; font-size: 1em; }}
  #tableView, #cardView {{ display: none; }}
  #tableView.active, #cardView.active {{ display: block; }}
</style>
</head>
<body>

<div class="top-bar">
  <h1>📚 文献数据库</h1>
  <div class="stats">共 {total} 篇 · {analyzed} 篇有AI解读 · {generated_at}</div>
  <div class="actions">
    <button class="btn btn-tab active" id="tabCards" onclick="switchTab('cards')">🗂 卡片视图</button>
    <button class="btn btn-tab" id="tabTable" onclick="switchTab('table')">📋 表格视图</button>
    <button class="btn btn-export" onclick="exportExcel()">⬇ 导出 Excel</button>
  </div>
</div>

<div class="toolbar">
  <input type="text" id="searchBox" placeholder="搜索标题、作者、期刊…" oninput="applyFilters()">
  <select id="journalFilter" onchange="applyFilters()">
    <option value="">全部期刊</option>
    {''.join(f'<option value="{j}">{j}</option>' for j in journals)}
  </select>
  <select id="topicFilter" onchange="applyFilters()">
    <option value="">全部方向</option>
    {''.join(f'<option value="{t}">{t}</option>' for t in topics)}
  </select>
  <select id="analysisFilter" onchange="applyFilters()">
    <option value="">全部</option>
    <option value="yes">有AI解读</option>
    <option value="no">仅摘要</option>
  </select>
  <select id="sortBy" onchange="applyFilters()">
    <option value="score">按评分</option>
    <option value="date">按日期</option>
  </select>
  <button class="btn" onclick="expandAll()">展开全部</button>
  <button class="btn" onclick="collapseAll()">收起全部</button>
</div>
<div class="count-bar" id="countBar">显示 {total} 篇</div>

<!-- 卡片视图 -->
<div id="cardView" class="active">
  <div class="container" id="cardContainer">
    {cards_html}
  </div>
  <div class="no-results" id="noResultsCard" style="display:none">没有符合条件的文章</div>
</div>

<!-- 表格视图 -->
<div id="tableView">
  <div class="table-container">
    <table id="mainTable">
      <thead>
        <tr>
          <th onclick="sortTable(0)">日期</th>
          <th onclick="sortTable(1)">期刊</th>
          <th onclick="sortTable(2)">方向</th>
          <th onclick="sortTable(3)">评分</th>
          <th onclick="sortTable(4)">标题</th>
          <th onclick="sortTable(5)">作者</th>
          <th>DOI</th>
          <th>AI解读</th>
        </tr>
      </thead>
      <tbody id="tableBody">
        {table_rows_html}
      </tbody>
    </table>
    <div class="no-results" id="noResultsTable" style="display:none">没有符合条件的文章</div>
  </div>
</div>

<script>
// ── Tab 切换 ─────────────────────────────────────────────────
function switchTab(tab) {{
  document.getElementById('cardView').classList.toggle('active', tab === 'cards');
  document.getElementById('tableView').classList.toggle('active', tab === 'table');
  document.getElementById('tabCards').classList.toggle('active', tab === 'cards');
  document.getElementById('tabTable').classList.toggle('active', tab === 'table');
  applyFilters();
}}

// ── 锚点自动展开 ─────────────────────────────────────────────
window.addEventListener('load', function() {{
  const hash = window.location.hash;
  if (hash) {{
    const card = document.querySelector(hash);
    if (card) {{
      const body = card.querySelector('.card-body');
      if (body) body.style.display = 'block';
      setTimeout(() => card.scrollIntoView({{behavior: 'smooth', block: 'start'}}), 150);
    }}
  }}
}});

function expandById(id) {{
  const card = document.getElementById('article-' + id);
  if (card) {{
    const body = card.querySelector('.card-body');
    if (body) body.style.display = 'block';
    card.scrollIntoView({{behavior: 'smooth', block: 'start'}});
  }}
}}

// ── 卡片操作 ─────────────────────────────────────────────────
function toggleCard(header) {{
  const body = header.nextElementSibling;
  body.style.display = body.style.display === 'none' ? 'block' : 'none';
}}
function expandAll() {{
  document.querySelectorAll('.card-body').forEach(b => b.style.display = 'block');
}}
function collapseAll() {{
  document.querySelectorAll('.card-body').forEach(b => b.style.display = 'none');
}}

// ── 筛选逻辑 ─────────────────────────────────────────────────
function applyFilters() {{
  const q        = document.getElementById('searchBox').value.toLowerCase();
  const journal  = document.getElementById('journalFilter').value;
  const topic    = document.getElementById('topicFilter').value;
  const analysis = document.getElementById('analysisFilter').value;
  const sortBy   = document.getElementById('sortBy').value;
  const isTable  = document.getElementById('tableView').classList.contains('active');

  let visible = 0;

  if (!isTable) {{
    // 卡片视图筛选
    const container = document.getElementById('cardContainer');
    const cards = Array.from(container.querySelectorAll('.card'));
    cards.forEach(card => {{
      const text     = card.innerText.toLowerCase();
      const jMatch   = !journal  || card.dataset.journal === journal;
      const tMatch   = !topic    || card.dataset.topic   === topic;
      const qMatch   = !q        || text.includes(q);
      const hasBadge = card.querySelector('.badge-analyzed') !== null;
      const aMatch   = !analysis || (analysis === 'yes' ? hasBadge : !hasBadge);
      const show = jMatch && tMatch && qMatch && aMatch;
      card.style.display = show ? '' : 'none';
      if (show) visible++;
    }});
    const sorted = cards.filter(c => c.style.display !== 'none');
    sorted.sort((a, b) => sortBy === 'score'
      ? parseFloat(b.dataset.score) - parseFloat(a.dataset.score)
      : b.dataset.date.localeCompare(a.dataset.date));
    sorted.forEach(c => container.appendChild(c));
    document.getElementById('noResultsCard').style.display = visible === 0 ? 'block' : 'none';
  }} else {{
    // 表格视图筛选
    const rows = Array.from(document.querySelectorAll('#tableBody tr'));
    rows.forEach(row => {{
      const text   = row.innerText.toLowerCase();
      const jMatch = !journal  || row.dataset.journal === journal;
      const tMatch = !topic    || row.dataset.topic   === topic;
      const qMatch = !q        || text.includes(q);
      const aMatch = !analysis || row.dataset.analysis === analysis;
      const show = jMatch && tMatch && qMatch && aMatch;
      row.style.display = show ? '' : 'none';
      if (show) visible++;
    }});
    document.getElementById('noResultsTable').style.display = visible === 0 ? 'block' : 'none';
  }}

  document.getElementById('countBar').textContent = `显示 ${{visible}} 篇`;
}}

// ── 表格排序 ─────────────────────────────────────────────────
let _sortCol = -1, _sortAsc = true;
function sortTable(col) {{
  const tbody = document.getElementById('tableBody');
  const rows  = Array.from(tbody.querySelectorAll('tr'));
  const ths   = document.querySelectorAll('#mainTable th');

  if (_sortCol === col) {{ _sortAsc = !_sortAsc; }}
  else {{ _sortCol = col; _sortAsc = true; }}

  ths.forEach((th, i) => {{
    th.classList.remove('sorted-asc', 'sorted-desc');
    if (i === col) th.classList.add(_sortAsc ? 'sorted-asc' : 'sorted-desc');
  }});

  rows.sort((a, b) => {{
    // score 列优先用 data-score 属性（避免 ⭐ 符号干扰 parseFloat）
    if (col === 3) {{
      const an = parseFloat(a.dataset.score || '0');
      const bn = parseFloat(b.dataset.score || '0');
      return _sortAsc ? an - bn : bn - an;
    }}
    const av = a.cells[col]?.innerText.trim() || '';
    const bv = b.cells[col]?.innerText.trim() || '';
    const an = parseFloat(av), bn = parseFloat(bv);
    const cmp = (!isNaN(an) && !isNaN(bn)) ? an - bn : av.localeCompare(bv, 'zh');
    return _sortAsc ? cmp : -cmp;
  }});
  rows.forEach(r => tbody.appendChild(r));
}}

// ── 导出 Excel（SheetJS，纯前端）────────────────────────────
function exportExcel() {{
  const rows = Array.from(document.querySelectorAll('#tableBody tr'));
  const data = [["日期","期刊","方向","评分","标题","作者","DOI","原文链接","有AI解读"]];

  rows.forEach(row => {{
    if (row.style.display === 'none') return;
    const cells = row.cells;
    const titleAnchor = cells[4]?.querySelector('a');
    const doiAnchor   = cells[6]?.querySelector('a');
    data.push([
      cells[0]?.innerText.trim(),
      cells[1]?.innerText.trim(),
      cells[2]?.innerText.trim(),
      cells[3]?.innerText.trim(),
      titleAnchor?.innerText.trim() || cells[4]?.innerText.trim(),
      cells[5]?.innerText.trim(),
      doiAnchor?.innerText.trim() || cells[6]?.innerText.trim(),
      titleAnchor?.href || '',
      cells[7]?.innerText.trim() === '查看解读' ? '是' : '否',
    ]);
  }});

  const ws = XLSX.utils.aoa_to_sheet(data);
  // 列宽
  ws['!cols'] = [
    {{wch:12}},{{wch:25}},{{wch:20}},{{wch:8}},
    {{wch:55}},{{wch:30}},{{wch:18}},{{wch:40}},{{wch:10}}
  ];
  const wb = XLSX.utils.book_new();
  XLSX.utils.book_append_sheet(wb, ws, "相关文章");
  const date = new Date().toISOString().slice(0,10);
  XLSX.writeFile(wb, `relevant_papers_${{date}}.xlsx`);
}}
</script>
</body>
</html>"""

    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    Path(output_path).write_text(html, encoding="utf-8")
    print(f"✅ HTML 索引页已生成: {output_path} (共 {total} 篇文章)", flush=True)


# ── 主入口 ────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="统计和导出数据库文献")
    parser.add_argument("--db",        default="data/db/chem_daily.db")
    parser.add_argument("--threshold", type=int, default=5)
    parser.add_argument("--html",      default="data/output/paper_index.html")
    parser.add_argument("--search",    default="")
    parser.add_argument("--export",    default="", help="导出 CSV")
    parser.add_argument("--no-html",   action="store_true")
    args = parser.parse_args()

    try:
        conn = sqlite3.connect(args.db)
        conn.row_factory = sqlite3.Row
    except Exception as e:
        print(f"无法读取数据库: {e}")
        return

    if not args.no_html:
        build_html_index(conn, args.threshold, args.html)

    # ── 统计大盘 ─────────────────────────────────────────────
    query = "SELECT * FROM articles"
    params = []
    if args.search:
        query += " WHERE title LIKE ? OR abstract LIKE ? OR authors LIKE ? OR journal LIKE ?"
        like = f"%{args.search}%"
        params = [like, like, like, like]

    articles = conn.execute(query, params).fetchall()
    total = len(articles)

    if total == 0:
        print("数据库为空")
        conn.close()
        return

    relevant = [a for a in articles if (a["relevance"] or 0) >= args.threshold]
    analyzed = [a for a in relevant if a["analysis"]]

    print(f"\n📊 数据库统计")
    print(f"  总文章数:                {total}")
    print(f"  相关文章 (>={args.threshold}分): {len(relevant)}")
    print(f"  有 AI 解读:              {len(analyzed)}")

    journal_counter = Counter(a["journal"] for a in articles)
    print(f"\n📰 期刊分布 (Top 10):")
    for journal, count in journal_counter.most_common(10):
        print(f"  {journal}: {count}")

    if args.export:
        with open(args.export, 'w', newline='', encoding='utf-8-sig') as f:
            writer = csv.writer(f)
            writer.writerow(["ID","DOI","期刊","研究方向","AI评分","发表日期","标题","作者","链接","有AI解读"])
            for row in articles:
                article = dict(row)
                topic = classify_article(article)
                authors_clean = " / ".join(
                    a.strip() for a in
                    (article.get("authors","") or "").replace("\n",",").split(",")
                    if a.strip()
                )
                writer.writerow([
                    article.get("id"), article.get("doi"), article.get("journal"),
                    topic, article.get("relevance"), article.get("pub_date"),
                    article.get("title"), authors_clean, article.get("url"),
                    "是" if article.get("analysis") else "否",
                ])
        print(f"✅ CSV 导出完成: {args.export}")

    conn.close()


if __name__ == "__main__":
    main()
