/* settings.js - 设置视图：在线编辑写回 config.yaml（保留注释） */
"use strict";

const Settings = {
  fieldRow(label, inner) {
    return `<div class="field"><label>${API.esc(label)}</label>${inner}</div>`;
  },

  render(c) {
    const f = c.fetcher || {};
    const out = c.output || {};
    const sched = c.scheduler || {};
    const zot = c.zotero || {};
    const track = c.tracking || {};
    const llm = c.llm || {};
    const known = llm.providers || {};
    const knownNames = Object.keys(known);

    // 提供方下拉：当前值 + 常见默认项 + 已保存的提供方；"✏️ 新建"入口保留自定义命名
    const providerNames = [...new Set([llm.provider, "deepseek", "qwen", ...knownNames].filter(Boolean))];
    const providerOptions = providerNames.map((name) =>
      `<option value="${API.esc(name)}"${name === llm.provider ? " selected" : ""}>${API.esc(name)}${known[name] ? "" : "（未配置）"}</option>`
    ).join("");
    const currentModel = llm.model || "";

    return `
      <div class="card"><h3>模型</h3>
        ${this.fieldRow("提供方", `<select id="s_provider" onchange="Settings.onProviderChange()">${providerOptions}
            <option value="__custom__">✏️ 新建提供方…</option></select>
          <input id="s_provider_custom" style="display:none;" placeholder="输入新提供方名，如 siliconflow" />`)}
        ${this.fieldRow("Base URL", `<input id="s_baseurl" value="${API.esc(llm.base_url || "")}" placeholder="https://api.deepseek.com 等任意 OpenAI 兼容地址" oninput="Settings.onBaseUrlInput()" onblur="Settings.maybeAutoFetchModels()" />`)}
        ${this.fieldRow("模型名", `<select id="s_model" onchange="Settings.onModelSelectChange()">
            ${currentModel
              ? `<option value="${API.esc(currentModel)}" selected>${API.esc(currentModel)}</option>`
              : `<option value="" selected>—— 填好 Base URL 后自动加载 ———</option>`}
            <option value="__custom__">✏️ 手动输入模型名…</option></select>
          <input id="s_model_custom" style="display:none;" placeholder="输入模型名" />
          <button type="button" class="secondary small-btn" onclick="Settings.fetchModels()">刷新模型</button>`)}
        ${this.fieldRow("API Key", `<input id="s_apikey" type="password" placeholder="${llm.api_key_set ? "已配置（" + API.esc(llm.api_key_masked) + "），留空保持不变" : "未配置，请输入"}" oninput="Settings.onApiKeyInput()" onblur="Settings.maybeAutoFetchModels()" />`)}
        <div class="row" style="margin-top:12px;">
          <button class="secondary" onclick="Settings.testLLM()">测试连接</button>
          <span id="llmTestMsg" class="small"></span>
        </div>
        <p class="small" style="margin:10px 0 0;">提供方下拉选择，✏️ 可新建任意名称；Base URL 填 OpenAI 兼容接口地址，填好后自动探测该端点的可用模型（也可点「刷新模型」），模型直接在下拉框里选（✏️ 仍可手动输入）。测试连接按当前表单值实测一次对话接口（Key 留空时用已保存的 Key）。</p>
      </div>
      <div class="card"><h3>相关性过滤</h3>
        ${this.fieldRow("阈值（0-10）", `<input id="s_threshold" type="number" min="0" max="10" step="0.5" value="${API.esc(c.relevance_threshold ?? 4)}" />`)}
        ${this.fieldRow("研究方向（每行一个）", `<textarea id="s_topics" rows="4">${API.esc((c.research_topics || []).join("\n"))}</textarea>`)}
      </div>
      <div class="card"><h3>抓取</h3>
        ${this.fieldRow("全文抓取", `<label class="small"><input id="s_fulltext" type="checkbox" ${f.use_fulltext ? "checked" : ""} /> 启用</label>`)}
        ${this.fieldRow("浏览器渲染", `<label class="small"><input id="s_browser" type="checkbox" ${f.use_browser ? "checked" : ""} /> 启用</label>`)}
        ${this.fieldRow("每刊上限（篇）", `<input id="s_maxart" type="number" min="1" value="${API.esc(f.max_articles_per_journal ?? 100)}" />`)}
        ${this.fieldRow("日期过滤（天）", `<input id="s_datedays" type="number" min="0" value="${API.esc(f.date_filter_days ?? 3)}" />`)}
      </div>
      <div class="card"><h3>推送</h3>
        ${this.fieldRow("邮件", `<label class="small"><input id="s_email" type="checkbox" ${out.email_enabled ? "checked" : ""} /> 启用</label>`)}
        ${this.fieldRow("收件人（逗号分隔）", `<input id="s_recipients" value="${API.esc((out.email_recipients || []).join(", "))}" placeholder="a@x.com, b@y.com" />`)}
        ${this.fieldRow("飞书", `<label class="small"><input id="s_feishu" type="checkbox" ${out.feishu_enabled ? "checked" : ""} /> 启用</label>`)}
        ${this.fieldRow("飞书 Webhook", `<input id="s_webhook" value="${API.esc(out.feishu_webhook || "")}" placeholder="启用飞书后填写" />`)}
      </div>
      <div class="card"><h3>Zotero</h3>
        ${this.fieldRow("推送开关", `<label class="small"><input id="s_zot_on" type="checkbox" ${zot.enabled ? "checked" : ""} /> 启用推送</label>`)}
        ${this.fieldRow("User ID", `<input id="s_zot_uid" value="${API.esc(zot.user_id || "")}" placeholder="留空自动解析" />`)}
        ${this.fieldRow("API Key", `<input id="s_zot_key" type="password" placeholder="${zot.api_key_set ? "已配置，留空保持不变" : "zotero.org/settings/keys 申请"}" />`)}
        ${this.fieldRow("目标 Collection", `<input id="s_zot_coll" value="${API.esc(zot.collection || "")}" placeholder="collection key（可用测试连接查看）" />`)}
        ${this.fieldRow("附带笔记", `<label class="small"><input id="s_zot_note" type="checkbox" ${zot.include_note ? "checked" : ""} /> 推送时附阅读笔记与AI速记</label>`)}
        ${this.fieldRow("OA PDF 附件", `<label class="small"><input id="s_zot_pdf" type="checkbox" ${zot.attach_oa_pdf ? "checked" : ""} /> 尝试挂 OA PDF（PDF 存 Zotero，不存本地）</label>`)}
        <div class="row" style="margin-top:12px;">
          <button class="secondary" onclick="Settings.testZotero()">测试连接</button>
          <span id="zotTestMsg" class="small"></span>
        </div>
      </div>
      <div class="card"><h3>追踪</h3>
        ${this.fieldRow("引文/作者追踪", `<label class="small"><input id="s_track_on" type="checkbox" ${track.enabled ? "checked" : ""} /> 启用（随每日任务自动执行）</label>`)}
        ${this.fieldRow("引用检查间隔（天）", `<input id="s_track_cit" type="number" min="1" value="${API.esc(track.citation_check_days ?? 3)}" />`)}
        ${this.fieldRow("作者检查间隔（天）", `<input id="s_track_au" type="number" min="1" value="${API.esc(track.author_check_days ?? 3)}" />`)}
      </div>
      <div class="card"><h3>连接（已有 Token）</h3>
        ${this.fieldRow("输入服务端 Token", `<input id="s_connect_token" type="password" placeholder="服务端已配置 Token 时，在此连接本浏览器" />
          <button type="button" class="secondary" onclick="Settings.connectToken()">连接</button>
          <span id="connectTokenMsg" class="small"></span>`)}
        <p class="small" style="margin:8px 0 0;">新浏览器 / 清空缓存后：粘贴服务端 Token 点「连接」即可继续操作，无需改服务端配置。</p>
      </div>
      <div class="card"><h3>屏蔽名单 <span class="small muted" id="blCount"></span></h3>
        <p class="small muted" style="margin:0 0 8px;">清理低分删除的文章会记录在此，抓取时不再回流入库。误删的可在下方解除屏蔽。</p>
        <div id="blockListBox" class="small"></div>
      </div>
      <div class="card"><h3>调度</h3>
        ${this.fieldRow("每日运行时间", `<input id="s_runtime" value="${API.esc(sched.run_time || "08:00")}" placeholder="08:00" />`)}
        ${this.fieldRow("启用 API Token（可选）", `<input id="s_token" type="password" value="" placeholder="${c.web.api_token_set ? "已配置（留空保持不变）" : "本机自用可留空；需要时填写并保存"}" />
          ${c.web.api_token_set ? '<button type="button" class="secondary small-btn" onclick="Settings.clearToken()">清除 Token</button>' : ""}`)}
        ${this.fieldRow("保护读取", `<label class="small"><input id="s_protect" type="checkbox" ${c.web.protect_read ? "checked" : ""} /> 开启后 GET 接口也需 Token（默认关闭，仅本机使用无需打开）</label>`)}
        <p class="small" style="margin:8px 0 0;">默认不启用 Token，写操作可直接用。若把服务暴露到局域网/公网，建议填写 Token 并保存，浏览器会自动记住。配置文件：<span class="mono">${API.esc(c.config_path)}</span></p>
      </div>
    `;
  },

  /** 读取当前表单里的提供方名（下拉或"新建"输入框） */
  getProvider() {
    const sel = document.getElementById("s_provider");
    if (!sel) return "";
    if (sel.value === "__custom__") {
      return (document.getElementById("s_provider_custom")?.value ?? "").trim();
    }
    return (sel.value || "").trim();
  },

  /** 读取当前表单里的模型名（下拉或"手动输入"框） */
  getModelValue() {
    const sel = document.getElementById("s_model");
    if (!sel) return "";
    if (sel.value === "__custom__") {
      return (document.getElementById("s_model_custom")?.value ?? "").trim();
    }
    return (sel.value || "").trim();
  },

  onProviderChange() {
    // 切换提供方（下拉）：回填该提供方已保存的 base_url / model；选"新建"时显示名称输入框
    const sel = document.getElementById("s_provider");
    if (!sel) return;
    const customEl = document.getElementById("s_provider_custom");
    if (sel.value === "__custom__") {
      if (customEl) { customEl.style.display = ""; customEl.focus(); }
      this._fillProviderFields("");  // 新提供方：清空待填
    } else {
      if (customEl) customEl.style.display = "none";
      this._fillProviderFields(sel.value);
    }
    this.maybeAutoFetchModels();  // 切换提供方后自动探测该端点的模型
  },

  /** Base URL 输入：停顿 ~0.8s 后自动探测（避免每敲一个字符发一次请求） */
  onBaseUrlInput() {
    clearTimeout(this._urlDebounce);
    this._urlDebounce = setTimeout(() => this.maybeAutoFetchModels(), 800);
  },

  /** Key 变化后允许对同一 URL 重新探测 */
  onApiKeyInput() {
    this._autoFetchSig = null;
  },

  /** 满足条件时自动探测可用模型：URL 合法且有可用 Key（表单或已保存），同址同 Key 不重复探测 */
  async maybeAutoFetchModels() {
    clearTimeout(this._urlDebounce);
    const url = (document.getElementById("s_baseurl")?.value ?? "").trim();
    if (!url || !/^https?:\/\/[^\s/]+\.[^\s/]+/i.test(url)) return;
    const formKey = (document.getElementById("s_apikey")?.value ?? "").trim();
    const provider = this.getProvider();
    const saved = this.cached && this.cached.llm && this.cached.llm.providers
      ? this.cached.llm.providers[provider] : null;
    if (!formKey && !(saved && saved.api_key_set)) return;  // 无 Key 可用，探测必然失败，保持安静
    const sig = `${url}|${formKey ? "form-key" : `saved:${provider}`}`;
    if (sig === this._autoFetchSig || this._fetchInFlight) return;
    this._autoFetchSig = sig;
    await this.fetchModels(true);
  },

  onModelSelectChange() {
    const sel = document.getElementById("s_model");
    const customEl = document.getElementById("s_model_custom");
    if (!sel || !customEl) return;
    if (sel.value === "__custom__") { customEl.style.display = ""; customEl.focus(); }
    else customEl.style.display = "none";
  },

  /** 重建模型下拉：current 作为当前选项（不在列表时带"不在列表中"标记便于发现） */
  _setModelOptions(current, models = []) {
    const sel = document.getElementById("s_model");
    if (!sel) return;
    current = (current || "").trim();
    let opts = models.map((m) =>
      `<option value="${API.esc(m)}"${m === current ? " selected" : ""}>${API.esc(m)}</option>`).join("");
    if (current && !models.includes(current)) {
      opts = `<option value="${API.esc(current)}" selected>${API.esc(current)}（不在列表中）</option>` + opts;
    }
    if (!opts) {
      opts = `<option value="" selected>—— 点击「获取可用模型」加载 ———</option>`;
    }
    sel.innerHTML = opts + `<option value="__custom__">✏️ 手动输入模型名…</option>`;
    this.onModelSelectChange();
  },

  _fillProviderFields(name) {
    const known = this.cached && this.cached.llm && this.cached.llm.providers
      ? this.cached.llm.providers[name] : null;
    const baseUrlEl = document.getElementById("s_baseurl");
    const keyEl = document.getElementById("s_apikey");
    if (!baseUrlEl || !keyEl) return;
    if (known) {
      baseUrlEl.value = known.base_url || "";
      this._setModelOptions(known.model || "");
      keyEl.value = "";
      keyEl.placeholder = known.api_key_set ? "该提供方已配置 key，留空保持不变" : "未配置，请输入";
    } else {
      baseUrlEl.value = "";
      this._setModelOptions("");
      keyEl.value = "";
      keyEl.placeholder = "未配置，请输入";
    }
  },

  async testLLM() {
    const msgEl = document.getElementById("llmTestMsg");
    msgEl.textContent = "测试中（最长 25 秒）...";
    try {
      const data = await API.post("/api/llm/test", {
        provider: this.getProvider(),
        base_url: (document.getElementById("s_baseurl")?.value ?? "").trim(),
        model: this.getModelValue(),
        api_key: (document.getElementById("s_apikey")?.value ?? "").trim(),
      });
      if (data.ok) {
        msgEl.innerHTML = `<span class="ok">✓ 连接正常 · ${data.latency_ms}ms · 模型回复「${API.esc(data.reply || "(空)")}」</span>`;
      } else {
        msgEl.innerHTML = `<span class="err">✗ ${API.esc(data.error || "测试失败")}</span>`;
      }
    } catch (e) {
      msgEl.innerHTML = `<span class="err">✗ 测试失败: ${API.esc(e.message)}</span>`;
    }
  },

  /** 按 Base URL 拉取端点上可用的模型列表，填入模型下拉框（auto=自动探测时的提示措辞） */
  async fetchModels(auto = false) {
    const msgEl = document.getElementById("llmTestMsg");
    if (this._fetchInFlight) return;
    this._fetchInFlight = true;
    try {
      msgEl.textContent = auto ? "正在自动探测可用模型..." : "获取模型列表中（最长 25 秒）...";
      const data = await API.post("/api/llm/models", {
        provider: this.getProvider(),
        base_url: (document.getElementById("s_baseurl")?.value ?? "").trim(),
        api_key: (document.getElementById("s_apikey")?.value ?? "").trim(),
      });
      if (!data.ok) {
        msgEl.innerHTML = `<span class="err">✗ ${API.esc(data.error || "获取失败")}</span>`;
        return;
      }
      const models = data.models || [];
      const current = this.getModelValue();
      this._setModelOptions(current, models);
      let msg = `✓ 获取到 ${data.count} 个可用模型，已在下拉框中可选`;
      if (current && !models.includes(current)) {
        msg += `；当前填写的「${current}」不在列表中，请重新选择`;
        msgEl.innerHTML = `<span class="err">${API.esc(msg)}</span>`;
      } else {
        msgEl.innerHTML = `<span class="ok">${API.esc(msg)}</span>`;
      }
    } catch (e) {
      msgEl.innerHTML = `<span class="err">✗ 获取失败: ${API.esc(e.message)}</span>`;
    } finally {
      this._fetchInFlight = false;
    }
  },

  async testZotero() {
    const msgEl = document.getElementById("zotTestMsg");
    const val = (id) => (document.getElementById(id)?.value ?? "").trim();
    msgEl.textContent = "测试中...";
    try {
      // 带上表单里未保存的 key/uid，与 LLM 测试一致（先测后存）
      const data = await API.post("/api/zotero/test", {
        api_key: val("s_zot_key"),
        user_id: val("s_zot_uid"),
      });
      if (data.ok) {
        const names = (data.collections || []).slice(0, 8).map((c) => `${c.name}(${c.key})`).join("，");
        msgEl.innerHTML = `<span class="ok">✓ ${API.esc(data.username)}（${API.esc(String(data.user_id))}）</span>` +
          (names ? `<div class="small muted">Collections：${API.esc(names)}</div>` : "");
      } else {
        msgEl.innerHTML = `<span class="err">✗ ${API.esc(data.error || "失败")}</span>`;
      }
    } catch (e) {
      msgEl.innerHTML = `<span class="err">✗ ${API.esc(e.message)}</span>`;
    }
  },

  async reseedMetrics() {
    if (!confirm("用内置种子覆盖期刊指标？会丢失手工修改的 IF。")) return;
    const msgEl = document.getElementById("jcrMsg");
    try {
      const data = await API.post("/api/journal-metrics/reseed", {});
      if (!data.ok) throw new Error(data.error || "失败");
      msgEl.innerHTML = `<span class="ok">已重置 ${data.updated} 条内置指标</span>`;
    } catch (e) {
      msgEl.innerHTML = `<span class="err">${API.esc(e.message)}</span>`;
    }
  },

  async loadBlocklist() {
    const box = document.getElementById("blockListBox");
    if (!box) return;
    try {
      const data = await API.get("/api/blocklist");
      const items = data.items || [];
      document.getElementById("blCount").textContent = items.length ? `(${items.length})` : "";
      if (!items.length) {
        box.innerHTML = '<span class="muted">暂无屏蔽记录。</span>';
        return;
      }
      box.innerHTML = items.slice(0, 50).map((b) => `
        <div class="row" style="justify-content:space-between; margin:2px 0;">
          <span class="mono small">${API.esc((b.title || b.doi || b.url || "?").slice(0, 60))}</span>
          <button class="secondary small-btn" onclick="Settings.unblock(${b.id})">解除</button>
        </div>`).join("") +
        (items.length > 50 ? `<div class="small muted">…其余 ${items.length - 50} 条略</div>` : "");
    } catch (e) {
      box.innerHTML = `<span class="err">${API.esc(e.message)}</span>`;
    }
  },

  async unblock(id) {
    try {
      await API.del("/api/blocklist/" + id);
      this.loadBlocklist();
    } catch (e) { alert("解除失败: " + e.message); }
  },

  async clearToken() {
    if (!confirm("确定清除网页 API Token 吗？清除后所有写操作将不再需要 Token（直到重新设置）。")) return;
    const msgEl = document.getElementById("settingsMsg");
    try {
      const data = await API.post("/api/settings", { "web.api_token_clear": true });
      if (data.ok) {
        msgEl.innerHTML = '<span class="ok">已清除 API Token（立即生效）</span>';
        API.setToken("");
        await this.load();
      } else {
        msgEl.innerHTML = `<span class="err">${API.esc(data.error || "清除失败")}</span>`;
      }
    } catch (e) {
      msgEl.innerHTML = `<span class="err">清除失败: ${API.esc(e.message)}</span>`;
    }
  },

  /** 公开接口：把已有 Token 写入本浏览器（服务端已配 Token、本机无 Token 时用） */
  async connectToken() {
    const msgEl = document.getElementById("connectTokenMsg");
    const token = (document.getElementById("s_connect_token")?.value || "").trim();
    if (!token) {
      msgEl.innerHTML = '<span class="err">请填写 Token</span>';
      return;
    }
    msgEl.textContent = "验证中...";
    try {
      const res = await fetch("/api/auth/token", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ token }),
      });
      const data = await res.json();
      if (!data.ok) throw new Error(data.error || "验证失败");
      API.setToken(token);
      msgEl.innerHTML = '<span class="ok">✓ 已连接，本浏览器将自动携带该 Token</span>';
      if (typeof this.load === "function") await this.load();
    } catch (e) {
      msgEl.innerHTML = `<span class="err">${API.esc(e.message)}</span>`;
    }
  },

  async load() {
    try {
      const c = await API.get("/api/settings");
      this.cached = c;
      document.getElementById("settingsContent").innerHTML = this.render(c);
      this.loadBlocklist();
    } catch (e) {
      document.getElementById("settingsContent").innerHTML =
        `<span class="err">设置读取失败: ${API.esc(e.message)}</span>`;
    }
  },

  async save() {
    const msgEl = document.getElementById("settingsMsg");
    msgEl.innerText = "保存中...";
    const val = (id) => document.getElementById(id)?.value ?? "";
    const chk = (id) => document.getElementById(id)?.checked ?? false;
    const payload = {
      "llm.provider": this.getProvider(),
      "llm.base_url": val("s_baseurl").trim(),
      "llm.model": this.getModelValue(),
      "llm.api_key": val("s_apikey").trim(),
      "relevance_threshold": parseFloat(val("s_threshold")) || 0,
      "research_topics": val("s_topics").split("\n").map((s) => s.trim()).filter(Boolean),
      "fetcher.use_fulltext": chk("s_fulltext"),
      "fetcher.use_browser": chk("s_browser"),
      "fetcher.max_articles_per_journal": parseInt(val("s_maxart")) || 100,
      "fetcher.date_filter_days": parseInt(val("s_datedays")) || 0,
      "output.email_enabled": chk("s_email"),
      "output.email_recipients": val("s_recipients").split(",").map((s) => s.trim()).filter(Boolean),
      "output.feishu_enabled": chk("s_feishu"),
      "output.feishu_webhook": val("s_webhook").trim(),
      "scheduler.run_time": val("s_runtime").trim(),
    };
    // Token 留空 = 不修改；输入新值则提交，并同步写入浏览器，之后写操作自动带上
    const newToken = val("s_token").trim();
    if (newToken) payload["web.api_token"] = newToken;
    payload["web.protect_read"] = chk("s_protect");
    payload["zotero.enabled"] = chk("s_zot_on");
    payload["zotero.user_id"] = val("s_zot_uid").trim();
    const zotKey = val("s_zot_key").trim();
    if (zotKey) payload["zotero.api_key"] = zotKey;
    payload["zotero.collection"] = val("s_zot_coll").trim();
    payload["zotero.include_note"] = chk("s_zot_note");
    payload["zotero.attach_oa_pdf"] = chk("s_zot_pdf");
    payload["tracking.enabled"] = chk("s_track_on");
    payload["tracking.citation_check_days"] = parseInt(val("s_track_cit")) || 3;
    payload["tracking.author_check_days"] = parseInt(val("s_track_au")) || 3;
    try {
      const data = await API.post("/api/settings", payload);
      if (data.ok) {
        let msg = `已保存：${(data.changed || []).join(", ")}`;
        if (data.warning) msg += `（注意：${data.warning}）`;
        if (newToken) API.setToken(newToken);
        msgEl.innerHTML = `<span class="ok">${API.esc(msg)}</span>`;
        await this.load();
      } else {
        msgEl.innerHTML = `<span class="err">${API.esc(data.error || "保存失败")}</span>`;
      }
    } catch (e) {
      msgEl.innerHTML = `<span class="err">保存失败: ${API.esc(e.message)}</span>`;
    }
  },
};
