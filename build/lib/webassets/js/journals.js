/* journals.js - 订阅管理视图 */
"use strict";

const Journals = {
  msg(text, ok) {
    const el = document.getElementById("journalMsg");
    el.innerHTML = `<span class="${ok ? "ok" : "err"}">${text}</span>`;
  },

  async load() {
    try {
      const data = await API.get("/api/journals");
      const tbody = document.querySelector("#journalTable tbody");
      tbody.innerHTML = "";
      for (const j of data.items) {
        const tr = document.createElement("tr");
        const status = j.enabled
          ? '<span class="badge green">启用</span>'
          : '<span class="badge red">停用</span>';
        tr.innerHTML = `
          <td class="mono">${API.esc(j.id)}</td>
          <td>${API.esc(j.name || "未命名")}</td>
          <td class="mono" style="max-width:320px; word-break:break-all;">${API.esc(j.rss)}</td>
          <td>${API.esc(j.publisher || "自动")}</td>
          <td>${j.source === "config" ? '<span class="badge muted">内置迁移</span>' : '<span class="badge blue">自建</span>'}</td>
          <td>${status}</td>
          <td>
            <button class="secondary" onclick="Journals.toggle(${j.id})">${j.enabled ? "停用" : "启用"}</button>
            <button class="danger" onclick="Journals.remove(${j.id})">删除</button>
          </td>
        `;
        tbody.appendChild(tr);
      }
      if (data.items.length === 0) {
        tbody.innerHTML = '<tr><td colspan="7" class="small">暂无订阅，在上方添加或批量导入。</td></tr>';
      }
    } catch (e) {
      this.msg("订阅列表获取失败: " + API.esc(e.message), false);
    }
  },

  async test() {
    const rss = document.getElementById("jRss").value.trim();
    const publisher = document.getElementById("jPublisher").value;
    if (!rss) { this.msg("请先填写 RSS 链接", false); return; }
    this.msg("测试中，请稍候...", true);
    try {
      const data = await API.post("/api/journals/test", { rss, publisher });
      if (data.ok) {
        const samples = (data.sample || []).map((s) => `• ${API.esc(s)}`).join("<br>");
        this.msg(`可用：${API.esc(data.feed_title || "RSS 源")}，共 ${data.count} 条<br>${samples}`, true);
      } else {
        this.msg(API.esc(data.error || "测试失败"), false);
      }
    } catch (e) {
      this.msg("测试失败: " + API.esc(e.message), false);
    }
  },

  async add() {
    const name = document.getElementById("jName").value.trim();
    const rss = document.getElementById("jRss").value.trim();
    const publisher = document.getElementById("jPublisher").value;
    if (!rss) { this.msg("请先填写 RSS 链接", false); return; }
    try {
      const data = await API.post("/api/journals", { name, rss, publisher });
      if (data.ok) {
        this.msg("已保存订阅", true);
        document.getElementById("jName").value = "";
        document.getElementById("jRss").value = "";
        document.getElementById("jPublisher").value = "";
        await this.load();
      } else {
        this.msg(API.esc(data.error || "保存失败"), false);
      }
    } catch (e) {
      this.msg("保存失败: " + API.esc(e.message), false);
    }
  },

  async toggle(id) {
    try {
      await API.post(`/api/journals/${id}/toggle`, {});
      await this.load();
    } catch (e) { this.msg("操作失败: " + API.esc(e.message), false); }
  },

  async remove(id) {
    if (!confirm("确定删除该订阅吗？")) return;
    try {
      await API.del("/api/journals/" + id);
      await this.load();
    } catch (e) { this.msg("删除失败: " + API.esc(e.message), false); }
  },

  onImportFile(ev) {
    const f = ev.target.files[0];
    if (!f) return;
    const reader = new FileReader();
    reader.onload = (e) => { document.getElementById("importText").value = e.target.result; };
    reader.readAsText(f);
  },

  async import() {
    const text = document.getElementById("importText").value.trim();
    const msgEl = document.getElementById("importMsg");
    if (!text) { msgEl.innerText = "请粘贴链接内容或选择文件"; return; }
    msgEl.innerText = "导入中...";
    try {
      const data = await API.post("/api/journals/import", { text });
      if (data.ok) {
        const skipped = (data.skipped || []).map((s) => `• ${API.esc(s.url || "(空)")}（${API.esc(s.reason)}）`).join("<br>");
        let msg = `<span class="ok">导入成功 ${data.added_count} 个</span>`;
        if (data.skipped_count > 0) msg += `<br><span class="small">跳过 ${data.skipped_count} 个：</span><br><span class="small">${skipped}</span>`;
        msgEl.innerHTML = msg;
        document.getElementById("importText").value = "";
        await this.load();
      } else {
        msgEl.innerHTML = `<span class="err">${API.esc(data.error || "导入失败")}</span>`;
      }
    } catch (e) {
      msgEl.innerHTML = `<span class="err">导入失败: ${API.esc(e.message)}</span>`;
    }
  },
};
