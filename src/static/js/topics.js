/* topics.js - 研究专题：创建/编辑/论文集合管理 */
"use strict";

const Topics = {
  current: null,
  topics: [],

  showCreate() { document.getElementById("topicCreateCard").hidden = false; },
  hideCreate() { document.getElementById("topicCreateCard").hidden = true; },

  async load() {
    if (!document.getElementById("topicCreateCard").hidden) return; // 编辑中不刷新
    try {
      const data = await API.get("/api/topics");
      this.topics = data.items || [];
      this.renderList();
    } catch (e) {
      document.getElementById("topicsList").innerHTML =
        `<div class="err">专题加载失败: ${API.esc(e.message)}</div>`;
    }
  },

  async create() {
    const name = document.getElementById("topicName").value.trim();
    if (!name) return alert("请输入专题名称");
    try {
      await API.post("/api/topics", {
        name,
        research_question: document.getElementById("topicQuestion").value.trim(),
        notes: document.getElementById("topicNotes").value,
      });
      this.hideCreate();
      document.getElementById("topicName").value = "";
      this.load();
    } catch (e) { alert("创建失败: " + e.message); }
  },

  renderList() {
    const el = document.getElementById("topicsList");
    if (!this.topics.length) {
      el.innerHTML = '<div class="empty-state">还没有专题。点击右上角"新建专题"开始组织你的研究方向。</div>';
      return;
    }
    el.innerHTML = "";
    for (const t of this.topics) {
      const card = document.createElement("div");
      card.className = "today-card";
      card.innerHTML = `
        <div class="today-card-title"><a onclick="Topics.open(${t.id})">${API.esc(t.name)}</a></div>
        ${t.research_question ? `<div class="small muted">❓ ${API.esc(t.research_question)}</div>` : ""}
        <div class="today-card-foot">
          <span class="pill">📄 ${t.paper_count} 篇</span>
          <button class="secondary small-btn" onclick="Topics.open(${t.id})">打开</button>
        </div>
      `;
      el.appendChild(card);
    }
  },

  async open(id) {
    try {
      this.current = await API.get("/api/topics/" + id);
      this.renderDetail();
    } catch (e) { alert("专题加载失败: " + e.message); }
  },

  renderDetail() {
    const t = this.current;
    document.getElementById("topicsList").hidden = true;
    const el = document.getElementById("topicDetail");
    el.hidden = false;
    const papers = t.papers || [];
    el.innerHTML = `
      <div class="card">
        <div class="row" style="justify-content:space-between;">
          <h3 style="margin:0;">${API.esc(t.name)}</h3>
          <div class="row" style="margin:0;">
            <button class="secondary small-btn" onclick="Topics.edit()">编辑</button>
            <button class="secondary small-btn" onclick="Topics.remove()">删除专题</button>
            <button class="secondary small-btn" onclick="Topics.back()">← 返回列表</button>
          </div>
        </div>
        ${t.research_question ? `<div class="today-reason"><b>研究问题：</b>${API.esc(t.research_question)}</div>` : ""}
        ${t.notes ? `<div class="small muted" style="margin-top:6px; white-space:pre-wrap;">${API.esc(t.notes)}</div>` : ""}
      </div>
      <div class="card">
        <h3>论文集合（${papers.length}）</h3>
        ${papers.length ? papers.map((a) => `
          <div class="reading-row">
            <div class="feed-card-score ${a.relevance >= 8 ? "high" : a.relevance >= 6 ? "mid" : "low"}">${a.relevance ? Number(a.relevance).toFixed(1) : "—"}</div>
            <div class="reading-row-main">
              <a class="reading-row-title" href="/article/${a.id}" target="_blank">${API.esc(API.cleanTitle(a.title || ""))}</a>
              <div class="small muted">${API.esc(a.journal || "")} · ${API.esc(a.topic || "")} ${a.evidence_level === "FULLTEXT" ? "· 全文依据" : "· 摘要依据"}</div>
            </div>
            <button class="secondary small-btn" onclick="Topics.removePaper(${t.id}, ${a.id})">移除</button>
          </div>`).join("")
        : '<div class="empty-state">还没有论文。在文献库勾选论文后点"加入专题"。</div>'}
      </div>
    `;
  },

  back() {
    this.current = null;
    document.getElementById("topicDetail").hidden = true;
    document.getElementById("topicsList").hidden = false;
  },

  edit() {
    const t = this.current;
    const name = prompt("专题名称", t.name);
    if (name === null) return;
    const q = prompt("研究问题", t.research_question || "");
    if (q === null) return;
    const notes = prompt("备注", t.notes || "");
    if (notes === null) return;
    API.post("/api/topics/" + t.id, { name, research_question: q, notes })
      .then(() => this.open(t.id))
      .catch((e) => alert("保存失败: " + e.message));
  },

  remove() {
    if (!confirm("确定删除该专题？（不会删除文献本身）")) return;
    API.del("/api/topics/" + this.current.id)
      .then(() => this.back())
      .catch((e) => alert("删除失败: " + e.message));
  },

  removePaper(topicId, articleId) {
    API.del(`/api/topics/${topicId}/papers/${articleId}`)
      .then(() => this.open(topicId))
      .catch((e) => alert("移除失败: " + e.message));
  },

  async loadTopicOptions() {
    try {
      const data = await API.get("/api/topics");
      const sel = document.getElementById("batchTopicSelect");
      const cur = sel.value;
      sel.innerHTML = '<option value="">选择专题…</option>' +
        (data.items || []).map((t) => `<option value="${t.id}">${API.esc(t.name)}</option>`).join("");
      if ([...sel.options].some((o) => o.value === cur)) sel.value = cur;
    } catch (e) { /* 忽略 */ }
  },
};
