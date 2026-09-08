/* today.js - 今日精选：按评分分桶（优先阅读 / 值得关注 / 快速浏览） */
"use strict";

const Today = {
  data: null,

  init() {
    // 使用本地日期（toISOString 是 UTC，跨午夜时会与库中 localtime 日期错位一天）
    const d = new Date();
    const local = `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
    document.getElementById("todayDate").value = local;
  },

  async load() {
    const errEl = document.getElementById("todayError");
    errEl.hidden = true;
    const date = document.getElementById("todayDate").value;
    try {
      this.data = await API.get("/api/today?date=" + encodeURIComponent(date));
      this.render();
    } catch (e) {
      errEl.textContent = "今日数据加载失败: " + e.message;
      errEl.hidden = false;
    }
  },

  evidenceBadge(a) {
    if (a.evidence_level === "FULLTEXT") return '<span class="ev-badge ev-full">全文依据</span>';
    if (a.evidence_level === "ABSTRACT_ONLY") return '<span class="ev-badge ev-abstract">摘要依据</span>';
    if (a.evidence_level === "UNKNOWN") return '<span class="ev-badge ev-unknown">依据未知</span>';
    return '<span class="ev-badge ev-unknown">摘要依据</span>';
  },

  statusChip(a) {
    const map = { queued: ["待读", "st-queued"], reading: ["在读", "st-reading"], read: ["已读", "st-read"] };
    const s = map[a.read_status];
    return s ? `<span class="status-chip ${s[1]}">${s[0]}</span>` : "";
  },

  async queueAdd(id, btn) {
    try {
      await API.post(`/api/articles/${id}/status`, { read_status: "queued" });
      if (btn) { btn.textContent = "✓ 已加入清单"; btn.disabled = true; }
      const item = this.findItem(id);
      if (item) item.read_status = "queued";
    } catch (e) {
      alert("加入清单失败: " + e.message);
    }
  },

  findItem(id) {
    if (!this.data) return null;
    for (const key of ["top", "notable", "browse", "processing"]) {
      const hit = (this.data.buckets?.[key] || this.data[key] || []).find((a) => String(a.id) === String(id));
      if (hit) return hit;
    }
    return null;
  },

  render() {
    const d = this.data;
    document.getElementById("todayTitle").textContent = `今日精选 · ${d.date}`;

    const c = d.counts || {};
    const task = d.task || {};
    document.getElementById("todayOverview").innerHTML = `
      <div class="today-stat"><b>${d.total}</b><span>今日入库</span></div>
      <div class="today-stat top"><b>${c.top || 0}</b><span>优先阅读</span></div>
      <div class="today-stat"><b>${c.notable || 0}</b><span>值得关注</span></div>
      <div class="today-stat"><b>${c.browse || 0}</b><span>快速浏览</span></div>
      <div class="today-stat"><b>${c.processing || 0}</b><span>处理中/待重试</span></div>
      <div class="today-stat muted"><b>${task.running ? "运行中" : "空闲"}</b><span>任务状态</span></div>
    `;

    this.renderBucket("todayTop", d.buckets.top, "card");
    this.renderBucket("todayNotable", d.buckets.notable, "card");
    this.renderBucket("todayBrowse", d.buckets.browse, "compact");
    this.renderBucket("todayProcessing", d.processing, "compact");

    document.getElementById("todayTopWrap").hidden = !d.buckets.top.length;
    document.getElementById("todayNotableWrap").hidden = !d.buckets.notable.length;
    document.getElementById("todayBrowseWrap").hidden = !d.buckets.browse.length;
    document.getElementById("todayProcessingWrap").hidden = !d.processing.length;
  },

  renderBucket(elId, items, mode) {
    const el = document.getElementById(elId);
    el.innerHTML = "";
    if (!items || !items.length) {
      el.innerHTML = '<div class="empty-state small">本档今日没有文章。</div>';
      return;
    }
    for (const a of items) {
      const score = Number(a.relevance || 0);
      if (mode === "card") {
        const div = document.createElement("div");
        div.className = "today-card";
        div.innerHTML = `
          <div class="today-card-head">
            <div class="feed-card-score ${score >= 8 ? "high" : "mid"}">★ ${score.toFixed(1)}</div>
            <div class="feed-card-meta">
              <span class="journal-tag">${API.esc(a.journal || "未知期刊")}</span>
              <span class="topic-tag">${API.esc(a.topic || "未分类")}</span>
              ${this.statusChip(a)}
            </div>
            <button class="star-btn ${a.starred ? "on" : ""}" onclick="Detail.open(${a.id})" title="打开详情">→</button>
          </div>
          <div class="today-card-title"><a href="/article/${a.id}" target="_blank">${API.esc(API.cleanTitle(a.title || ""))}</a></div>
          ${a.relevance_reason ? `<div class="today-reason"><b>推荐理由：</b>${API.esc(a.relevance_reason)}</div>` : ""}
          <div class="today-card-foot">
            ${this.evidenceBadge(a)}
            ${a.has_analysis ? '<a class="link-btn" onclick="Detail.open(' + a.id + ')">查看解读</a>' : ""}
            <button class="secondary small-btn" onclick="Today.queueAdd(${a.id}, this)" ${a.read_status ? "disabled" : ""}>
              ${a.read_status === "queued" ? "✓ 待读中" : a.read_status === "read" ? "✓ 已读" : "＋ 加入清单"}
            </button>
            ${a.url ? `<a class="ext-link" href="${API.esc(a.url)}" target="_blank" rel="noopener">原文 ↗</a>` : ""}
          </div>
        `;
        el.appendChild(div);
      } else {
        const row = document.createElement("div");
        row.className = "today-row";
        row.innerHTML = `
          <span class="small muted">${score ? score.toFixed(1) : "—"}</span>
          <a class="today-row-title" href="/article/${a.id}" target="_blank">${API.esc(API.cleanTitle(a.title || ""))}</a>
          <span class="journal-tag">${API.esc(a.journal || "")}</span>
          ${a.score_status === "failed" ? '<span class="ev-badge ev-unknown">评分失败·将重试</span>' : this.evidenceBadge(a)}
        `;
        el.appendChild(row);
      }
    }
  },
};
