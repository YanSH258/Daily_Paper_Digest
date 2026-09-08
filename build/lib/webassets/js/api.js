/* api.js - 请求封装：Token 管理、JSON 请求、SSE 流式读取、工具函数 */
"use strict";

const API = {
  TOKEN_KEY: "dsh_api_token",

  token() {
    return localStorage.getItem(this.TOKEN_KEY) || "";
  },

  setToken(t) {
    localStorage.setItem(this.TOKEN_KEY, t || "");
  },

  headers(json = true) {
    const h = {};
    if (json) h["Content-Type"] = "application/json";
    const t = this.token();
    if (t) h["X-API-Token"] = t;
    return h;
  },

  async json(url, options = {}) {
    const res = await fetch(url, options);
    if (!res.ok) {
      let msg = "HTTP " + res.status;
      try {
        const j = await res.json();
        if (j && (j.error || j.message)) msg = j.error || j.message;
      } catch (e) { /* 忽略非 JSON 错误体 */ }
      throw new Error(msg);
    }
    return res.json();
  },

  get(url) {
    return this.json(url);
  },

  post(url, body) {
    return this.json(url, { method: "POST", headers: this.headers(), body: JSON.stringify(body || {}) });
  },

  del(url) {
    return this.json(url, { method: "DELETE", headers: this.headers(false) });
  },

  /**
   * POST 并消费 SSE 流。
   * 事件格式：data: {"delta": "..."} / {"error": "..."} / {"done": true}
   */
  async stream(url, body, { onDelta, onDone, onError, onContext } = {}) {
    let errored = false;
    try {
      const res = await fetch(url, {
        method: "POST",
        headers: this.headers(),
        body: JSON.stringify(body || {}),
      });
      if (!res.ok) {
        let msg = "HTTP " + res.status;
        try {
          const j = await res.json();
          if (j && j.error) msg = j.error;
        } catch (e) { /* 忽略 */ }
        throw new Error(msg);
      }
      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let buf = "";
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buf += decoder.decode(value, { stream: true });
        let idx;
        while ((idx = buf.indexOf("\n\n")) >= 0) {
          const raw = buf.slice(0, idx);
          buf = buf.slice(idx + 2);
          const line = raw.split("\n").find((l) => l.startsWith("data:"));
          if (!line) continue;
          let payload;
          try {
            payload = JSON.parse(line.slice(5).trim());
          } catch (e) {
            continue;
          }
          if (payload.error) {
            errored = true;
            if (onError) onError(payload.error);
            return;
          }
          if (payload.context && onContext) onContext(payload.context);
          if (payload.done) {
            if (onDone) onDone(payload);
            return;
          }
          if (payload.delta && onDelta) onDelta(payload.delta);
        }
      }
      if (onDone && !errored) onDone({});
    } catch (e) {
      if (onError) onError(e.message);
    }
  },

  esc(s) {
    return (s ?? "").toString().replace(/[&<>"']/g, (ch) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    }[ch]));
  },

  /**
   * 清洗学术文献标题中的 LaTeX 数学格式及特殊标记（如 APS 期刊中的 ${\mathrm{PbZrO}}_{3}$ 等）
   */
  cleanTitle(raw) {
    if (!raw) return "";
    let s = String(raw);
    // 1. 处理常见化学式/数学 LaTeX：${\mathrm{PbZrO}}_{3}$ -> PbZrO₃
    // 剥离 ${\mathrm{...}} -> ...
    s = s.replace(/\$\s*\{\s*\\mathrm\{([^}]+)\}\s*\}\s*(\^|\_)?(\{?[^}$]*\}?)?\s*\$/g, (m, chem, subsup, val) => {
      let cleanVal = (val || "").replace(/[{}]/g, "");
      return chem + cleanVal;
    });
    // 剥离普通的 $\mathrm{...}$ 或 $...$
    s = s.replace(/\$\s*\\mathrm\{([^}]+)\}\s*\$/g, "$1");
    s = s.replace(/\$([^\$]+)\$/g, (m, inner) => {
      return inner.replace(/\\mathrm\{([^}]+)\}/g, "$1").replace(/[{}]/g, "");
    });
    // 移除剩余裸露的 \mathrm{...}、\mathit{...}、\mathbf{...}
    s = s.replace(/\\math[a-z]+\{([^}]+)\}/g, "$1");
    // 移除多余的花括号
    s = s.replace(/_\{([^}]+)\}/g, "$1").replace(/\^\{([^}]+)\}/g, "$1");
    // 压缩多余空格
    return s.replace(/\s+/g, " ").trim();
  },
};
