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
      const errors = [s.last_error, stats.score_abort_error, s.digest_error, ...(stats.digest_errors || [])].filter(Boolean);
      const status = s.running ? "running" : (s.last_status || (s.last_error ? "failed" : "idle"));
      const statusText = {running:"运行中", success:"成功", partial:"部分失败", failed:"失败", idle:"空闲"}[status] || status;
      const statusClass = status === "success" || status === "running" ? "green" : status === "partial" ? "amber" : status === "failed" ? "red" : "muted";
      box.innerHTML = `
        <span class="badge ${statusClass}">${statusText}</span>
        task_id=<span class="mono">${API.esc(s.task_id || "-")}</span><br>
        trigger=${API.esc(s.trigger || "-")} mode=${API.esc(s.mode === "weekly" ? "周报（legacy）" : s.mode || "-")}<br>
        started=${API.esc(s.started_at || "-")} ended=${API.esc(s.ended_at || "-")}<br>
        runs=${API.esc(s.run_count ?? 0)} success=${API.esc(s.success_count ?? 0)} partial=${API.esc(s.partial_count ?? 0)} failure=${API.esc(s.failure_count ?? 0)}<br>
        ${stats.tokens ? `最近任务 LLM 用量: 请求 ${API.esc(stats.tokens.requests ?? stats.tokens.calls ?? 0)} 次 / 成功 ${API.esc(stats.tokens.calls ?? 0)} 次 / 输入 ${API.esc(stats.tokens.prompt_tokens)} + 输出 ${API.esc(stats.tokens.completion_tokens)} tokens<br>` : ""}
        ${this.renderPipelineStats(stats)}
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

  renderPipelineStats(stats) {
    const keys = ["fetched_raw", "new_eligible", "new_quarantined", "needs_date", "outside_window", "invalid_date",
      "score_queue_total", "score_attempted", "score_deferred", "score_succeeded", "score_failed", "score_failed_backlog", "score_aborted", "llm_requests",
      "rescore_selected", "rescored_ok", "rescored_failed", "dates_completed",
      "source_failed", "source_incomplete"];
    const labels = {fetched_raw:"原始抓取", new_eligible:"新文献准入", new_quarantined:"隔离", needs_date:"待日期", outside_window:"窗口外",
      invalid_date:"无效日期", score_queue_total:"评分队列", score_attempted:"评分尝试", score_deferred:"评分积压", score_succeeded:"评分成功",
      score_failed:"评分失败", score_failed_backlog:"待重试评分", score_aborted:"熔断未尝试", llm_requests:"LLM请求", rescore_selected:"本轮重评",
      rescored_ok:"重评成功", rescored_failed:"重评失败（保留原分数）", dates_completed:"补齐发表日期",
      source_failed:"来源失败", source_incomplete:"来源未抓全"};
    const values = keys.filter(k => stats[k] !== undefined).map(k => `${labels[k]}=${API.esc(stats[k])}`);
    const remaining = Number(stats.score_deferred || 0) + Number(stats.analysis_deferred || 0);
    const completion = stats.budget_completed ? (remaining ? `本轮预算已完成，仍有 ${API.esc(remaining)} 篇待处理。` : "本轮预算已完成，无预算延期文章。") : "";
    const failedBacklog = Number(stats.score_failed_backlog || 0);
    const backlogNote = failedBacklog ? `<p class="small">另有 ${API.esc(failedBacklog)} 篇此前失败的评分待重试，后续运行会自动重试。</p>` : "";
    return values.length ? `<p class="small">流水线统计：${values.join(" · ")}</p><p>${completion}</p>${backlogNote}` : backlogNote;
  },

  addRunButtons() {
    const result = document.getElementById("runResult");
    if (!result || document.getElementById("taskModeButtons")) return;
    const box = document.createElement("span"); box.id = "taskModeButtons"; box.className = "row";
    [["trial", "小批量试运行"], ["preview", "采集预览（不评分）"]].forEach(([runMode, text]) => {
      const b = document.createElement("button"); b.className = "secondary"; b.textContent = text;
      b.onclick = () => this.trigger("light", runMode); box.appendChild(b);
    });
    result.parentNode.insertBefore(box, result);
  },

  async trigger(mode, runMode = "normal") {
    const resultEl = document.getElementById("runResult");
    resultEl.textContent = "提交中...";
    try {
      const data = await API.post("/api/run", { mode, run_mode: runMode });
      if (data.ok === false) throw new Error(API.errorMessage(data.error));
      resultEl.innerHTML = `<span class="ok">已触发，task_id=${API.esc(data.task_id)}</span>`;
      await this.loadStatus();
    } catch (error) {
      resultEl.innerHTML = `<span class="err">触发失败：${API.esc(API.errorMessage(error))}</span> <button class="secondary">重试</button>`;
      resultEl.querySelector("button").onclick = () => this.trigger(mode, runMode);
    }
  },
};

if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", () => Tasks.addRunButtons());
else Tasks.addRunButtons();
