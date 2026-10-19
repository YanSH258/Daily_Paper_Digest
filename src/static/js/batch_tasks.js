/* Shared background operations: server-side progress survives browser reloads. */
"use strict";
const BatchTasks = {
  loading: false,
  posting: false,
  previous: new Map(),
  applied: new Set(),
  expanded: new Set(),
  requestId() {
    if (crypto.randomUUID) return crypto.randomUUID();
    const bytes = crypto.getRandomValues(new Uint8Array(16));
    bytes[6] = (bytes[6] & 15) | 64;
    bytes[8] = (bytes[8] & 63) | 128;
    const h = [...bytes].map(b => b.toString(16).padStart(2, "0")).join("");
    return `${h.slice(0,8)}-${h.slice(8,12)}-${h.slice(12,16)}-${h.slice(16,20)}-${h.slice(20)}`;
  },
  async submit(kind, ids) {
    if (this.posting) throw new Error("正在提交后台任务，请稍后");
    this.posting = true;
    try {
      const res = await API.post("/api/batch-tasks", {kind, ids, request_id: this.requestId()});
      if (!res.ok) throw new Error(API.errorMessage(res.error));
      this.previous.set(res.task_id, "running");
      await this.load();
      return res.task;
    } finally { this.posting = false; }
  },
  async retry(taskId) {
    if (this.posting) return;
    this.posting = true;
    try {
      const res = await API.post(`/api/batch-tasks/${encodeURIComponent(taskId)}/retry`, {});
      if (!res.ok) throw new Error(API.errorMessage(res.error));
      this.previous.set(res.task_id, "running");
      await this.load();
    } catch (error) { alert("重试未提交：" + API.errorMessage(error)); }
    finally { this.posting = false; }
  },
  async load() {
    if (this.loading) return;
    const targets = [document.getElementById("batchTaskList"), document.getElementById("batchTaskNotice")].filter(Boolean);
    if (!targets.length) return;
    this.loading = true;
    try {
      const res = await API.get("/api/batch-tasks");
      if (!res.ok) throw new Error(API.errorMessage(res.error));
      let reload = false;
      for (const task of res.items) {
        const known = this.previous.has(task.task_id);
        if (this.previous.get(task.task_id) === "running" && task.status !== "running") reload = true;
        this.previous.set(task.task_id, task.status);
        if (task.kind === "zotero" && typeof Library !== "undefined") {
          for (const r of task.results) {
            const key = `${task.task_id}:${r.id}`;
            if (!this.applied.has(key) && known && r.ok && !r.warnings?.length) Library.selection.delete(r.id);
            this.applied.add(key);
          }
        }
      }
      for (const target of targets) this.render(target, target.id === "batchTaskNotice" ? res.items.slice(0, 1) : res.items);
      if (typeof Library !== "undefined") {
        Library.renderBatchBar();
        if (reload) Library.load();
      }
    } catch (error) {
      for (const target of targets) {
        target.innerHTML = `<p class="err">后台任务加载失败：${API.esc(API.errorMessage(error))}</p><button class="secondary">重试加载</button>`;
        target.querySelector("button").onclick = () => this.load();
      }
    } finally { this.loading = false; }
  },
  render(target, tasks) {
    const statuses = {running: "运行中", success: "完成", partial: "部分完成", failed: "失败", interrupted: "已中断"};
    target.innerHTML = tasks.length ? tasks.map(task => {
      const incomplete = task.results.filter(r => !r.ok || r.warnings?.length);
      const retryable = incomplete.length || task.pending_ids.length;
      const resultText = r => r.error || r.warnings?.join("；") || (r.outcome === "skipped" ? "已有译文，跳过" : "完成");
      return `<div class="card small">
        <strong>${task.kind === "zotero" ? "Zotero 推送" : "标题翻译"} · ${API.esc(statuses[task.status] || task.status)}</strong>
        <p class="muted">${API.esc(task.started_at || "")}</p>
        <p role="status">已处理 ${API.esc(task.completed)} / ${API.esc(task.total)} 篇 · 未完成 ${API.esc(incomplete.length + task.pending_ids.length)} 篇</p>
        <progress max="${API.esc(task.total || 1)}" value="${API.esc(task.completed)}" aria-label="后台任务进度"></progress>
        <p>${API.esc(task.error || "")}</p>
        ${task.status === "interrupted" ? "<p>服务曾中断；请核对结果后手动重试。无 DOI 且未保存关联的 Zotero 条目需先人工核对。</p>" : ""}
        <details data-batch-details="${API.esc(task.task_id)}" ${this.expanded.has(task.task_id) ? "open" : ""}><summary>逐项结果</summary><ul>
          ${task.results.map(r => `<li><a href="/article/${encodeURIComponent(r.id)}" target="_blank" rel="noopener">${API.esc(r.title || `文献 ${r.id}`)}</a>：${API.esc(resultText(r))}</li>`).join("")}
          ${task.pending_ids.map(id => `<li>文献 ${API.esc(id)}：${task.status === "running" ? "待完成" : "未完成，请核对后重试"}</li>`).join("")}
        </ul></details>
        ${task.retry_task_id ? "<p>已提交重试任务，请查看最新记录。</p>" : task.status !== "running" && retryable ? `<button class="secondary" data-batch-retry="${API.esc(task.task_id)}">仅重试失败 / 未完成项</button>` : ""}
        ${incomplete.some(r => r.retryable === false) ? "<p>部分条目需先人工恢复 Zotero 关联，未纳入自动重试。</p>" : ""}
      </div>`;
    }).join("") : '<p class="small muted">暂无后台操作。批量推送和标题翻译的进度将在这里显示。</p>';
    for (const details of target.querySelectorAll("[data-batch-details]")) {
      details.ontoggle = () => {
        if (details.open) this.expanded.add(details.dataset.batchDetails);
        else this.expanded.delete(details.dataset.batchDetails);
      };
    }
    for (const button of target.querySelectorAll("[data-batch-retry]")) {
      button.onclick = async () => { button.disabled = true; try { await this.retry(button.dataset.batchRetry); } finally { button.disabled = false; } };
    }
  },
};
