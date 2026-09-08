/* chat.js - 文献 AI 对话：SSE 流式消费 + 轻量 Markdown 渲染 */
"use strict";

/* 极简 Markdown → HTML（输入先转义，安全；仅覆盖对话常用语法） */
const MarkdownLite = {
  render(text) {
    let s = API.esc(text ?? "");

    // 代码块 ```...```
    s = s.replace(/```(\w*)\n?([\s\S]*?)```/g, (_, lang, code) =>
      `<pre><code>${code.replace(/\n$/, "")}</code></pre>`);
    // 行内代码
    s = s.replace(/`([^`\n]+)`/g, "<code>$1</code>");
    // 链接 [text](url)（过滤 javascript: 等危险协议）
    s = s.replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g,
      '<a href="$2" target="_blank" rel="noopener">$1</a>');
    // 粗体 / 斜体
    s = s.replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>");
    s = s.replace(/(^|[^*])\*([^*\n]+)\*/g, "$1<em>$2</em>");

    // 按行处理标题 / 列表 / 引用 / 段落
    const lines = s.split("\n");
    const out = [];
    let listType = null;   // null | "ul" | "ol"
    let para = [];

    const flushPara = () => {
      if (para.length) {
        out.push(`<p>${para.join("<br>")}</p>`);
        para = [];
      }
    };
    const flushList = () => {
      if (listType) {
        out.push(`</${listType}>`);
        listType = null;
      }
    };

    for (const line of lines) {
      const t = line.trim();
      const h = t.match(/^(#{1,4})\s+(.*)$/);
      const ul = t.match(/^[-*•]\s+(.*)$/);
      const ol = t.match(/^\d+[.)]\s+(.*)$/);
      const quote = t.match(/^&gt;\s?(.*)$/);

      if (!t) {
        flushPara();
        flushList();
        continue;
      }
      if (h) {
        flushPara(); flushList();
        const level = Math.min(h[1].length + 1, 4); // ### 从 h3 起，避免过大
        out.push(`<h${level}>${h[2]}</h${level}>`);
        continue;
      }
      if (ul) {
        flushPara();
        if (listType !== "ul") { flushList(); out.push("<ul>"); listType = "ul"; }
        out.push(`<li>${ul[1]}</li>`);
        continue;
      }
      if (ol) {
        flushPara();
        if (listType !== "ol") { flushList(); out.push("<ol>"); listType = "ol"; }
        out.push(`<li>${ol[1]}</li>`);
        continue;
      }
      if (quote) {
        flushPara(); flushList();
        out.push(`<blockquote>${quote[1]}</blockquote>`);
        continue;
      }
      if (/^<pre>/.test(t)) {
        flushPara(); flushList();
        out.push(t);
        continue;
      }
      // 表格语法处理：| col1 | col2 |
      if (t.startsWith("|") && t.endsWith("|")) {
        flushPara(); flushList();
        // 收集表格行
        const tableLines = [t];
        let nextIdx = lines.indexOf(line) + 1;
        while (nextIdx < lines.length && lines[nextIdx].trim().startsWith("|") && lines[nextIdx].trim().endsWith("|")) {
          tableLines.push(lines[nextIdx].trim());
          lines[nextIdx] = ""; // 标记已消费
          nextIdx++;
        }
        if (tableLines.length >= 2) {
          let tblHtml = '<div class="table-wrap"><table>';
          let inBody = false;
          for (let rowIdx = 0; rowIdx < tableLines.length; rowIdx++) {
            const rowStr = tableLines[rowIdx];
            // 分隔行 (e.g. |---|---|)
            if (/^\|[\s\-:|]+\|$/.test(rowStr)) {
              if (!inBody) {
                tblHtml += '<tbody>';
                inBody = true;
              }
              continue;
            }
            const cells = rowStr.slice(1, -1).split("|").map(c => c.trim());
            if (rowIdx === 0 && !inBody) {
              tblHtml += '<thead><tr>' + cells.map(c => `<th>${c}</th>`).join("") + '</tr></thead>';
            } else {
              tblHtml += '<tr>' + cells.map(c => `<td>${c}</td>`).join("") + '</tr>';
            }
          }
          if (inBody) tblHtml += '</tbody>';
          tblHtml += '</table></div>';
          out.push(tblHtml);
          continue;
        }
      }
      para.push(t);
    }
    flushPara();
    flushList();
    return out.join("");
  },
};

/* 文献对话模块：绑定在详情抽屉内的聊天区 */
const Chat = {
  articleId: null,
  streaming: false,

  init(articleId) {
    this.articleId = articleId;
    this.streaming = false;
    const wrap = document.getElementById("chatBox");
    wrap.dataset.article = String(articleId);
    this.bindInput();
    this.loadHistory();
  },

  bindInput() {
    const input = document.getElementById("chatInput");
    const send = document.getElementById("chatSend");
    send.onclick = () => this.send();
    input.onkeydown = (e) => {
      if (e.key === "Enter" && !e.shiftKey) {
        e.preventDefault();
        this.send();
      }
    };
  },

  async loadHistory() {
    const listEl = document.getElementById("chatMessages");
    listEl.innerHTML = '<div class="chat-empty">加载对话记录...</div>';
    try {
      const data = await API.get(`/api/articles/${this.articleId}/chat`);
      listEl.innerHTML = "";
      if (!data.items || data.items.length === 0) {
        listEl.innerHTML = '<div class="chat-empty">还没有对话。基于这篇文献的标题 / 摘要 / AI 解读向它提问，记录会保存在这里。</div>';
        return;
      }
      for (const m of data.items) {
        this.appendBubble(m.role, m.content, { done: true });
      }
      this.scrollBottom();
    } catch (e) {
      listEl.innerHTML = `<div class="chat-empty">对话记录加载失败：${API.esc(e.message)}</div>`;
    }
  },

  appendBubble(role, text, { done = false } = {}) {
    const listEl = document.getElementById("chatMessages");
    const empty = listEl.querySelector(".chat-empty");
    if (empty) empty.remove();

    const div = document.createElement("div");
    div.className = `msg ${role === "user" ? "user" : "assistant"}${done ? "" : " streaming"}`;
    if (role === "user") {
      div.textContent = text;
    } else {
      const md = document.createElement("div");
      md.className = "md";
      md.innerHTML = MarkdownLite.render(text);
      div.appendChild(md);
    }
    listEl.appendChild(div);
    this.scrollBottom();
    return div;
  },

  scrollBottom() {
    const listEl = document.getElementById("chatMessages");
    listEl.scrollTop = listEl.scrollHeight;
  },

  setBusy(busy) {
    this.streaming = busy;
    document.getElementById("chatSend").disabled = busy;
    document.getElementById("chatInput").disabled = busy;
  },

  send() {
    if (this.streaming) return;
    const input = document.getElementById("chatInput");
    const question = input.value.trim();
    if (!question) return;

    input.value = "";
    this.appendBubble("user", question);
    this.setBusy(true);

    const bubble = this.appendBubble("assistant", "");
    const mdEl = bubble.querySelector(".md");
    let acc = "";

    API.stream(`/api/articles/${this.articleId}/chat`, { question }, {
      onDelta: (delta) => {
        acc += delta;
        mdEl.innerHTML = MarkdownLite.render(acc);
        this.scrollBottom();
      },
      onDone: () => {
        bubble.classList.remove("streaming");
        if (!acc) mdEl.innerHTML = "<em>（模型未返回内容）</em>";
        this.setBusy(false);
        document.getElementById("chatInput").focus();
      },
      onError: (msg) => {
        bubble.classList.remove("streaming");
        if (!acc) {
          mdEl.innerHTML = `<span style="color:var(--red-fg);">${API.esc(msg)}</span>`;
        }
        this.setBusy(false);
      },
    });
  },

  askSuggestion(text) {
    const input = document.getElementById("chatInput");
    if (!input || this.streaming) return;
    input.value = text;
    this.send();
  },

  async clear() {
    if (!confirm("确定清空这篇文献的对话记录吗？")) return;
    try {
      await API.del(`/api/articles/${this.articleId}/chat`);
      this.loadHistory();
    } catch (e) {
      alert("清空失败: " + e.message);
    }
  },
};
