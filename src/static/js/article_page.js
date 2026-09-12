/* article_page.js - 完整文献阅读页：结构化解读 + 阅读管理 + 笔记 + 问答 */
"use strict";

const Article = {
  id: null,
  a: null,

  init() {
    const m = location.pathname.match(/\/article\/(\d+)/);
    if (!m) {
      document.getElementById("loading").textContent = "无效的文章链接";
      return;
    }
    this.id = m[1];
    this.load();
  },

  async load() {
    try {
      const a = await API.get("/api/articles/" + this.id);
      this.a = a;
      document.getElementById("loading").hidden = true;
      document.getElementById("layout").hidden = false;
      this.render(a);
      this.loadRelated(a);
      this.loadJournal(a);
      this.loadWatchState();
      this.loadHighlights();
      Chat.init(a.id);
    } catch (e) {
      document.getElementById("loading").innerHTML = `<span class="err">加载失败: ${API.esc(e.message)}</span>`;
    }
  },

  toast(msg) {
    const t = document.getElementById("toast");
    t.textContent = msg;
    t.classList.add("show");
    setTimeout(() => t.classList.remove("show"), 1800);
  },

  render(a) {
    document.title = `${API.cleanTitle(a.title || "文献")} · 文献阅读`;
    document.getElementById("aTitle").textContent = API.cleanTitle(a.title || "无标题");

    document.getElementById("aMeta").innerHTML =
      `<b>期刊：</b>${API.esc(a.journal || "-")} · <b>日期：</b>${API.esc(a.pub_date || "-")}` +
      ` · <b>作者：</b>${API.esc(a.authors || "-")}` +
      (a.doi ? ` · <b>DOI：</b><a href="https://doi.org/${API.esc(a.doi)}" target="_blank" rel="noopener">${API.esc(a.doi)}</a>` : "");

    const translated = document.getElementById("aTitleZh");
    translated.hidden = !a.title_zh;
    translated.textContent = a.title_zh ? a.title_zh + " · 机器翻译" : "";
    const score = Number(a.relevance || 0);
    document.getElementById("aPills").innerHTML = `
      <span class="pill blue">${API.esc(a.topic || "未分类")}</span>
      ${score ? `<span class="pill">⭐ ${score.toFixed(1)} / 10</span>` : ""}
      ${a.evidence_level === "FULLTEXT"
        ? '<span class="ev-badge ev-full">全文依据</span>'
        : a.evidence_level === "ABSTRACT_ONLY"
          ? '<span class="ev-badge ev-abstract">摘要依据</span>'
          : '<span class="ev-badge ev-unknown">依据未知</span>'}
      ${a.has_analysis ? "" : '<span class="pill">暂无 AI 解读</span>'}
      ${a.score_status === "failed" ? '<span class="pill">评分失败·将自动重试</span>' : ""}
      ${a.url ? `<a href="${API.esc(a.url)}" target="_blank" rel="noopener">原文 ↗</a>` : ""}
    `;

    document.getElementById("aReason").textContent = a.relevance_reason || "暂无研究关联依据，暂不判断对你课题的适用性。";
    document.getElementById("analysisEvidence").textContent = a.evidence_level === "FULLTEXT"
      ? "AI 解读 · 基于全文，请结合原文核实" : a.evidence_level === "ABSTRACT_ONLY"
      ? "AI 解读 · 基于摘要" : "AI 解读 · 依据范围未确认";

    // 操作行
    const fb = a.relevance_feedback || "";
    document.getElementById("aActions").innerHTML = `
      <button class="primary" onclick="Article.queue()" ${a.read_status ? "disabled" : ""}>
        ${a.read_status === "read" ? "✓ 已读" : a.read_status === "reading" ? "✓ 在读" : a.read_status === "queued" ? "✓ 待读中" : "＋ 加入阅读清单"}
      </button>
      ${a.url ? `<a class="ext-link" href="${API.esc(a.url)}" target="_blank" rel="noopener"><button class="secondary">阅读原文 ↗</button></a>` : ""}
      ${fb === "relevant" ? '<span class="pill">已标记：有用 👍</span>' : fb === "irrelevant" ? '<span class="pill">已标记：不相关 👎</span>' : ""}
    `;

    // 阅读状态下拉
    const sel = document.getElementById("readStatus");
    sel.innerHTML = [["", "未加入清单"], ["queued", "📖 待读"], ["reading", "🔍 在读"], ["read", "✅ 已读"]]
      .map(([v, label]) => `<option value="${v}" ${(a.read_status || "") === v ? "selected" : ""}>${label}</option>`).join("");

    const starBtn = document.getElementById("starBtn");
    starBtn.textContent = a.starred ? "★ 已收藏" : "☆ 收藏";
    document.getElementById("feedbackNote").textContent =
      fb === "relevant" ? "已标记为相关，推荐评估会参考。" : fb === "irrelevant" ? "已标记为不相关。" : "";

    document.getElementById("analysisBody").innerHTML = a.analysis
      ? MarkdownLite.render(a.analysis) : '<p class="muted">暂无解读。阅读摘要和原文后再判断研究价值。</p>';

    // 摘要
    document.getElementById("abstractBody").innerHTML =
      `<div class="md">${API.esc(a.abstract || "暂无摘要内容。")}</div>`;

    // 全文节选
    if (a.fulltext_text) {
      document.getElementById("fulltextCard").hidden = false;
      document.getElementById("fulltextBox").textContent =
        a.fulltext_text.slice(0, 20000) + (a.fulltext_text.length > 20000 ? "\n...（已截断显示）" : "");
    }

    // 标签与笔记
    const tags = (a.tags || "").split(",").map((t) => t.trim()).filter(Boolean);
    document.getElementById("tagBadges").innerHTML = tags.length
      ? tags.map((t) => `<span class="tag-badge">${API.esc(t)}</span>`).join("")
      : '<span class="small muted">暂无标签</span>';
    document.getElementById("tagsInput").value = tags.join(", ");
    document.getElementById("noteArea").value = a.note || "";

    // Zotero 状态
    const zBtn = document.getElementById("zoteroBtn");
    if (a.zotero_key) {
      zBtn.textContent = "✓ 已在 Zotero";
      document.getElementById("zoteroNote").textContent = "条目 key: " + a.zotero_key;
    }
  },

  async loadJournal(a) {
    const box = document.getElementById("journalInfo");
    box.textContent = a.journal || "期刊信息未提供";
    try {
      const data = await API.get("/api/journal-metrics");
      const metric = (data.items || []).find(m => m.name === a.journal);
      if (!metric) return;
      box.innerHTML = `<strong>${API.esc(a.journal)}</strong>` +
        `<p class="small">影响因子：${API.esc(metric.if_value ?? "未提供")} · 分区：${API.esc(metric.cas_zone ?? "未提供")}</p>` +
        '<p class="small muted">本地期刊指标；指标年份未确认</p>';
    } catch (e) { box.append(document.createTextNode("（指标暂不可用）")); }
  },

  async loadRelated(a) {
    const box = document.getElementById("relatedArticles");
    if (!a.topic) { box.textContent = "尚无研究方向，暂无相关文献。"; return; }
    try {
      const data = await API.get("/api/articles?" + new URLSearchParams({topic: a.topic, limit: "6", min_score: "0"}));
      const items = (data.items || []).filter(item => Number(item.id) !== Number(a.id)).slice(0, 5);
      box.innerHTML = items.length ? items.map(item =>
        `<a class="related-item" href="/article/${Number(item.id)}">${API.esc(API.cleanTitle(item.title || "无标题"))}<small>同研究方向：${API.esc(a.topic)}</small></a>`
      ).join("") : '<p class="small muted">库内暂无同方向文献。</p>';
    } catch (e) {
      box.innerHTML = '<p class="small muted">相关文献加载失败。</p><button onclick="Article.loadRelated(Article.a)">重试</button>';
    }
  },

  splitSections(md) {
    const lines = String(md).split("\n");
    const sections = [];
    let cur = null;
    for (const line of lines) {
      const m = line.match(/^###\s+(.*)$/);
      if (m) {
        if (cur) sections.push(cur);
        cur = { title: m[1].trim(), body: "" };
      } else if (cur) {
        cur.body += line + "\n";
      }
    }
    if (cur) sections.push(cur);
    return sections;
  },

  async loadWatchState() {
    try {
      const data = await API.get(`/api/articles/${this.id}`);
      this.watched = !!data.watched;
      const cb = document.getElementById("watchCitations");
      if (cb) cb.checked = this.watched;
    } catch (e) { /* 忽略 */ }
  },

  async toggleWatch(active) {
    try { await API.post(`/api/articles/${this.id}/watch`, { active }); }
    catch (e) { alert("操作失败: " + e.message); }
  },

  async pushZotero() {
    try {
      const resp = await API.post(`/api/articles/${this.id}/zotero`, {});
      if (resp.ok) {
        this.a.zotero_key = resp.key;
        document.getElementById("zoteroBtn").textContent = resp.already ? "✓ 已在 Zotero" : "✓ 已推送";
        document.getElementById("zoteroNote").textContent = "条目 key: " + resp.key;
      } else alert(resp.error || "推送失败");
    } catch (e) { alert("推送失败: " + e.message); }
  },

  renderHighlights(items) {
    const box = document.getElementById("hlList");
    if (!box) return;
    box.innerHTML = items.length
      ? items.map((h) => `<div class="reading-row" style="align-items:flex-start;">
            <div class="reading-row-main"><div class="small">“${API.esc(h.text.slice(0, 160))}”</div>
            ${h.note ? `<div class="small muted">${API.esc(h.note)}</div>` : ""}</div>
            <button class="secondary" onclick="Article.removeHighlight(${h.id})">删</button>
          </div>`).join("")
      : '<div class="small muted">在上方全文节选中选中文字即可收藏。</div>';
  },

  async loadHighlights() {
    try {
      const data = await API.get(`/api/articles/${this.id}/highlights`);
      this.renderHighlights(data.items || []);
    } catch (e) { /* 忽略 */ }
  },

  async removeHighlight(id) {
    try { await API.del("/api/highlights/" + id); this.loadHighlights(); }
    catch (e) { alert("删除失败: " + e.message); }
  },

  async saveSelection() {
    const sel = window.getSelection().toString().trim();
    if (!sel) return;
    try {
      const resp = await API.post(`/api/articles/${this.id}/highlights`, { text: sel });
      if (resp.ok) {
        document.getElementById("hlSaveBtn").hidden = true;
        this.loadHighlights();
      } else alert(resp.error || "收藏失败");
    } catch (e) { alert("收藏失败: " + e.message); }
  },

  async reanalyze(fetchFulltext = false) {
    const a = this.a;
    if (!a) return;
    if (fetchFulltext && !confirm("先获取全文（arXiv/OA 自动下载）再做 AI 解读？将调用 LLM，约 30-120 秒。")) return;
    const btn = document.getElementById("reanalyzeBtn");
    const msg = document.getElementById("reanalyzeMsg");
    btn.disabled = true;
    msg.textContent = "解读运行中…";
    try {
      const resp = await API.post(`/api/articles/${this.id}/reanalyze`, { fetch_fulltext: fetchFulltext });
      if (resp.ok) {
        this.a = resp.item;
        this.render(this.a);
        msg.textContent = `✓ 完成（${Math.round(resp.latency_ms / 1000)}s）`;
      } else {
        msg.textContent = "✗ " + (resp.error || "失败");
      }
    } catch (e) {
      msg.textContent = "✗ " + e.message;
    } finally {
      btn.disabled = false;
      setTimeout(() => { msg.textContent = ""; }, 5000);
    }
  },

  toggleBody(id, header) {
    const el = document.getElementById(id);
    el.hidden = !el.hidden;
    const arrow = header.querySelector(".small");
    if (arrow) arrow.textContent = el.hidden ? "▸" : "▾";
  },

  applyUpdate(resp) {
    if (resp && resp.ok && resp.item) this.a = resp.item;
  },

  async queue() { await this.saveReadStatus("queued"); },

  async saveReadStatus(value) {
    const status = value !== undefined ? value : document.getElementById("readStatus").value;
    try {
      const resp = await API.post(`/api/articles/${this.id}/status`, { read_status: status });
      this.applyUpdate(resp);
      this.render(this.a);
      this.toast(status === "read" ? "已标记为已读" : status === "reading" ? "开始阅读" : status === "queued" ? "已加入清单" : "已移出清单");
    } catch (e) {
      alert("更新失败: " + e.message);
    }
  },

  async toggleStar() {
    try {
      const resp = await API.post(`/api/articles/${this.id}/star`, { starred: !this.a.starred });
      this.applyUpdate(resp);
      this.render(this.a);
    } catch (e) { alert("操作失败: " + e.message); }
  },

  async feedback(kind) {
    const next = this.a.relevance_feedback === kind ? "" : kind;
    try {
      const resp = await API.post(`/api/articles/${this.id}/feedback`, { feedback: next });
      this.applyUpdate(resp);
      this.render(this.a);
    } catch (e) { alert("反馈失败: " + e.message); }
  },

  async saveTags() {
    const tags = document.getElementById("tagsInput").value.split(",").map((t) => t.trim()).filter(Boolean);
    try {
      const resp = await API.post(`/api/articles/${this.id}/tags`, { tags });
      this.applyUpdate(resp);
      this.render(this.a);
      document.getElementById("tagsMsg").textContent = "已保存";
      setTimeout(() => { document.getElementById("tagsMsg").textContent = ""; }, 1500);
    } catch (e) { alert("保存失败: " + e.message); }
  },

  async saveNote() {
    try {
      const resp = await API.post(`/api/articles/${this.id}/note`, {
        note: document.getElementById("noteArea").value,
      });
      this.applyUpdate(resp);
      document.getElementById("noteMsg").textContent = "已保存";
      setTimeout(() => { document.getElementById("noteMsg").textContent = ""; }, 1500);
    } catch (e) { alert("保存失败: " + e.message); }
  },

  async downloadCitation(fmt) {
    try {
      const res = await fetch(`/api/articles/${this.id}/citation?format=${fmt}`);
      if (!res.ok) throw new Error("HTTP " + res.status);
      const text = await res.text();
      try {
        await navigator.clipboard.writeText(text);
        this.toast(`${fmt.toUpperCase()} 已复制到剪贴板`);
      } catch (e) {
        const blob = new Blob([text], { type: "text/plain;charset=utf-8" });
        const a = document.createElement("a");
        a.href = URL.createObjectURL(blob);
        a.download = `citation.${fmt}`;
        a.click();
        URL.revokeObjectURL(a.href);
      }
    } catch (e) { alert("导出失败: " + e.message); }
  },
};

document.addEventListener("DOMContentLoaded", () => {
  Article.init();
  const box = document.getElementById("fulltextBox");
  const btn = document.getElementById("hlSaveBtn");
  if (box && btn) {
    box.addEventListener("mouseup", () => {
      const sel = window.getSelection().toString().trim();
      btn.hidden = !(sel && sel.length > 5);
    });
  }
});
