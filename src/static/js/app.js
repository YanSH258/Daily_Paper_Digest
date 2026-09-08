/* app.js - 视图路由、全局初始化、Token 管理 */
"use strict";

const VIEW_LOADERS = {
  today: () => Today.load(),
  database: () => { Library.load(); Library.loadTags(); },
  reading: () => Reading.load(),
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

  showView("today");
  Tasks.loadStatus();
  setInterval(() => {
    // 任务状态轮询：仅更新 DOM，不打扰其他视图
    if (typeof Tasks !== "undefined") Tasks.loadStatus();
  }, 5000);
}

document.addEventListener("DOMContentLoaded", init);
