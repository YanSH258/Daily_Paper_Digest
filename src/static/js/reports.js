/* reports.js - 固定版本历史、受保护报告预览与恢复 */
"use strict";

const Reports = {
  current: null,
  versions: new Map(),
  blobs: new Set(),
  previewBlob: null,
  request: 0,
  selectionKey: "dsh_report_version",

  async load() {
    await Promise.all([this.loadLegacy(), this.loadVersions()]);
  },

  errorRow(tbody, columns, label, error, retry) {
    tbody.innerHTML = `<tr><td colspan="${columns}" class="err">${API.esc(label)}: ${API.esc(API.errorMessage(error))} <button class="secondary">重试加载</button></td></tr>`;
    tbody.querySelector("button").onclick = retry;
  },

  async loadLegacy() {
    const tbody = document.querySelector("#reportTable tbody");
    tbody.innerHTML = '<tr><td colspan="7" class="small">加载中...</td></tr>';
    try {
      const data = await API.get("/api/reports");
      tbody.innerHTML = "";
      if (!data.items.length) tbody.innerHTML = '<tr><td colspan="7" class="small">暂无兼容报告记录。</td></tr>';
      for (const r of data.items) {
        const tr = document.createElement("tr");
        let pushes = "—";
        try {
          const result = typeof r.push_results === "string" ? JSON.parse(r.push_results) : r.push_results;
          pushes = Object.entries(result || {}).map(([k, v]) => `${k}:${v === true ? "成功" : v === false ? "失败" : "待发/结果未知"}`).join(" ") || "—";
        } catch (_) { pushes = "推送状态无法解析"; }
        tr.innerHTML = `<td>${r.kind === "weekly" ? "周报（legacy）" : "日报索引"}</td>
          <td class="mono">${API.esc(r.report_date)}</td><td>${API.esc(r.total_found ?? "-")}</td>
          <td>${API.esc(r.total_pushed ?? "-")}</td><td class="small">${API.esc(pushes)}</td>
          <td class="small muted">${API.esc(r.created_at)}</td><td><button class="secondary">预览</button></td>`;
        tr.querySelector("button").onclick = () => {
          try { sessionStorage.removeItem(this.selectionKey); } catch (_) { /* 可禁用存储 */ }
          history.replaceState(null, "", location.pathname + location.search);
          this.preview({ date: r.report_date, htmlUrl: r.html_url, mdUrl: r.md_url });
        };
        if (r.kind !== "weekly") {
          const resend = document.createElement("button");
          resend.className = "secondary";
          resend.textContent = "补发推送";
          resend.onclick = () => this.resend(r.report_date, resend);
          tr.lastElementChild.appendChild(resend);
        }
        tbody.appendChild(tr);
      }
    } catch (error) {
      this.errorRow(tbody, 7, "报告列表获取失败", error, () => this.loadLegacy());
    }
  },

  statusText(v) {
    const artifacts = (v.artifacts || []).map(a => `${a.format}:${a.status}${a.error ? " — " + API.errorMessage(a.error) : ""}`);
    const deliveries = (v.deliveries || []).map(d => `${d.channel}:${d.status}${d.error ? " — " + API.errorMessage(d.error) : ""}`);
    const errors = (v.errors || []).map(e => API.errorMessage(e));
    if (v.summary_error) errors.push(API.errorMessage(v.summary_error));
    if ((v.deliveries || []).some(d => d.status === "unknown")) errors.push("结果未知：先核对收件情况，不自动补发");
    return [...artifacts, ...deliveries, ...errors].join("；") || "暂无产物或投递记录";
  },

  async loadVersions() {
    const tbody = document.getElementById("digestVersionsBody");
    tbody.innerHTML = '<tr><td colspan="5" class="small">加载中...</td></tr>';
    try {
      const data = await API.get("/api/digests?limit=30");
      this.versions.clear();
      tbody.innerHTML = "";
      if (!data.items.length) tbody.innerHTML = '<tr><td colspan="5" class="small muted">暂无版本化日报，运行流水线或发布后生成。</td></tr>';
      for (const v of data.items) {
        this.versions.set(String(v.version_id), v);
        const tr = document.createElement("tr");
        tr.dataset.versionId = String(v.version_id);
        tr.innerHTML = `<td class="mono">${API.esc(v.date)}</td><td>v${API.esc(v.version)} ${API.esc(v.status)}</td>
          <td>${API.esc(v.selected_count ?? "—")}</td><td class="small">${API.esc(this.statusText(v))}</td>
          <td><button class="secondary" data-action="preview">预览</button>
          <button class="secondary" data-action="render">恢复报告文件</button>
          <button class="secondary" data-action="retry">补发失败/待发渠道</button></td>`;
        tr.querySelector('[data-action="preview"]').onclick = () => this.previewVersion(v.version_id);
        tr.querySelector('[data-action="render"]').onclick = e => this.renderVersion(v.version_id, e.target);
        const retry = tr.querySelector('[data-action="retry"]');
        retry.disabled = !(v.deliveries || []).some(d => ["pending", "failed"].includes(d.status));
        retry.onclick = e => this.retryVersion(v.version_id, e.target);
        for (const delivery of v.deliveries || []) {
          const addAction = (label, action, body, question) => {
            const button = document.createElement("button");
            button.className = "secondary";
            button.textContent = `${delivery.channel}：${label}`;
            button.onclick = () => {
              if (confirm(question)) this.recover(v.version_id, action, body, button);
            };
            tr.lastElementChild.appendChild(button);
          };
          if (delivery.status === "sending" && delivery.claim_token) {
            addAction("恢复为结果未知", "recover-send", { channel: delivery.channel, claim_token: delivery.claim_token },
              "确认原发送任务已停止或失联？将此领取标记为 unknown，不发送；随后需核对收件情况。");
          } else if (delivery.status === "unknown") {
            addAction("核对已送达", "resolve-send", { channel: delivery.channel, delivered: true },
              "确认已在收件端核实此版本已经送达？将标记 sent，不再补发。");
            addAction("核对未送达", "resolve-send", { channel: delivery.channel, delivered: false },
              "确认已在收件端核实此版本未送达？将标记 pending，本操作不发送；之后可单独补发。");
          }
        }
        tbody.appendChild(tr);
      }
      const selected = this.selectedVersion();
      if (selected) {
        await this.previewVersion(selected, false);
      }
    } catch (error) {
      this.errorRow(tbody, 5, "版本列表获取失败", error, () => this.loadVersions());
    }
  },

  selectedVersion() {
    const fromHash = new URLSearchParams(location.hash.slice(1)).get("digest_version");
    if (fromHash && /^\d+$/.test(fromHash)) return fromHash;
    try { return sessionStorage.getItem(this.selectionKey); } catch (_) { return null; }
  },

  async previewVersion(versionId, scroll = true) {
    if (!/^\d+$/.test(String(versionId))) return;
    try {
      const v = this.versions.get(String(versionId)) || await API.get(`/api/digests/${versionId}`);
      this.versions.set(String(versionId), v);
      const artifact = format => (v.artifacts || []).find(a => a.format === format && a.status === "rendered")?.url;
      try { sessionStorage.setItem(this.selectionKey, String(versionId)); } catch (_) { /* 可禁用存储 */ }
      history.replaceState(null, "", `#digest_version=${versionId}`);
      await this.preview({ date: `${v.date} v${v.version}`, versionId, htmlUrl: artifact("html"), mdUrl: artifact("markdown"), status: this.statusText(v) }, scroll);
    } catch (error) {
      await this.preview({ date: `版本 ${versionId}`, versionId }, scroll);
      this.previewError(error);
    }
  },

  async preview(report, scroll = true) {
    this.current = report;
    this.showingMd = false;
    document.getElementById("reportPreviewCard").hidden = false;
    document.getElementById("reportPreviewDate").textContent = report.date;
    document.getElementById("reportArtifactStatus").textContent = report.status || "";
    document.getElementById("reportRenderBtn").hidden = !report.versionId;
    if (scroll) document.getElementById("reportPreviewCard").scrollIntoView({ behavior: "smooth", block: "start" });
    await this.loadPreview();
  },

  revoke(url) {
    if (url) { URL.revokeObjectURL(url); this.blobs.delete(url); }
  },

  release() {
    this.request++;
    for (const url of this.blobs) URL.revokeObjectURL(url);
    this.blobs.clear();
    this.previewBlob = null;
    document.getElementById("reportFrame").removeAttribute("src");
  },

  previewError(error) {
    const el = document.getElementById("reportPreviewStatus");
    el.className = "err";
    el.textContent = "报告加载失败：" + API.errorMessage(error) + "。可重试加载；文件缺失时恢复报告文件。";
  },

  async loadPreview() {
    const serial = ++this.request;
    const frame = document.getElementById("reportFrame");
    frame.removeAttribute("src");
    this.revoke(this.previewBlob);
    this.previewBlob = null;
    const status = document.getElementById("reportPreviewStatus");
    status.className = "small";
    status.textContent = "加载中...";
    document.getElementById("reportMdBtn").textContent = this.showingMd ? "返回 HTML 版" : "查看 Markdown 源文件";
    try {
      const url = this.showingMd ? this.current?.mdUrl : this.current?.htmlUrl;
      if (!url) throw new Error("当前版本尚无可用报告文件");
      const raw = await API.blob(url);
      const text = await raw.text();
      if (serial !== this.request) return;
      // Blob HTML 继承页面源；空 sandbox 与 CSP 同时阻止脚本、外部请求和表单。
      const policy = `<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; img-src data:; form-action 'none'; base-uri 'none'">`;
      const body = this.showingMd ? `<pre style="white-space:pre-wrap">${API.esc(text)}</pre>` : text;
      const blob = new Blob([policy, body], { type: "text/html;charset=utf-8" });
      this.previewBlob = URL.createObjectURL(blob);
      this.blobs.add(this.previewBlob);
      frame.src = this.previewBlob;
      status.textContent = "已加载固定报告文件";
    } catch (error) {
      if (serial === this.request) this.previewError(error);
    }
  },

  toggleMd() { this.showingMd = !this.showingMd; return this.loadPreview(); },

  async download(format) {
    const report = this.current;
    try {
      const url = format === "html" ? report?.htmlUrl : report?.mdUrl;
      if (!url) throw new Error("当前版本尚无可用报告文件");
      const blob = await API.blob(url);
      const objectUrl = URL.createObjectURL(blob);
      this.blobs.add(objectUrl);
      const a = document.createElement("a");
      a.href = objectUrl;
      a.download = `${report.date.replace(/[^\w.-]/g, "_")}.${format === "html" ? "html" : "md"}`;
      document.body.appendChild(a);
      a.click();
      a.remove();
      setTimeout(() => this.revoke(objectUrl), 1000);
    } catch (error) { this.previewError(error); }
  },

  async renderVersion(versionId = this.current?.versionId, btn) {
    if (!versionId) return;
    await this.recover(versionId, "render", {}, btn);
  },

  async retryVersion(versionId, btn) {
    const v = this.versions.get(String(versionId));
    const channels = (v?.deliveries || []).filter(d => ["pending", "failed"].includes(d.status)).map(d => d.channel);
    if (!channels.length) return;
    await this.recover(versionId, "retry-send", { channels }, btn);
  },

  async recover(versionId, action, body, btn) {
    if (btn) btn.disabled = true;
    const message = document.getElementById("reportActionStatus");
    message.className = "small";
    message.textContent = "处理中...";
    try {
      const result = await API.post(`/api/digests/${versionId}/${action}`, body);
      const incomplete = result.ok === false || result.overall_status === "partial" || result.overall_status === "failed" || (result.errors || []).length;
      message.className = incomplete ? "err" : "ok";
      message.textContent = `${incomplete ? "仍有未完成项" : "操作完成"}：${this.statusText(result)}`;
      await this.loadVersions();
    } catch (error) {
      message.className = "err";
      message.textContent = "操作失败：" + API.errorMessage(error);
    } finally { if (btn) btn.disabled = false; }
  },

  async resend(date, btn) {
    btn.disabled = true;
    const message = document.getElementById("reportActionStatus");
    message.textContent = "发送中...";
    try {
      const result = await API.post("/api/reports/resend", { date });
      if (!result.ok) throw new Error(API.errorMessage(result.error));
      const statuses = Object.entries(result.push_results || {});
      message.className = result.overall_status === "partial" || statuses.some(([, ok]) => ok !== true) ? "err" : "ok";
      message.textContent = result.version_id ? this.statusText(result)
        : statuses.map(([channel, ok]) => `${channel}:${ok === true ? "成功" : ok === false ? "失败" : "待发/结果未知"}`).join("；") || "无可补发渠道";
      await this.load();
    } catch (error) {
      message.className = "err";
      message.textContent = "补发失败：" + API.errorMessage(error);
    } finally { btn.disabled = false; }
  },

  async genWeekly() {
    if (!confirm("将汇总本周文献生成 legacy 周报（不调用 LLM、不推送），继续？")) return;
    try {
      const resp = await API.post("/api/run", { mode: "weekly" });
      if (!resp.ok) throw new Error(API.errorMessage(resp.error));
      document.getElementById("reportActionStatus").textContent = "legacy 周报已开始生成，稍后刷新列表查看。";
    } catch (error) {
      document.getElementById("reportActionStatus").textContent = "触发失败：" + API.errorMessage(error);
    }
  },
};

window.addEventListener("pagehide", () => Reports.release());
document.addEventListener("DOMContentLoaded", () => {
  const openVersion = () => {
    if (new URLSearchParams(location.hash.slice(1)).has("digest_version")) showView("reports");
  };
  // app.js 的默认视图初始化完成后恢复分享的固定版本。
  queueMicrotask(openVersion);
  window.addEventListener("hashchange", openVersion);
});
