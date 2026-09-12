/* tasks.js - 运行任务、固定日报版本与失败恢复 */
"use strict";

const Tasks = {
  loading: false,
  async loadStatus() {
    if (this.loading) return;
    this.loading = true;
    const box = document.getElementById("statusBox");
    try {
      const data = await API.get("/api/status");
      const s = data.task || {};
      const stats = s.last_stats || {};
      const digest = s.digest || {
        overall_status: stats.digest_overall_status, errors: stats.digest_errors || [],
        artifacts: stats.digest_artifacts || [], deliveries: stats.digest_deliveries || [],
      };
      const versionId = digest.version_id || stats.digest_version_id;
      const errors = [s.last_error, s.digest_error, ...(stats.digest_errors || [])].filter(Boolean);
      box.innerHTML = `
        <span class="badge ${s.running ? "green" : "muted"}">${s.running ? "运行中" : s.last_error ? "任务失败" : "空闲"}</span>
        task_id=<span class="mono">${API.esc(s.task_id || "-")}</span><br>
        trigger=${API.esc(s.trigger || "-")} mode=${API.esc(s.mode === "weekly" ? "周报（legacy）" : s.mode || "-")}<br>
        started=${API.esc(s.started_at || "-")} ended=${API.esc(s.ended_at || "-")}<br>
        runs=${API.esc(s.run_count ?? 0)} success=${API.esc(s.success_count ?? 0)} failure=${API.esc(s.failure_count ?? 0)}<br>
        ${stats.tokens ? `最近任务 LLM 用量: 调用 ${API.esc(stats.tokens.calls)} 次 / 输入 ${API.esc(stats.tokens.prompt_tokens)} + 输出 ${API.esc(stats.tokens.completion_tokens)} tokens<br>` : ""}
        ${stats.report_skipped_reason ? `<p>报告未生成：${API.esc(stats.report_skipped_reason)}</p>` : ""}
        ${versionId ? `<p>最近日报 v${API.esc(digest.version || stats.digest_version)} · 精选 ${API.esc(digest.selected_count ?? stats.selected_count ?? "—")} · ${API.esc(digest.overall_status || stats.digest_overall_status || "状态待刷新")}</p>
          <p class="small">${API.esc(Reports.statusText(digest))}</p><div id="taskDigestActions" class="row"></div>` : ""}
        ${errors.map(error => `<p class="err">${API.esc(API.errorMessage(error))}</p>`).join("")}
        <button class="secondary" id="taskRefreshBtn">刷新状态</button>`;
      document.getElementById("taskRefreshBtn").onclick = () => this.loadStatus();
      if (versionId && /^\d+$/.test(String(versionId))) {
        const actions = document.getElementById("taskDigestActions");
        const link = document.createElement("a");
        link.textContent = "查看固定日报版本 / 恢复报告与投递";
        link.href = `/#digest_version=${versionId}`;
        link.onclick = e => {
          e.preventDefault();
          history.replaceState(null, "", link.getAttribute("href"));
          Reports.current = null;
          showView("reports");
        };
        actions.appendChild(link);
      }
    } catch (error) {
      box.innerHTML = `<span class="err">状态获取失败：${API.esc(API.errorMessage(error))}</span> <button class="secondary">重试加载</button>`;
      box.querySelector("button").onclick = () => this.loadStatus();
    } finally { this.loading = false; }
  },

  async trigger(mode) {
    const resultEl = document.getElementById("runResult");
    resultEl.textContent = "提交中...";
    try {
      const data = await API.post("/api/run", { mode });
      if (data.ok === false) throw new Error(API.errorMessage(data.error));
      resultEl.innerHTML = `<span class="ok">已触发，task_id=${API.esc(data.task_id)}</span>`;
      await this.loadStatus();
    } catch (error) {
      resultEl.innerHTML = `<span class="err">触发失败：${API.esc(API.errorMessage(error))}</span> <button class="secondary">重试</button>`;
      resultEl.querySelector("button").onclick = () => this.trigger(mode);
    }
  },
};
