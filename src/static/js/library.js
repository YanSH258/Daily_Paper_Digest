/* library.js - 文献库视图：全部/收藏 tab、筛选、星标、标签、分页 */
"use strict";

const Library = {
  page: 0,
  perPage: 50,
  total: 0,
  tab: "all",
  loadedOnce: false,
  selection: new Set(),

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

  toggleManualAdd() {
    const panel = document.getElementById("manualAddPanel");
    if (!panel) return;
    const show = panel.style.display === "none" || !panel.style.display;
    panel.style.display = show ? "block" : "none";
    if (show) document.getElementById("maDoi")?.focus();
  },

  async fetchByDoi() {
    const msg = document.getElementById("manualAddMsg");
    const doi = (document.getElementById("maDoi").value || "").trim();
    if (!doi) {
      msg.innerHTML = '<span class="err">请先填 DOI</span>';
      return;
    }
    msg.textContent = "正在从 OpenAlex 补全…";
    try {
      const res = await API.post("/api/articles/manual", { doi, dry_run: true });
      if (!res.ok) throw new Error(res.error || "补全失败");
      const a = res.article || {};
      const set = (id, v) => {
        const el = document.getElementById(id);
        if (el && v) el.value = v;
      };
      set("maTitle", a.title);
      set("maJournal", a.journal);
      set("maDate", a.pub_date);
      set("maAuthors", a.authors);
      set("maUrl", a.url);
      const abs = document.getElementById("maAbstract");
      if (abs && a.abstract) abs.value = a.abstract;
      msg.innerHTML = res.fetched_from_openalex
        ? '<span class="ok">已补全，请核对后点「加入文献库」</span>'
        : '<span class="err">OpenAlex 未找到，请手动填写后加入</span>';
    } catch (e) {
      msg.innerHTML = `<span class="err">${API.esc(e.message)}</span>`;
    }
  },

  async submitManualAdd() {
    const msg = document.getElementById("manualAddMsg");
    const val = (id) => (document.getElementById(id)?.value || "").trim();
    const body = {
      doi: val("maDoi"),
      title: val("maTitle"),
      journal: val("maJournal"),
      pub_date: val("maDate"),
      authors: val("maAuthors"),
      url: val("maUrl"),
      tags: val("maTags"),
      topic: val("maTopic"),
      abstract: document.getElementById("maAbstract")?.value || "",
    };
    if (!body.doi && !body.title) {
      msg.innerHTML = '<span class="err">至少填 DOI 或标题</span>';
      return;
    }
    msg.textContent = "添加中…";
    try {
      const res = await API.post("/api/articles/manual", body);
      if (res.duplicate) {
        msg.innerHTML = `<span class="err">库中已有该文献（id=${res.id}）</span>`;
        return;
      }
      if (!res.ok) throw new Error(res.error || "添加失败");
      msg.innerHTML = `<span class="ok">✓ 已添加 id=${res.id} · <a href="/article/${res.id}" target="_blank">打开</a></span>`;
      this._clearManualForm();
      this.page = 0;
      this.load();
    } catch (e) {
      msg.innerHTML = `<span class="err">${API.esc(e.message)}</span>`;
    }
  },

  _clearManualForm() {
    ["maDoi", "maTitle", "maJournal", "maDate", "maAuthors", "maUrl", "maTags", "maAbstract"].forEach((id) => {
      const el = document.getElementById(id);
      if (el) el.value = "";
    });
    const topic = document.getElementById("maTopic");
    if (topic) topic.value = "";
  },

  onScorePreset(val) {
    const input = document.getElementById("minScore");
    if (val === "custom") {
      input.hidden = false;
      input.focus();
    } else {
      input.hidden = true;
      input.value = val || "0";
    }
    this.page = 0;
    this.load();
  },

  currentMinScore() {
    return document.getElementById("minScore").value || "0";
  },

  async cleanupLow() {
    const minScore = Number(this.currentMinScore() || 0);
    if (!minScore || minScore <= 0) {
      alert("请先在筛选里把相关性设为 ≥3 / ≥5 / ≥7 等，再点「清理低分」。");
      return;
    }
    // Token 仅在服务端配置了 web.api_token 时才需要；未配置则直接调用
    try {
      const preview = await API.post("/api/articles/cleanup", {
        min_score: minScore,
        preview: true,
      });
      if (!preview.ok) throw new Error(preview.error || "预览失败");
      const sample = (preview.sample || [])
        .map((s) => `· [${Number(s.relevance || 0).toFixed(1)}] ${(s.title || "").slice(0, 60)}`)
        .join("\n");
      const ok = confirm(
        `将删除相关性 < ${minScore} 且无收藏/笔记/标签/Zotero/阅读状态的文献\n` +
          `共 ${preview.would_delete} 篇\n\n` +
          `${preview.protected || ""}\n\n样例：\n${sample}\n\n确认删除？此操作不可恢复（本地已有备份）。`
      );
      if (!ok) return;
      // 先导出 CSV 备份（浏览器下载 + 服务端落盘），再删除
      try {
        const csvRes = await fetch("/api/articles/cleanup/export", {
          method: "POST",
          headers: API.headers(),
          body: JSON.stringify({ min_score: minScore }),
        });
        if (csvRes.ok) {
          const blob = await csvRes.blob();
          const a = document.createElement("a");
          a.href = URL.createObjectURL(blob);
          const cd = csvRes.headers.get("Content-Disposition") || "";
          const m = /filename\*=UTF-8''([^;]+)/.exec(cd);
          a.download = m ? decodeURIComponent(m[1]) : `cleanup-backup-lt${minScore}.csv`;
          a.click();
          URL.revokeObjectURL(a.href);
        }
      } catch (e) { /* 备份失败仍继续删除，服务端可能已落盘 */ }
      const res = await API.post("/api/articles/cleanup", {
        min_score: minScore,
        preview: false,
      });
      if (!res.ok) throw new Error(res.error || "删除失败");
      alert(`已备份并删除 ${res.deleted} 篇（备份见浏览器下载与 data/output/）`);
      this.page = 0;
      this.load();
    } catch (e) {
      const msg = String(e.message || e);
      if (/unauthorized|401/i.test(msg)) {
        alert("服务端已开启 API Token 保护：请在左下角填入与设置里相同的 Token 后再清理。");
      } else {
        alert("清理失败: " + msg);
      }
    }
  },

  async translateTitles() {
    const btn = document.getElementById("translateBtn");
    const items = this.lastItems || [];
    if (!items.length) {
      alert("当前页没有文献，请先加载文献库");
      return;
    }
    if (!confirm(`用免费 MyMemory 接口翻译当前页 ${items.length} 条标题？（有日配额，失败会自动重试一次）`)) return;
    if (btn) {
      btn.disabled = true;
      btn.textContent = "翻译中…";
    }
    try {
      const res = await API.post("/api/articles/translate", {
        ids: items.map((a) => a.id),
        limit: items.length,
        provider: "mymemory",
      });
      if (!res.ok) throw new Error(res.error || "翻译失败");
      alert(`当前页：新译 ${res.translated} · 已有/跳过 ${res.skipped} · 失败 ${res.failed}`);
      if (res.failed > 0) {
        alert("仍有失败条目，可再点一次「译标题」补译。");
      }
      this.load();
    } catch (e) {
      alert("翻译失败: " + e.message);
    } finally {
      if (btn) {
        btn.disabled = false;
        btn.textContent = "译标题";
      }
    }
  },

  journalMeta(a) {
    const parts = [];
    parts.push(API.esc(a.journal || "未知期刊"));
    if (a.impact_factor != null && a.impact_factor !== "") {
      parts.push(`IF ${Number(a.impact_factor).toFixed(1)}`);
    }
    // cas_zone 存 JCR 四分位 1-4，显示为 Q1…；无则退回「N区」
    if (a.cas_zone) {
      parts.push(`Q${a.cas_zone}`);
    }
    return parts;
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
    const readStatus = document.getElementById("readStatusFilter").value;
    const minScore = document.getElementById("minScore").value || "0";
    const analyzedOnly = document.getElementById("analyzedOnly").checked ? "true" : "false";
    const params = new URLSearchParams({
      q, journal, topic, tag, min_score: minScore, analyzed_only: analyzedOnly,
      read_status: readStatus,
      starred: this.tab === "starred" ? "true" : "false",
      limit: String(this.perPage),
      offset: String(this.page * this.perPage),
    });

    const summaryEl = document.getElementById("summary");
    summaryEl.textContent = "查询中...";
    try {
      try { history.replaceState(null, "", "#" + params.toString()); } catch (e) {}
    const data = await API.get("/api/articles?" + params.toString());
      this.total = data.total ?? data.items.length;
      this.lastItems = data.items || [];
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

  evidenceBadge(a) {
    if (a.evidence_level === "FULLTEXT") return '<span class="ev-badge ev-full">全文依据</span>';
    if (a.evidence_level === "ABSTRACT_ONLY") return '<span class="ev-badge ev-abstract">摘要依据</span>';
    return '<span class="ev-badge ev-unknown">依据未知</span>';
  },

  statusChip(a) {
    const map = { queued: ["待读", "st-queued"], reading: ["在读", "st-reading"], read: ["已读", "st-read"] };
    const s = map[a.read_status];
    return s ? `<span class="status-chip ${s[1]}">${s[0]}</span>` : "";
  },

  renderCards(items) {
    const container = document.getElementById("articleCardContainer");
    if (!container) return;
    container.innerHTML = "";
    if (!items.length) {
      container.innerHTML = '<div class="empty-state">没有符合条件的文献。</div>';
      this.renderBatchBar();
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
            <span class="journal-tag">${this.journalMeta(a).map((p, i) =>
              i === 0 ? p : `<span class="jmetric">${p}</span>`
            ).join(" ")}</span>
            <span class="topic-tag">${API.esc(a.topic || "未分类")}</span>
            <span class="date-tag">${API.esc(a.pub_date || "")}</span>
            ${this.statusChip(a)}
          </div>
          <button class="star-btn ${a.starred ? "on" : ""}" data-star="${a.id}" data-val="${a.starred ? 1 : 0}"
            title="${a.starred ? "取消收藏" : "收藏"}">${a.starred ? "★" : "☆"}</button>
        </div>
        <div class="feed-card-title title-link" data-id="${a.id}">${
          a.title_zh ? `<div class="title-zh">${API.esc(a.title_zh)}</div>` : ""
        }${API.esc(API.cleanTitle(a.title || ""))}</div>
        ${a.relevance_reason ? `<div class="today-reason"><b>推荐理由：</b>${API.esc(a.relevance_reason)}</div>` : ""}
        <div class="feed-card-authors">${API.esc(a.authors || "-")}</div>
        <div class="feed-card-foot">
          <div class="feed-card-tags">${tagBadges}</div>
          <div class="feed-card-actions">
            ${badge}
            ${this.evidenceBadge(a)}
            <a class="ext-link" href="/article/${a.id}" target="_blank">阅读页</a>
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
    this.renderBatchBar();
  },

  renderRows(items) {
    const tbody = document.querySelector("#articleTable tbody");
    tbody.innerHTML = "";
    if (!items.length) {
      tbody.innerHTML = '<tr><td colspan="10" class="small">没有符合条件的文献。</td></tr>';
      return;
    }
    for (const a of items) {
      const tr = document.createElement("tr");
      const tags = (a.tags || "").split(",").map((t) => t.trim()).filter(Boolean);
      const tagBadges = tags.map((t) => `<span class="tag-badge">${API.esc(t)}</span>`).join("");
      const statusMap = { queued: "待读", reading: "在读", read: "已读" };
      tr.innerHTML = `
        <td><input type="checkbox" data-check="${a.id}" ${this.selection.has(a.id) ? "checked" : ""} /></td>
        <td><button class="star-btn ${a.starred ? "on" : ""}" data-star="${a.id}" data-val="${a.starred ? 1 : 0}"
          title="${a.starred ? "取消收藏" : "收藏"}">${a.starred ? "★" : "☆"}</button></td>
        <td>${Number(a.relevance || 0).toFixed(1)}</td>
        <td>${API.esc(a.topic || "")}</td>
        <td>${API.esc(a.journal || "")}${a.impact_factor != null && a.impact_factor !== ""
          ? `<div class="small muted">IF ${Number(a.impact_factor).toFixed(1)}${a.cas_zone ? " · Q" + a.cas_zone : ""}</div>`
          : ""}</td>
        <td><span class="title-link" data-id="${a.id}">${
          a.title_zh ? `<div class="title-zh">${API.esc(a.title_zh)}</div>` : ""
        }${API.esc(API.cleanTitle(a.title || ""))}</span></td>
        <td>${tagBadges}</td>
        <td>${statusMap[a.read_status] || "—"}</td>
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
    tbody.querySelectorAll("input[data-check]").forEach((el) => {
      el.addEventListener("change", () => {
        const id = Number(el.dataset.check);
        if (el.checked) this.selection.add(id); else this.selection.delete(id);
        this.renderBatchBar();
      });
    });
  },

  toggleCheckAll(el) {
    const boxes = document.querySelectorAll("#articleTable tbody input[data-check]");
    boxes.forEach((b) => {
      b.checked = el.checked;
      const id = Number(b.dataset.check);
      if (el.checked) this.selection.add(id); else this.selection.delete(id);
    });
    this.renderBatchBar();
  },

  clearSelection() {
    this.selection.clear();
    this.load();
  },

  renderBatchBar() {
    const bar = document.getElementById("batchBar");
    if (!bar) return;
    bar.hidden = this.selection.size === 0;
    const countEl = document.getElementById("batchCount");
    if (countEl) countEl.textContent = `已选 ${this.selection.size} 篇`;
  },

  async batchSetStatus(status) {
    if (!this.selection.size) return;
    try {
      const resp = await API.post("/api/articles/batch", { ids: [...this.selection], read_status: status });
      if (resp.ok) { this.selection.clear(); this.load(); }
      else alert(resp.error || "批量更新失败");
    } catch (e) { alert("批量更新失败: " + e.message); }
  },

  async batchPushZotero() {
    if (!this.selection.size) return;
    if (!confirm(`确定把选中的 ${this.selection.size} 篇推送到 Zotero 吗？`)) return;
    try {
      const resp = await API.post("/api/zotero/batch", { ids: [...this.selection] });
      alert(`推送完成：成功 ${resp.pushed}，已存在 ${resp.skipped}` +
            (resp.failed.length ? `，失败 ${resp.failed.length}` : ""));
      this.load();
    } catch (e) { alert("推送失败: " + e.message); }
  },

  async batchAddToTopic() {
    const tid = document.getElementById("batchTopicSelect").value;
    if (!tid) return alert("请先在批量栏选择专题");
    if (!this.selection.size) return;
    try {
      const resp = await API.post(`/api/topics/${tid}/papers`, { ids: [...this.selection] });
      if (resp.ok) { alert(`已加入专题（新增 ${resp.added} 篇）`); this.load(); }
      else alert(resp.error || "加入失败");
    } catch (e) { alert("加入失败: " + e.message); }
  },

  async runCompare() {
    if (this.selection.size < 2 || this.selection.size > 4)
      return alert("AI 对比需要选择 2-4 篇文献");
    if (!confirm("将调用 LLM 生成对比（约 30-60 秒），继续？")) return;
    try {
      const resp = await API.post("/api/compare", { ids: [...this.selection] });
      if (resp.ok) window.open("/results/" + resp.id, "_blank");
      else alert(resp.error || "生成失败");
    } catch (e) { alert("生成失败: " + e.message); }
  },

  async runRelatedWork() {
    if (this.selection.size < 2) return alert("草稿段落需要至少选择 2 篇文献");
    const focus = prompt("写作侧重（可留空）", "") ;
    if (focus === null) return;
    try {
      const resp = await API.post("/api/related-work", { ids: [...this.selection], focus });
      if (resp.ok) window.open("/results/" + resp.id, "_blank");
      else alert(resp.error || "生成失败");
    } catch (e) { alert("生成失败: " + e.message); }
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
      const blob = new Blob([text], { type: "text/plain;charset=utf-8" });
      const a = document.createElement("a");
      a.href = URL.createObjectURL(blob);
      a.download = `selection.${fmt}`;
      a.click();
      URL.revokeObjectURL(a.href);
    } catch (e) { alert("导出失败: " + e.message); }
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
