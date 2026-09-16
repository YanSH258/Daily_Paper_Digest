/* Run: node /absolute/path/to/tests/report_ui_checks.cjs
 * Requires Node.js >= 18; only Node built-ins, mocked DOM/fetch/storage.
 * Does not start a server, access production data, or send requests.
 * VM checks do NOT verify browser rendering, sandbox enforcement, or downloads.
 */
"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { test } = require("node:test");

class Element {
  constructor() {
    this.children = [];
    this.dataset = {};
    this.hidden = false;
    this.src = "";
    this.nodes = new Map();
  }
  set innerHTML(value) {
    this.html = value;
    this.children = [];
    this.nodes.clear();
  }
  get innerHTML() { return this.html; }
  querySelector(selector) {
    if (!this.nodes.has(selector)) this.nodes.set(selector, new Element());
    return this.nodes.get(selector);
  }
  appendChild(child) { this.children.push(child); }
  get lastElementChild() { return this.querySelector("last"); }
  getAttribute(name) { return this[name]; }
  removeAttribute(name) { this[name] = ""; }
  scrollIntoView() {}
  remove() { this.removed = true; }
  click() { this.clicked = true; }
}

function harness() {
  const elements = new Map(), store = new Map(), blobs = new Map();
  const requests = [], revoked = [], timers = [], confirmations = [], views = [];
  const windowHandlers = new Map();
  const element = id => {
    if (!elements.has(id)) elements.set(id, new Element());
    return elements.get(id);
  };
  const detail = {
    version_id: 7, date: "2026-09-11", version: 2, status: "published", selected_count: 2,
    artifacts: [
      { format: "html", status: "rendered", url: "/reports/daily/2026-09-11/v2/report.html" },
      { format: "markdown", status: "rendered", url: "/reports/daily/2026-09-11/v2/report.md" },
    ],
    deliveries: [{ channel: "email", status: "unknown" }],
  };
  const state = { failLegacy: false, failVersions: false, failBlob: false, failStatus: false, confirm: true, task: {} };
  let sequence = 0;
  class MockURL extends URL {}
  MockURL.createObjectURL = blob => {
    const url = `blob:mock-${++sequence}`;
    blobs.set(url, blob);
    return url;
  };
  MockURL.revokeObjectURL = url => { revoked.push(url); blobs.delete(url); };
  const jsonResponse = value => ({ ok: true, json: async () => value });
  const context = {
    console, URL: MockURL, URLSearchParams, Blob, queueMicrotask,
    setTimeout: callback => timers.push(callback),
    localStorage: { getItem: () => "fake-test-token" },
    sessionStorage: { getItem: key => store.get(key), setItem: (key, value) => store.set(key, value), removeItem: key => store.delete(key) },
    location: { origin: "http://localhost", hash: "", pathname: "/", search: "" },
    history: { replaceState: (_, __, url) => { context.location.hash = url.includes("#") ? url.slice(url.indexOf("#")) : ""; } },
    document: {
      getElementById: element, querySelector: element, createElement: () => new Element(), body: new Element(),
      addEventListener() {},
    },
    window: { addEventListener: (event, callback) => windowHandlers.set(event, callback) },
    showView: name => views.push(name),
    confirm: question => { confirmations.push(question); return state.confirm; },
    fetch: async (url, options) => {
      requests.push({ url, options });
      if (url === "/api/reports") {
        if (state.failLegacy) throw new Error("legacy offline");
        return jsonResponse({ items: [] });
      }
      if (url === "/api/digests?limit=30") {
        if (state.failVersions) throw new Error("versions offline");
        return jsonResponse({ items: [detail] });
      }
      if (url.startsWith("/api/digests/")) return jsonResponse(detail);
      if (url === "/api/status") {
        if (state.failStatus) throw new Error("status offline");
        return jsonResponse({ task: state.task });
      }
      if (url.startsWith("http://localhost/reports/")) {
        if (state.failBlob) return { ok: false, status: 401, json: async () => ({ error: "unauthorized" }) };
        return { ok: true, blob: async () => new Blob(["<script>bad()</script><h1>fixed</h1>"]) };
      }
      throw new Error(`Unexpected mock request: ${url}`);
    },
  };
  vm.createContext(context);
  for (const name of ["api", "reports", "tasks"]) {
    const filename = path.join(__dirname, "..", "src", "static", "js", `${name}.js`);
    vm.runInContext(fs.readFileSync(filename, "utf8"), context, { filename });
  }
  return { context, state, detail, element, store, blobs, requests, revoked, timers, confirmations, views, windowHandlers,
    run: source => vm.runInContext(source, context) };
}

test("indexes load independently, including failures; no per-row HTTP detail requests", async () => {
  const h = harness();
  await h.run("Reports.load()");
  assert.deepEqual(h.requests.map(r => r.url), ["/api/reports", "/api/digests?limit=30"]);
  h.state.failLegacy = true;
  h.requests.length = 0;
  await h.run("Reports.load()");
  assert.equal(h.element("digestVersionsBody").children.length, 1);
  assert.match(h.element("#reportTable tbody").innerHTML, /legacy offline/);
  h.state.failLegacy = false;
  await h.element("#reportTable tbody").querySelector("button").onclick();
  assert.match(h.element("#reportTable tbody").innerHTML, /暂无兼容报告/);
  h.state.failVersions = true;
  await h.run("Reports.load()");
  assert.match(h.element("digestVersionsBody").innerHTML, /versions offline/);
});

