/* tracking.js - 追踪视图：关注作者 + 引文追踪清单 */
"use strict";

const Tracking = {
  candidates: [],

  async load() {
    try {
      const [authors, seeds] = await Promise.all([
        API.get("/api/watch/authors"),
        API.get("/api/articles?read_status=none&min_score=0&limit=1"), // 占位，真实来源下面
      ]);
      this.renderAuthors(authors.items || []);
      await this.loadSeeds();
    } catch (e) {
      document.getElementById("authorList").innerHTML =
        `<div class="err">加载失败: ${API.esc(e.message)}</div>`;
    }
  },

  async loadSeeds() {
    // 引文追踪清单：从 status 接口拿不到，用 watched 过滤接口不存在 → 遍历最近文献标记
    // 简化实现：后端提供 watched seeds 数据在 articles 里无直接筛选，这里展示全部已星标文献
    try {
      const data = await API.get("/api/articles?starred=true&limit=50&min_score=0");
      const el = document.getElementById("seedList");
      const items = data.items || [];
      if (!items.length) {
        el.innerHTML = '<div class="empty-state">在文献详情中打开"关注此文的新引用"开始追踪。</div>';
        return;
      }
      el.innerHTML = items.map((a) => `
        <div class="reading-row">
          <div class="reading-row-main">
            <a class="reading-row-title" href="/article/${a.id}" target="_blank">${API.esc(API.cleanTitle(a.title || ""))}</a>
            <div class="small muted">${API.esc(a.journal || "")} ${a.doi ? "· DOI " + API.esc(a.doi) : ""}</div>
          </div>
        </div>`).join("");
    } catch (e) {
      document.getElementById("seedList").innerHTML = `<div class="err">${API.esc(e.message)}</div>`;
    }
  },

  renderAuthors(authors) {
    const el = document.getElementById("authorList");
    if (!authors.length) {
      el.innerHTML = '<div class="empty-state">还没有关注作者。检索并添加后，其新文章会自动入库评分。</div>';
      return;
    }
    el.innerHTML = authors.map((a) => `
      <div class="reading-row">
        <div class="reading-row-main">
          <span class="reading-row-title">${API.esc(a.name)}</span>
          ${a.openalex_id ? `<span class="small muted">· ${API.esc(a.openalex_id)}</span>` : ""}
          <div class="small muted">最近检查：${API.esc(a.last_run || "尚未运行")}</div>
        </div>
        <button class="secondary small-btn" onclick="Tracking.removeAuthor(${a.id})">移除</button>
      </div>`).join("");
  },

  async searchAuthor() {
    const name = document.getElementById("authorSearch").value.trim();
    if (!name) return;
    const box = document.getElementById("authorCandidates");
    box.innerHTML = '<span class="small muted">检索中...</span>';
    try {
      const data = await API.post("/api/watch/authors/search", { name });
      this.candidates = data.items || [];
      box.innerHTML = this.candidates.length
        ? this.candidates.map((c, i) => `
            <button class="chip" onclick="Tracking.addAuthor(${i})">
              ${API.esc(c.name)}${c.affiliation ? " · " + API.esc(c.affiliation) : ""}
              (被引 ${c.cited_count ?? "?"})
            </button>`).join("")
        : '<span class="small muted">无结果</span>';
    } catch (e) {
      box.innerHTML = `<span class="err">${API.esc(e.message)}</span>`;
    }
  },

  async addAuthor(i) {
    const c = this.candidates[i];
    if (!c) return;
    try {
      await API.post("/api/watch/authors", { name: c.name, openalex_id: c.openalex_id });
      this.load();
    } catch (e) { alert("添加失败: " + e.message); }
  },

  async removeAuthor(id) {
    try {
      await API.del("/api/watch/authors/" + id);
      this.load();
    } catch (e) { alert("移除失败: " + e.message); }
  },
};
