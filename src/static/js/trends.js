/* trends.js - 趋势视图：纯 CSS 条形图 */
"use strict";

const Trends = {
  async load() {
    try {
      const d = await API.get("/api/stats/trends?months=8");
      this.renderMonthly(d.monthly || []);
      this.renderBars("trendJournals", (d.journals || []).map((j) => ({
        label: j.journal, value: j.total,
        sub: `相关 ${j.relevant}/${j.total}`,
      })));
      this.renderBars("trendVia", (d.discovered_via || []).map((v) => ({
        label: ({ rss: "RSS 订阅", citation_watch: "引文追踪", author_watch: "作者追踪",
                  openalex_query: "OpenAlex 检索" }[v.via]) || v.via,
        value: v.count, sub: "",
      })));
      this.renderBars("trendRead", (d.read_status || []).map((r) => ({
        label: r.status, value: r.count, sub: "",
      })));
    } catch (e) {
      document.getElementById("trendMonthly").innerHTML = `<div class="err">${API.esc(e.message)}</div>`;
    }
  },

  renderMonthly(rows) {
    const el = document.getElementById("trendMonthly");
    if (!rows.length) { el.innerHTML = '<div class="empty-state">暂无数据。</div>'; return; }
    const max = Math.max(...rows.map((r) => r.total)) || 1;
    rows.reverse();
    el.innerHTML = rows.map((r) => `
      <div class="trend-row">
        <span class="trend-label mono">${API.esc(r.month)}</span>
        <div class="trend-track">
          <div class="trend-fill" style="width:${Math.round(r.total / max * 100)}%;"></div>
          <div class="trend-fill accent" style="width:${Math.round((r.relevant || 0) / max * 100)}%;"></div>
        </div>
        <span class="small muted">${r.total} 篇 / 相关 ${r.relevant || 0}</span>
      </div>`).join("");
  },

  renderBars(elId, items) {
    const el = document.getElementById(elId);
    if (!items.length) { el.innerHTML = '<div class="empty-state">暂无数据。</div>'; return; }
    const max = Math.max(...items.map((i) => i.value)) || 1;
    el.innerHTML = items.map((i) => `
      <div class="trend-row">
        <span class="trend-label">${API.esc(i.label)}</span>
        <div class="trend-track"><div class="trend-fill" style="width:${Math.round(i.value / max * 100)}%;"></div></div>
        <span class="small muted">${i.value}${i.sub ? " · " + API.esc(i.sub) : ""}</span>
      </div>`).join("");
  },
};
