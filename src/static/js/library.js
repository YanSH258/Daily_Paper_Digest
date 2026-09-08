/* library.js - 文献库视图：全部/收藏 tab、筛选、星标、标签、分页 */
"use strict";

const Library = {
  page: 0,
  perPage: 50,
  total: 0,
  tab: "all",
  loadedOnce: false,

  init() {
    const tabs = document.getElementById("libraryTabs");
    tabs.addEventListener("click", (e) => {
      const btn = e.target.closest(".tab");
      if (!btn) return;
      this.tab = btn.dataset.tab;
      tabs.querySelectorAll(".tab").forEach((t) => t.classList.toggle("active", t === btn));
      this.page = 0;
      this.load();
    });
    // 回车即查询
    document.getElementById("q").addEventListener("keydown", (e) => {
      if (e.key === "Enter") this.search();
    });
  },

  search() {
    this.page = 0;
    this.load();
  },

  refresh() {
    if (document.getElementById("view-database").hidden) return;
    this.load();
  },

  async loadTags() {
    const sel = document.getElementById("tagFilter");
    const current = sel.value;
    try {
      const data = await API.get("/api/tags");
      sel.innerHTML = '<option value="">全部标签</option>';
      for (const t of data.items || []) {
        const opt = document.createElement("option");
        opt.value = t.tag;
        opt.textContent = `${t.tag} (${t.count})`;
        sel.appendChild(opt);
      }
      if ([...sel.options].some((o) => o.value === current)) sel.value = current;
    } catch (e) { /* 标签下拉加载失败不阻塞列表 */ }
  },

  viewMode: localStorage.getItem("dsh_view_mode") || "card",

  setViewMode(mode) {
    this.viewMode = mode;
    localStorage.setItem("dsh_view_mode", mode);
    const cardBtn = document.getElementById("viewModeCard");
    const tableBtn = document.getElementById("viewModeTable");
    const cardWrap = document.getElementById("articleCardContainer");
    const tableWrap = document.getElementById("articleTableWrap");

    if (cardBtn) cardBtn.classList.toggle("active", mode === "card");
    if (tableBtn) tableBtn.classList.toggle("active", mode === "table");
    if (cardWrap) cardWrap.style.display = (mode === "card" ? "grid" : "none");
    if (tableWrap) tableWrap.style.display = (mode === "table" ? "block" : "none");
  },

  async load() {
    this.loadedOnce = true;
    this.setViewMode(this.viewMode);
    const q = document.getElementById("q").value.trim();
    const journal = document.getElementById("journal").value;
    const topic = document.getElementById("topic").value;
    const tag = document.getElementById("tagFilter").value;
    const minScore = document.getElementById("minScore").value || "0";
    const analyzedOnly = document.getElementById("analyzedOnly").checked ? "true" : "false";
    const params = new URLSearchParams({
      q, journal, topic, tag, min_score: minScore, analyzed_only: analyzedOnly,
      starred: this.tab === "starred" ? "true" : "false",
      limit: String(this.perPage),
      offset: String(this.page * this.perPage),
    });

    const summaryEl = document.getElementById("summary");
    summaryEl.textContent = "查询中...";
    try {
      const data = await API.get("/api/articles?" + params.toString());
      this.total = data.total ?? data.items.length;
      this.renderCards(data.items);
      this.renderRows(data.items);
      this.renderJournals(data.journals);
      this.renderPagination();
      summaryEl.textContent = `共 ${this.total} 篇` +
        (this.total > this.perPage ? ` · 第 ${this.page + 1}/${Math.ceil(this.total / this.perPage)} 页` : "");
    } catch (e) {
      summaryEl.textContent = "查询失败: " + e.message;
    }
  },

  renderCards(items) {
    const container = document.getElementById("articleCardContainer");
    if (!container) return;
    container.innerHTML = "";
    if (!items.length) {
      container.innerHTML = '<div class="empty-state">没有符合条件的文献。</div>';
      return;
    }
    for (const a of items) {
      const tags = (a.tags || "").split(",").map((t) => t.trim()).filter(Boolean);
      const tagBadges = tags.map((t) => `<span class="tag-badge">${API.esc(t)}</span>`).join("");
      const score = Number(a.relevance || 0);
      const scoreClass = score >= 8 ? "high" : (score >= 6 ? "mid" : "low");
      const hasAnalysis = Boolean(a.has_analysis || a.analysis);
      const badge = hasAnalysis ? '<span class="badge green">AI 解读</span>' : '<span class="badge muted">仅摘要</span>';
      
      const card = document.createElement("div");
      card.className = "article-feed-card";
      card.innerHTML = `
        <div class="feed-card-head">
          <div class="feed-card-score ${scoreClass}">★ ${score.toFixed(1)}</div>
          <div class="feed-card-meta">
            <span class="journal-tag">${API.esc(a.journal || "未知期刊")}</span>
            <span class="topic-tag">${API.esc(a.topic || "未分类")}</span>
            <span class="date-tag">${API.esc(a.pub_date || "")}</span>
          </div>
          <button class="star-btn ${a.starred ? "on" : ""}" data-star="${a.id}" data-val="${a.starred ? 1 : 0}"
            title="${a.starred ? "取消收藏" : "收藏"}">${a.starred ? "★" : "☆"}</button>
        </div>
        <div class="feed-card-title title-link" data-id="${a.id}">${API.esc(API.cleanTitle(a.title || ""))}</div>
        <div class="feed-card-authors">${API.esc(a.authors || "-")}</div>
        <div class="feed-card-foot">
          <div class="feed-card-tags">${tagBadges}</div>
          <div class="feed-card-actions">
            ${badge}
            ${a.doi ? `<a href="https://doi.org/${API.esc(a.doi)}" target="_blank" rel="noopener" class="ext-link" onclick="event.stopPropagation()">DOI ↗</a>` : ""}
            ${a.url ? `<a href="${API.esc(a.url)}" target="_blank" rel="noopener" class="ext-link" onclick="event.stopPropagation()">原文 ↗</a>` : ""}
          </div>
        </div>
      `;
      container.appendChild(card);
    }
    container.querySelectorAll(".title-link[data-id]").forEach((el) => {
      el.addEventListener("click", () => Detail.open(el.dataset.id));
    });
    container.querySelectorAll("button[data-star]").forEach((el) => {
      el.addEventListener("click", (e) => {
        e.stopPropagation();
        this.toggleStar(el);
      });
    });
  },

  renderRows(items) {
    const tbody = document.querySelector("#articleTable tbody");
    tbody.innerHTML = "";
    if (!items.length) {
      tbody.innerHTML = '<tr><td colspan="8" class="small">没有符合条件的文献。</td></tr>';
      return;
    }
    for (const a of items) {
      const tr = document.createElement("tr");
      const tags = (a.tags || "").split(",").map((t) => t.trim()).filter(Boolean);
      const tagBadges = tags.map((t) => `<span class="tag-badge">${API.esc(t)}</span>`).join("");
      tr.innerHTML = `
        <td><button class="star-btn ${a.starred ? "on" : ""}" data-star="${a.id}" data-val="${a.starred ? 1 : 0}"
          title="${a.starred ? "取消收藏" : "收藏"}">${a.starred ? "★" : "☆"}</button></td>
        <td>${Number(a.relevance || 0).toFixed(1)}</td>
        <td>${API.esc(a.topic || "")}</td>
        <td>${API.esc(a.journal || "")}</td>
        <td><span class="title-link" data-id="${a.id}">${API.esc(API.cleanTitle(a.title || ""))}</span></td>
        <td>${tagBadges}</td>
        <td>${API.esc(a.pub_date || "")}</td>
        <td>${a.has_analysis ? '<span class="dot green"></span>' : '<span class="dot muted"></span>'}</td>
      `;
      tbody.appendChild(tr);
    }
    tbody.querySelectorAll(".title-link[data-id]").forEach((el) => {
      el.addEventListener("click", () => Detail.open(el.dataset.id));
    });
    tbody.querySelectorAll("button[data-star]").forEach((el) => {
      el.addEventListener("click", () => this.toggleStar(el));
    });
  },

  renderJournals(journals) {
    if (!journals) return;
    const sel = document.getElementById("journal");
    const current = sel.value;
    sel.innerHTML = '<option value="">全部期刊</option>';
    for (const j of journals) {
      const opt = document.createElement("option");
      opt.value = j;
      opt.textContent = j;
      if (j === current) opt.selected = true;
      sel.appendChild(opt);
    }
  },

  renderPagination() {
    const el = document.getElementById("pagination");
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
    info.textContent = `第 ${this.page + 1} / ${pages} 页 · 共 ${this.total} 篇`;

    const next = document.createElement("button");
    next.className = "secondary";
    next.textContent = "下一页 →";
    next.disabled = this.page >= pages - 1;
    next.onclick = () => { this.page++; this.load(); };

    el.appendChild(prev);
    el.appendChild(info);
    el.appendChild(next);
  },

  async toggleStar(el) {
    const id = el.dataset.star;
    const newVal = el.dataset.val !== "1";
    try {
      const resp = await API.post(`/api/articles/${id}/star`, { starred: newVal });
      if (!resp.ok) throw new Error(resp.error || "失败");
      el.dataset.val = newVal ? "1" : "0";
      el.classList.toggle("on", newVal);
      el.textContent = newVal ? "★" : "☆";
      // 收藏 tab 下取消收藏应移除该行
      if (this.tab === "starred" && !newVal) this.load();
    } catch (e) {
      alert("操作失败: " + e.message);
    }
  },
};
