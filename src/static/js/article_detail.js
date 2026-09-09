/* article_detail.js - 文献详情抽屉：星标 / 标签 / 笔记 / AI 对话入口 */
"use strict";

const Detail = {
  article: null,

  open(id) {
    document.getElementById("drawerOverlay").hidden = false;
    const drawer = document.getElementById("articleDrawer");
    drawer.hidden = false;
    drawer.scrollTop = 0;
    document.getElementById("drawerContent").innerHTML =
      '<div class="small muted" style="padding:20px 0;">加载中...</div>';
    this.load(id);
  },

  close() {
    document.getElementById("drawerOverlay").hidden = true;
    document.getElementById("articleDrawer").hidden = true;
    this.article = null;
    // 关闭后刷新列表（星标/标签可能已变化）
    if (typeof Library !== "undefined" && Library.loadedOnce) Library.refresh();
  },

  async load(id) {
    try {
      const a = await API.get("/api/articles/" + id);
      this.article = a;
      this.render(a);
      Chat.init(a.id);
    } catch (e) {
      document.getElementById("drawerContent").innerHTML =
        `<div class="err">详情获取失败: ${API.esc(e.message)}</div>`;
    }
  },

  tagList(a) {
    return (a.tags || "").split(",").map((t) => t.trim()).filter(Boolean);
  },

  currentTab: "analysis",

  switchTab(tabName) {
    this.currentTab = tabName;
    document.querySelectorAll(".drawer-tabs .drawer-tab").forEach(t => {
      t.classList.toggle("active", t.dataset.tab === tabName);
    });
    document.querySelectorAll(".drawer-tab-pane").forEach(p => {
      p.hidden = (p.id !== `drawer-pane-${tabName}`);
    });
  },

  render(a) {
    const tags = this.tagList(a);
    const tagBadges = tags.length
      ? tags.map((t) => `<span class="tag-badge">${API.esc(t)}</span>`).join("")
      : '<span class="muted small">暂无标签</span>';

    const hasAnalysis = Boolean(a.analysis);
    const evidenceBadge = a.evidence_level === "FULLTEXT"
      ? '<span class="ev-badge ev-full">全文依据</span>'
      : (a.evidence_level === "ABSTRACT_ONLY"
          ? '<span class="ev-badge ev-abstract">摘要依据</span>'
          : '<span class="ev-badge ev-unknown">依据未知</span>');

    const statusOptions = [
      ["", "未加入清单"], ["queued", "📖 待读"], ["reading", "🔍 在读"], ["read", "✅ 已读"],
    ].map(([v, label]) =>
      `<option value="${v}" ${(a.read_status || "") === v ? "selected" : ""}>${label}</option>`
    ).join("");

    const feedback = a.relevance_feedback || "";
    document.getElementById("drawerContent").innerHTML = `
      <div class="drawer-top">
        <h3>${API.esc(API.cleanTitle(a.title || ""))}</h3>
        <button class="drawer-close" onclick="Detail.close()">关闭 ✕</button>
      </div>

      <div class="drawer-actions">
        <button class="star-btn ${a.starred ? "on" : ""}" id="detailStar" title="收藏"
          onclick="Detail.toggleStar()">${a.starred ? "★" : "☆"}</button>
        <span class="badge blue">${API.esc(a.topic || "未分类")}</span>
        <span class="badge ${Number(a.relevance || 0) >= 7 ? "green" : "muted"}">⭐ ${Number(a.relevance || 0).toFixed(1)}</span>
        ${evidenceBadge}
        <a class="ext-link" href="/article/${a.id}" target="_blank">📖 完整阅读页</a>
        ${a.zotero_key
          ? `<a class="ext-link" href="https://www.zotero.org/users/${Settings._zUserId || '0'}/items/${API.esc(a.zotero_key)}" target="_blank" title="${API.esc(a.zotero_key)}">Zotero ✓</a>`
          : `<a class="ext-link" onclick="Detail.pushZotero()">推送 Zotero</a>`}
        <label class="small"><input type="checkbox" id="watchSeed" ${Detail.watched ? "checked" : ""} onchange="Detail.toggleWatch(this.checked)" /> 关注新引用</label>
        ${a.url ? `<a href="${API.esc(a.url)}" target="_blank" rel="noopener">原文 ↗</a>` : ""}
        ${a.doi ? `<a href="https://doi.org/${API.esc(a.doi)}" target="_blank" rel="noopener">DOI ↗</a>` : ""}
      </div>
      <div class="meta-line"><strong>期刊：</strong>${API.esc(a.journal || "-")} · <strong>日期：</strong>${API.esc(a.pub_date || "-")}</div>
      <div class="meta-line"><strong>作者：</strong>${API.esc(a.authors || "-")}</div>
      ${a.relevance_reason ? `<div class="today-reason"><b>推荐理由：</b>${API.esc(a.relevance_reason)}</div>` : ""}

      <div class="drawer-actions" style="gap:8px;">
        <select id="detailReadStatus" onchange="Detail.saveReadStatus()" style="max-width:150px;">${statusOptions}</select>
        <button class="secondary small-btn ${feedback === 'relevant' ? 'active-green' : ''}" onclick="Detail.saveFeedback('relevant')">👍 有用</button>
        <button class="secondary small-btn ${feedback === 'irrelevant' ? 'active-red' : ''}" onclick="Detail.saveFeedback('irrelevant')">👎 不相关</button>
        <a class="ext-link" onclick="Detail.downloadCitation('bibtex')">复制 BibTeX</a>
      </div>

      <!-- 选项卡切换 -->
      <div class="drawer-tabs">
        <button class="drawer-tab ${this.currentTab === 'analysis' ? 'active' : ''}" data-tab="analysis" onclick="Detail.switchTab('analysis')">AI 解读</button>
        <button class="drawer-tab ${this.currentTab === 'abstract' ? 'active' : ''}" data-tab="abstract" onclick="Detail.switchTab('abstract')">原文摘要</button>
        <button class="drawer-tab ${this.currentTab === 'notes' ? 'active' : ''}" data-tab="notes" onclick="Detail.switchTab('notes')">我的笔记与标签</button>
      </div>

      <!-- Tab 1: AI 解读 -->
      <div class="drawer-tab-pane" id="drawer-pane-analysis" ${this.currentTab !== 'analysis' ? 'hidden' : ''}>
        <div class="content md analysis-content">
          ${hasAnalysis ? MarkdownLite.render(a.analysis) : '<div class="empty-state">暂无 AI 解读（未过相关性门槛或未启用全文解读）</div>'}
        </div>
      </div>

      <!-- Tab 2: 原文摘要 -->
      <div class="drawer-tab-pane" id="drawer-pane-abstract" ${this.currentTab !== 'abstract' ? 'hidden' : ''}>
        <div class="abstract-box">
          <div class="content">${API.esc(a.abstract || "暂无摘要内容。")}</div>
        </div>
      </div>

      <!-- Tab 3: 笔记与标签 -->
      <div class="drawer-tab-pane" id="drawer-pane-notes" ${this.currentTab !== 'notes' ? 'hidden' : ''}>
        <div class="drawer-section">
          <h4>文献标签</h4>
          <div style="margin-bottom:8px;">${tagBadges}</div>
          <div class="editor-row">
            <input id="detailTagsInput" placeholder="自定义标签，逗号分隔，如：精读, 待复现" value="${API.esc(tags.join(", "))}" />
            <button class="secondary" onclick="Detail.saveTags()">保存标签</button>
          </div>
        </div>

        <div class="drawer-section">
          <h4>我的阅读笔记</h4>
          <textarea id="detailNote" class="note-area" placeholder="记录阅读思路、痛点、待办实验...">${API.esc(a.note || "")}</textarea>
          <div class="editor-row">
            <button class="secondary" onclick="Detail.saveNote()">保存笔记</button>
            <span id="detailNoteMsg" class="small"></span>
          </div>
        </div>
      </div>

      <!-- 伴读 AI 对话区 -->
      <div class="drawer-section drawer-chat-section">
        <h4>AI 伴读问答</h4>
        <div class="chat-box" id="chatBox">
          <div class="chat-suggestions">
            <button type="button" class="chip" onclick="Chat.askSuggestion('用通俗的语言解释一下本文的核心创新点？')">💡 核心创新点？</button>
            <button type="button" class="chip" onclick="Chat.askSuggestion('这篇论文用到了哪些基准数据集和评估指标？')">🔬 评测与指标？</button>
            <button type="button" class="chip" onclick="Chat.askSuggestion('如果要复现该方法，主要难点和计算开销在哪里？')">⚙️ 复现难点？</button>
          </div>
          <div class="chat-messages" id="chatMessages">
            <div class="chat-empty">加载中...</div>
          </div>
          <div class="chat-ops">
            <span class="small muted" id="chatState"></span>
            <button class="link-btn" onclick="Chat.retry()">重新生成</button>
            <button class="link-btn" onclick="Chat.clear()">清空对话</button>
          </div>
          <div class="chat-input-row">
            <textarea id="chatInput" placeholder="问问这篇文献…（Enter 发送，Shift+Enter 换行）"></textarea>
            <button id="chatSend">发送</button>
          </div>
        </div>
      </div>
    `;
  },

  applyUpdate(resp, msgEl, rerender = true) {
    if (!resp || !resp.ok) {
      if (msgEl) msgEl.innerHTML = `<span class="err">${API.esc((resp && resp.error) || "保存失败")}</span>`;
      return;
    }
    this.article = resp.item;
    if (rerender) {
      this.render(resp.item);
      // 对话区被重渲染，需要重新绑定当前文献
      Chat.init(resp.item.id);
    }
    if (msgEl) msgEl.innerHTML = '<span class="ok">已保存</span>';
  },

  async toggleStar() {
    const a = this.article;
    if (!a) return;
    try {
      const resp = await API.post(`/api/articles/${a.id}/star`, { starred: !a.starred });
      this.applyUpdate(resp);
    } catch (e) {
      alert("操作失败: " + e.message);
    }
  },

  async pushZotero() {
    const a = this.article;
    if (!a) return;
    try {
      const resp = await API.post(`/api/articles/${a.id}/zotero`, {});
      if (resp.ok) {
        this.article.zotero_key = resp.key;
        this.render(this.article);
        alert(resp.already ? "该文献已在 Zotero 中" : "已推送到 Zotero ✓");
      } else alert(resp.error || "推送失败");
    } catch (e) { alert("推送失败: " + e.message); }
  },

  async toggleWatch(active) {
    const a = this.article;
    if (!a) return;
    try {
      await API.post(`/api/articles/${a.id}/watch`, { active });
    } catch (e) { alert("操作失败: " + e.message); }
  },

  async saveReadStatus() {
    const a = this.article;
    if (!a) return;
    const status = document.getElementById("detailReadStatus").value;
    try {
      const resp = await API.post(`/api/articles/${a.id}/status`, { read_status: status });
      this.applyUpdate(resp, null, false);
    } catch (e) {
      alert("更新失败: " + e.message);
    }
  },

  async saveFeedback(feedback) {
    const a = this.article;
    if (!a) return;
    const next = a.relevance_feedback === feedback ? "" : feedback;
    try {
      const resp = await API.post(`/api/articles/${a.id}/feedback`, { feedback: next });
      this.applyUpdate(resp);
    } catch (e) {
      alert("反馈失败: " + e.message);
    }
  },

  async downloadCitation(fmt) {
    try {
      const res = await fetch(`/api/articles/${this.article.id}/citation?format=${fmt}`);
      if (!res.ok) throw new Error("HTTP " + res.status);
      const text = await res.text();
      try {
        await navigator.clipboard.writeText(text);
        alert("BibTeX 已复制到剪贴板");
      } catch (e) {
        const blob = new Blob([text], { type: "text/plain;charset=utf-8" });
        const a = document.createElement("a");
        a.href = URL.createObjectURL(blob);
        a.download = `citation.${fmt}`;
        a.click();
        URL.revokeObjectURL(a.href);
      }
    } catch (e) {
      alert("导出失败: " + e.message);
    }
  },

  async saveTags() {
    const a = this.article;
    if (!a) return;
    const raw = document.getElementById("detailTagsInput").value;
    const tags = raw.split(",").map((t) => t.trim()).filter(Boolean);
    try {
      const resp = await API.post(`/api/articles/${a.id}/tags`, { tags });
      this.applyUpdate(resp);
    } catch (e) {
      alert("保存失败: " + e.message);
    }
  },

  async saveNote() {
    const a = this.article;
    if (!a) return;
    const msgEl = document.getElementById("detailNoteMsg");
    msgEl.textContent = "保存中...";
    try {
      const resp = await API.post(`/api/articles/${a.id}/note`, {
        note: document.getElementById("detailNote").value,
      });
      // 不重渲染，保持输入焦点
      this.applyUpdate(resp, msgEl, false);
    } catch (e) {
      msgEl.innerHTML = `<span class="err">${API.esc(e.message)}</span>`;
    }
  },
};
