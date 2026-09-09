/* reports.js - 历史日报视图：列表 + iframe 预览 */
"use strict";

const Reports = {
  current: null,   // { date, htmlUrl, mdUrl }

  baseName(filePath) {
    return (filePath || "").split(/[\\/]/).pop() || "";
  },

  async load() {
    const tbody = document.querySelector("#reportTable tbody");
    tbody.innerHTML = '<tr><td colspan="5" class="small">加载中...</td></tr>';
    try {
      const data = await API.get("/api/reports");
      tbody.innerHTML = "";
      if (!data.items.length) {
        tbody.innerHTML = '<tr><td colspan="7" class="small">暂无报告记录。</td></tr>';
        return;
      }
      for (const r of data.items) {
        const isWeekly = r.kind === "weekly";
        const md = this.baseName(r.file_path);
        const html = md.replace(/\.md$/, ".html");
        const tr = document.createElement("tr");
        let pushInfo = "—";
        if (r.push_results) {
          try {
            const pr = typeof r.push_results === "string" ? JSON.parse(r.push_results) : r.push_results;
            pushInfo = Object.entries(pr).map(([k, v]) => `${k}:${v ? "✓" : "✗"}`).join(" ") || "—";
          } catch (e) { /* 忽略 */ }
        }
        tr.innerHTML = `
          <td>${isWeekly ? '<span class="pill">周报</span>' : '<span class="pill">日报</span>'}</td>
          <td class="mono">${API.esc(r.report_date)}</td>
          <td>${API.esc(r.total_found ?? "-")}</td>
          <td>${API.esc(r.total_pushed ?? "-")}</td>
          <td class="small">${API.esc(pushInfo)}</td>
          <td class="small muted">${API.esc(r.created_at || "")}</td>
          <td>
            <button class="secondary" onclick='Reports.preview(${JSON.stringify(r.report_date).replace(/'/g, "&#39;")}, ${JSON.stringify(html).replace(/'/g, "&#39;")}, ${JSON.stringify(md).replace(/'/g, "&#39;")})'>预览</button>
            <button class="secondary" onclick='Reports.resend(${JSON.stringify(r.report_date).replace(/'/g, "&#39;")}, this)'>补发推送</button>
          </td>
        `;
        tbody.appendChild(tr);
      }
    } catch (e) {
      tbody.innerHTML = `<tr><td colspan="5" class="err">日报列表获取失败: ${API.esc(e.message)}</td></tr>`;
    }
  },

  preview(date, htmlFile, mdFile) {
    this.current = { date, htmlUrl: "/reports/" + htmlFile, mdUrl: "/reports/" + mdFile };
    document.getElementById("reportPreviewCard").hidden = false;
    document.getElementById("reportPreviewDate").textContent = date;
    document.getElementById("reportMdBtn").textContent = "查看 Markdown 源文件";
    document.getElementById("reportFrame").src = this.current.htmlUrl;
    document.getElementById("reportPreviewCard").scrollIntoView({ behavior: "smooth", block: "start" });
  },

  openInNew() {
    if (this.current) window.open(this.current.htmlUrl, "_blank");
  },

  toggleMd() {
    if (!this.current) return;
    const frame = document.getElementById("reportFrame");
    const btn = document.getElementById("reportMdBtn");
    const showingMd = frame.src.endsWith(".md");
    frame.src = showingMd ? this.current.htmlUrl : this.current.mdUrl;
    btn.textContent = showingMd ? "查看 Markdown 源文件" : "返回 HTML 版";
  },

  async genWeekly() {
    if (!confirm("将汇总本周文献生成周报（不调用 LLM、不推送），继续？")) return;
    try {
      const resp = await API.post("/api/run", { mode: "weekly" });
      if (resp.ok) alert("周报已开始生成，稍后在列表中刷新查看。");
      else alert(resp.error || "触发失败");
    } catch (e) { alert("触发失败: " + e.message); }
  },

  async resend(date, btn) {
    if (btn) { btn.disabled = true; btn.textContent = "发送中..."; }
    try {
      const resp = await API.post("/api/reports/resend", { date });
      if (resp.ok) {
        alert(`补发完成：${Object.entries(resp.push_results || {}).map(([k, v]) => `${k} ${v ? "✓" : "✗"}`).join("，") || "无启用渠道"}`);
        this.load();
      } else {
        alert(resp.error || "补发失败");
      }
    } catch (e) {
      alert("补发失败: " + e.message);
    } finally {
      if (btn) { btn.disabled = false; btn.textContent = "补发推送"; }
    }
  },
};
