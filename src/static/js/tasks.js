/* tasks.js - 运行任务视图：触发 + 状态轮询 */
"use strict";

const Tasks = {
  async loadStatus() {
    const box = document.getElementById("statusBox");
    try {
      const data = await API.get("/api/status");
      const s = data.task || {};
      const running = s.running
        ? '<span class="badge green">运行中</span>'
        : '<span class="badge muted">空闲</span>';
      const err = s.last_error
        ? `<div class="err" style="margin-top:8px;">${API.esc(s.last_error)}</div>`
        : "";
      box.innerHTML = `
        ${running} task_id=<span class="mono">${API.esc(s.task_id || "-")}</span>
        trigger=${API.esc(s.trigger || "-")} mode=${API.esc(s.mode || "-")}<br>
        started=${API.esc(s.started_at || "-")} ended=${API.esc(s.ended_at || "-")}<br>
        runs=${API.esc(s.run_count)} success=${API.esc(s.success_count)} failure=${API.esc(s.failure_count)}
        ${err}
      `;
    } catch (e) {
      box.innerText = "状态获取失败: " + e.message;
    }
  },

  async trigger(mode) {
    const resultEl = document.getElementById("runResult");
    resultEl.innerText = "提交中...";
    try {
      const data = await API.post("/api/run", { mode });
      resultEl.innerHTML = `<span class="ok">已触发，task_id=${API.esc(data.task_id)}</span>`;
      await this.loadStatus();
    } catch (e) {
      resultEl.innerText = "触发失败: " + e.message;
    }
  },
};