test("fixed version selection persists across reload; authenticated preview uses Blob", async () => {
  const h = harness();
  await h.run("Reports.load()");
  await h.run("Reports.previewVersion(7)");
  assert.equal(h.context.location.hash, "#digest_version=7");
  assert.equal(h.store.get("dsh_report_version"), "7");
  assert.match(h.element("reportFrame").src, /^blob:/);
  const request = h.requests.find(r => r.url.startsWith("http://localhost/reports/"));
  assert.equal(request.options.headers["X-API-Token"], "fake-test-token");
  assert.equal(request.options.redirect, "error");
  assert.equal(request.options.cache, "no-store");
  assert.equal(request.url.includes("token="), false);
  const wrapper = await h.blobs.get(h.element("reportFrame").src).text();
  assert.match(wrapper, /Content-Security-Policy/);
  assert.match(wrapper, /default-src 'none'/);
  await h.run("Reports.current = null; Reports.loadVersions()");
  assert.equal(h.run("Reports.current.versionId"), "7");
  h.context.location.hash = "";
  assert.equal(h.run("Reports.selectedVersion()"), "7");
});

test("Markdown toggling escapes source; superseded preview Blobs are revoked", async () => {
  const h = harness();
  await h.run("Reports.previewVersion(7)");
  const previous = h.element("reportFrame").src;
  await h.run("Reports.toggleMd()");
  assert(h.revoked.includes(previous));
  const wrapper = await h.blobs.get(h.element("reportFrame").src).text();
  assert.match(wrapper, /&lt;script&gt;bad\(\)&lt;\/script&gt;/);
  assert(h.requests.some(r => r.url.endsWith("report.md")));
});

test("preview failure clears stale content and retry recovers", async () => {
  const h = harness();
  await h.run("Reports.previewVersion(7)");
  h.state.failBlob = true;
  await h.run("Reports.loadPreview()");
  assert.match(h.element("reportPreviewStatus").textContent, /unauthorized/);
  assert.equal(h.element("reportFrame").src, "");
  h.state.failBlob = false;
  await h.run("Reports.loadPreview()");
  assert.match(h.element("reportFrame").src, /^blob:/);
});

test("download requests use authenticated fetch and release all Blob URLs", async () => {
  const h = harness();
  await h.run("Reports.previewVersion(7)");
  await h.run("Reports.download('markdown')");
  const anchor = h.context.document.body.children[0];
  assert.equal(anchor.clicked, true);
  assert.equal(anchor.removed, true);
  assert.equal(anchor.download, "2026-09-11_v2.md");
  assert.match(anchor.href, /^blob:/);
  assert.equal(h.requests.at(-1).options.headers["X-API-Token"], "fake-test-token");
  for (const timer of h.timers) timer();
  assert(h.revoked.includes(anchor.href));
  h.windowHandlers.get("pagehide")();
  assert.equal(h.run("Reports.blobs.size"), 0);
  assert.equal(h.blobs.size, 0);
});

test("shared Blob API rejects off-origin URLs and query credentials before fetch", async () => {
  const h = harness();
  for (const url of ["https://external.invalid/reports/a", "/reports/a?token=secret", "/api/status"]) {
    await assert.rejects(h.run(`API.blob(${JSON.stringify(url)})`), /无效的报告地址/);
  }
  assert.equal(h.requests.length, 0);
});

test("unknown delivery requires explicit confirmation and never automatically retries", async () => {
  const h = harness();
  await h.run("Reports.loadVersions()");
  const row = h.element("digestVersionsBody").children[0];
  assert.equal(row.querySelector('[data-action="retry"]').disabled, true);
  const notDelivered = row.lastElementChild.children.find(button => button.textContent.includes("核对未送达"));
  assert(notDelivered);
  h.state.confirm = false;
  notDelivered.onclick();
  assert.equal(h.requests.some(r => r.options.method === "POST"), false);
  h.state.confirm = true;
  h.run("const recover = Reports.recover.bind(Reports); Reports.recover = (...args) => globalThis.recovery = recover(...args)");
  notDelivered.onclick();
  await h.run("recovery");
  const post = h.requests.find(r => r.options.method === "POST");
  assert.equal(post.url, "/api/digests/7/resolve-send");
  assert.deepEqual(JSON.parse(post.options.body), { channel: "email", delivered: false });
  assert.match(h.confirmations.at(-1), /本操作不发送/);
  assert.equal(h.requests.some(r => r.url.endsWith("/retry-send")), false);
});

test("task status shows digest failures, links fixed version, and offers load recovery", async () => {
  const h = harness();
  h.state.task = { last_stats: { digest_version_id: 7, digest_version: 2, digest_overall_status: "partial",
    digest_errors: [{ message: "render unavailable", recovery_action: "restore report" }], digest_artifacts: [], digest_deliveries: [] } };
  await h.run("Tasks.loadStatus()");
  assert.match(h.element("statusBox").innerHTML, /render unavailable/);
  assert.match(h.element("statusBox").innerHTML, /partial/);
  const link = h.element("taskDigestActions").children[0];
  assert.equal(link.href, "/#digest_version=7");
  link.onclick({ preventDefault() {} });
  assert.equal(h.context.location.hash, "#digest_version=7");
  assert.deepEqual(h.views, ["reports"]);
  h.state.failStatus = true;
  await h.run("Tasks.loadStatus()");
  assert.match(h.element("statusBox").innerHTML, /status offline/);
  h.state.failStatus = false;
  await h.element("statusBox").querySelector("button").onclick();
  assert.match(h.element("statusBox").innerHTML, /partial/);
});
