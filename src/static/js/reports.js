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
        tbody.innerHTML = '<tr><td colspan="5" class="small">暂无日报记录。</td></tr>';
        return;
      }
      for (const r of data.items) {
        const md = this.baseName(r.file_path);
        const html = md.replace(/\.md$/, ".html");
        const tr = document.createElement("tr");
        tr.innerHTML = `
          <td class="mono">${API.esc(r.report_date)}</td>
          <td>${API.esc(r.total_found ?? "-")}</td>
          <td>${API.esc(r.total_pushed ?? "-")}</td>
          <td class="small muted">${API.esc(r.created_at || "")}</td>
          <td>
            <button class="secondary" onclick='Reports.preview(${JSON.stringify(r.report_date).replace(/'/g, "&#39;")}, ${JSON.stringify(html).replace(/'/g, "&#39;")}, ${JSON.stringify(md).replace(/'/g, "&#39;")})'>预览</button>
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
};
