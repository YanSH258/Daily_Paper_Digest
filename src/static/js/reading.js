/* reading.js - 阅读清单：待读 / 在读 / 已读，批量状态与导出 */
"use strict";

const Reading = {
  page: 0,
  perPage: 50,
  total: 0,
  status: "queued",
  topic: "",
  selection: new Set(),
  topicsLoaded: false,

  init() {
    const tabs = document.getElementById("readingTabs");
    tabs.addEventListener("click", (e) => {
      const btn = e.target.closest(".tab");
      if (!btn) return;
      this.status = btn.dataset.status;
      this.page = 0;
      this.selection.clear();
      tabs.querySelectorAll(".tab").forEach((t) => t.classList.toggle("active", t === btn));
      this.load();
    });
    document.getElementById("readingTopic").addEventListener("change", () => {
      this.topic = document.getElementById("readingTopic").value;
      this.page = 0;
      this.selection.clear();
      this.load();
    });
    tabs.querySelector('[data-status="queued"]').classList.add("active");
    // 方向筛选与文献库保持一致
    const src = document.getElementById("topic");
    const dst = document.getElementById("readingTopic");
    for (const opt of src.options) {
      if (opt.value === "") continue;
      const o = document.createElement("option");
      o.value = opt.value; o.textContent = opt.textContent;
      dst.appendChild(o);
    }
  },

  refresh() {
    if (document.getElementById("view-reading").hidden) return;
    this.load();
  },

  async load() {
    const summaryEl = document.getElementById("readingSummary");
    summaryEl.textContent = "查询中...";
    const params = new URLSearchParams({
      read_status: this.status,
      topic: this.topic,
      limit: String(this.perPage),
      offset: String(this.page * this.perPage),
      min_score: "0",
    });
    try {
      const data = await API.get("/api/articles?" + params.toString());
      this.total = data.total ?? data.items.length;
      this.renderList(data.items);
      this.renderPagination();
      summaryEl.textContent = `共 ${this.total} 篇`;
    } catch (e) {
      summaryEl.textContent = "查询失败: " + e.message;
    }
  },

  renderList(items) {
    const el = document.getElementById("readingList");
    el.innerHTML = "";
    if (!items.length) {
      el.innerHTML = '<div class="empty-state">这个状态下暂时没有文献。</div>';
      this.renderBatchBar();
      return;
    }
    for (const a of items) {
      const score = Number(a.relevance || 0);
      const checked = this.selection.has(a.id);
      const row = document.createElement("div");
      row.className = "reading-row";
      row.innerHTML = `
        <input type="checkbox" data-check="${a.id}" ${checked ? "checked" : ""} />
        <div class="feed-card-score ${score >= 8 ? "high" : score >= 6 ? "mid" : "low"}">${score ? score.toFixed(1) : "—"}</div>
        <div class="reading-row-main">
          <a class="reading-row-title" href="/article/${a.id}" target="_blank">${API.esc(API.cleanTitle(a.title || ""))}</a>
          <div class="small muted">
            <span class="journal-tag">${API.esc(a.journal || "")}</span>
            <span class="topic-tag">${API.esc(a.topic || "")}</span>
            ${a.has_analysis ? "✅ AI 解读" : "📄 仅摘要"}
            ${a.read_status === "read" ? " · 已读" : a.read_status === "reading" ? " · 在读" : ""}
          </div>
        </div>
        <div class="reading-row-ops">
          ${this.status !== "reading" ? `<button class="secondary small-btn" onclick="Reading.setStatus(${a.id}, 'reading')">开始读</button>` : ""}
          ${this.status !== "read" ? `<button class="secondary small-btn" onclick="Reading.setStatus(${a.id}, 'read')">已读</button>` : ""}
          <button class="secondary small-btn" onclick="Reading.setStatus(${a.id}, '')">移出</button>
          <a class="link-btn" onclick="Detail.open(${a.id})">详情</a>
        </div>
      `;
      row.querySelector("[data-check]").addEventListener("change", (e) => {
        const id = Number(e.target.dataset.check);
        if (e.target.checked) this.selection.add(id);
        else this.selection.delete(id);
        this.renderBatchBar();
      });
      el.appendChild(row);
    }
    this.renderBatchBar();
  },

  renderBatchBar() {
    const bar = document.getElementById("readingBatchBar");
    const countEl = document.getElementById("readingBatchCount");
    bar.hidden = this.selection.size === 0;
    countEl.textContent = `已选 ${this.selection.size} 篇`;
  },

  clearSelection() {
    this.selection.clear();
    this.load();
  },

  async setStatus(id, status) {
    try {
      await API.post(`/api/articles/${id}/status`, { read_status: status });
      this.selection.delete(id);
      this.load();
    } catch (e) {
      alert("更新失败: " + e.message);
    }
  },

  async batchSetStatus(status) {
    if (!this.selection.size) return;
    try {
      const resp = await API.post("/api/articles/batch", {
        ids: [...this.selection],
        read_status: status,
      });
      if (resp.ok) {
        this.selection.clear();
        this.load();
      } else {
        alert(resp.error || "批量更新失败");
      }
    } catch (e) {
      alert("批量更新失败: " + e.message);
    }
  },

  async batchExportCitation(fmt) {
    if (!this.selection.size) return;
    try {
      const res = await fetch("/api/articles/citation", {
        method: "POST",
        headers: API.headers(),
        body: JSON.stringify({ ids: [...this.selection], format: fmt }),
      });
      if (!res.ok) throw new Error("HTTP " + res.status);
      const text = await res.text();
      this.download(text, `reading.${fmt}`);
    } catch (e) {
      alert("导出失败: " + e.message);
    }
  },

  download(text, filename) {
    const blob = new Blob([text], { type: "text/plain;charset=utf-8" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = filename;
    a.click();
    URL.revokeObjectURL(a.href);
  },

  renderPagination() {
    const el = document.getElementById("readingPagination");
    const pages = Math.max(1, Math.ceil(this.total / this.perPage));
    el.innerHTML = "";
    if (pages <= 1) return;
    const prev = document.createElement("button");
    prev.className = "secondary";
    prev.textContent = "← 上一页";
    prev.disabled = this.page === 0;
    prev.onclick = () => { this.page--; this.load(); };
    const info = document.createElement("span");
    info.className = "small";
    info.textContent = `第 ${this.page + 1} / ${pages} 页`;
    const next = document.createElement("button");
    next.className = "secondary";
    next.textContent = "下一页 →";
    next.disabled = this.page >= pages - 1;
    next.onclick = () => { this.page++; this.load(); };
    el.appendChild(prev); el.appendChild(info); el.appendChild(next);
  },
};
