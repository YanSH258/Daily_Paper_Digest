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

    const reasonEl = document.getElementById("aReason");
    if (a.relevance_reason) {
      reasonEl.hidden = false;
      reasonEl.innerHTML = `<b>推荐理由：</b>${API.esc(a.relevance_reason)}`;
    }

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

    // 结构化解读：按 ### 分节，总结置顶
    if (a.analysis) {
      const sections = this.splitSections(a.analysis);
      const concl = sections.find((s) => /总结|速记/.test(s.title));
      if (concl) {
        document.getElementById("conclusionCard").hidden = false;
        document.getElementById("conclusionBody").innerHTML = MarkdownLite.render(concl.body.trim());
      }
      document.getElementById("analysisCard").hidden = false;
      document.getElementById("analysisBody").innerHTML = MarkdownLite.render(a.analysis);
    }

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

document.addEventListener("DOMContentLoaded", () => Article.init());
