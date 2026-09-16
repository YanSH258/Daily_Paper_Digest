/* result.js - 对比 / Related Work 草稿结果页 */
"use strict";

const Result = {
  id: null,
  item: null,

  init() {
    const m = location.pathname.match(/\/results\/(\d+)/);
    if (!m) {
      document.getElementById("content").textContent = "无效链接";
      return;
    }
    this.id = m[1];
    this.load();
  },

  async load() {
    try {
      this.item = await API.get("/api/results/" + this.id);
      document.getElementById("content").innerHTML = MarkdownLite.render(this.item.content || "");
      const kindLabel = this.item.kind === "related" ? "Related Work 草稿" : "多论文对比";
      document.title = `${kindLabel} · Daily Paper Digest`;
      document.getElementById("meta").textContent =
        `${kindLabel} · ${this.item.model || ""} · ${this.item.created_at || ""}`;
    } catch (e) {
      document.getElementById("content").innerHTML = `<span style="color:var(--red)">加载失败: ${API.esc(e.message)}</span>`;
    }
  },

  async copyMd() {
    try {
      await navigator.clipboard.writeText(this.item.content || "");
      alert("Markdown 已复制");
    } catch (e) {
      const blob = new Blob([this.item.content || ""], { type: "text/plain;charset=utf-8" });
      const a = document.createElement("a");
      a.href = URL.createObjectURL(blob);
      a.download = "result.md";
      a.click();
      URL.revokeObjectURL(a.href);
    }
  },
};

document.addEventListener("DOMContentLoaded", () => Result.init());
