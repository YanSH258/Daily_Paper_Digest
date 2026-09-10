/* app.js - 视图路由、全局初始化、Token 管理 */
"use strict";

const App = {
  _cursor: 0,
  toggleDark() {
    const dark = !document.body.classList.contains("dark");
    document.body.classList.toggle("dark", dark);
    localStorage.setItem("dsh_theme", dark ? "dark" : "light");
    const btn = document.getElementById("darkToggle");
    if (btn) btn.textContent = dark ? "☀️" : "🌙";
  },
};

const VIEW_LOADERS = {
  today: () => Today.load(),
  database: () => { Library.load(); Library.loadTags(); },
  reading: () => Reading.load(),
  topics: () => Topics.load(),
  trends: () => Trends.load(),
  tracking: () => Tracking.load(),
  subscriptions: () => Journals.load(),
  reports: () => Reports.load(),
  tasks: () => Tasks.loadStatus(),
  settings: () => Settings.load(),
};

function showView(name) {
  document.querySelectorAll(".view").forEach((v) => { v.hidden = true; });
  const target = document.getElementById("view-" + name);
  if (target) target.hidden = false;
  document.querySelectorAll(".nav-item").forEach((n) => {
    n.classList.toggle("active", n.dataset.view === name);
  });
  const loader = VIEW_LOADERS[name];
  if (loader) loader();
}

function initTokenBox() {
  const input = document.getElementById("tokenInput");
  const save = document.getElementById("tokenSave");
  const msg = document.getElementById("tokenMsg");
  input.value = API.token();
  save.addEventListener("click", () => {
    API.setToken(input.value.trim());
    msg.textContent = "已保存到浏览器";
    setTimeout(() => { msg.textContent = ""; }, 2000);
  });
}

function init() {
  document.querySelectorAll(".nav-item").forEach((n) => {
    n.addEventListener("click", () => showView(n.dataset.view));
  });
  document.getElementById("drawerOverlay").addEventListener("click", () => Detail.close());
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !document.getElementById("articleDrawer").hidden) Detail.close();
  });

  Library.init();
  Reading.init();
  Today.init();
  initTokenBox();
  initDarkMode();
  initShortcuts();

  // 从 URL hash 恢复文献库筛选（分享/返回不丢状态）
  if (location.hash.length > 1 && document.getElementById("view-database")) {
    const params = new URLSearchParams(location.hash.slice(1));
    if (params.has("q")) {
      showView("database");
      document.getElementById("q").value = params.get("q") || "";
      document.getElementById("journal").value = params.get("journal") || "";
      document.getElementById("topic").value = params.get("topic") || "";
      document.getElementById("readStatusFilter").value = params.get("read_status") || "";
      const score = params.get("min_score") || "0";
      const preset = document.getElementById("scorePreset");
      const custom = document.getElementById("minScore");
      if (["0", "3", "5", "7", "8"].includes(score)) {
        if (preset) preset.value = score;
        if (custom) { custom.value = score; custom.hidden = true; }
      } else {
        if (preset) preset.value = "custom";
        if (custom) { custom.value = score || "0"; custom.hidden = false; }
      }
    }
  }

  showView("today");
  Topics.loadTopicOptions();
  setInterval(() => Topics.loadTopicOptions(), 60000);
  Tasks.loadStatus();
  setInterval(() => {
    // 任务状态轮询：仅更新 DOM，不打扰其他视图
    if (typeof Tasks !== "undefined") Tasks.loadStatus();
  }, 5000);
}

function initDarkMode() {
  const theme = localStorage.getItem("dsh_theme") || "light";
  document.body.classList.toggle("dark", theme === "dark");
  const btn = document.getElementById("darkToggle");
  if (btn) btn.textContent = theme === "dark" ? "☀️" : "🌙";
}

function initShortcuts() {
  document.addEventListener("keydown", (e) => {
    if (e.target.matches("input, textarea, select") || e.metaKey || e.ctrlKey) return;
    if (document.getElementById("view-database").hidden) return;
    const cards = [...document.querySelectorAll("#articleCardContainer .article-feed-card, #articleTable tbody tr")];
    if (!cards.length) return;
    App._cursor = App._cursor || 0;
    if (e.key === "j" || e.key === "k") {
      App._cursor = Math.min(cards.length - 1, Math.max(0, App._cursor + (e.key === "j" ? 1 : -1)));
      cards.forEach((c, i) => c.classList.toggle("kb-focus", i === App._cursor));
      cards[App._cursor].scrollIntoView({ block: "nearest" });
    } else if (e.key === "s") {
      const el = cards[App._cursor] && cards[App._cursor].querySelector("[data-star]");
      if (el) el.click();
    } else if (e.key === "q") {
      const el = cards[App._cursor];
      const id = el && (el.dataset.id || (el.querySelector("[data-id]") || {}).dataset?.id);
      if (id) fetch(`/api/articles/${id}/status`, { method: "POST",
        headers: API.headers(), body: JSON.stringify({ read_status: "queued" }) })
        .then(() => { msgFlash("已加入待读清单"); });
    } else if (e.key === "o") {
      const el = cards[App._cursor];
      const link = el && el.querySelector("a[href^='/article/']");
      if (link) window.open(link.getAttribute("href"), "_blank");
    }
  });
}

function msgFlash(text) {
  const el = document.getElementById("toastBar") || (() => {
    const d = document.createElement("div");
    d.id = "toastBar";
    d.className = "toast-bar";
    document.body.appendChild(d);
    return d;
  })();
  el.textContent = text;
  el.classList.add("show");
  setTimeout(() => el.classList.remove("show"), 1800);
}

document.addEventListener("DOMContentLoaded", init);
