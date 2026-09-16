// 设置页 SMTP 字段 UI 检查：推送卡片渲染 + 保存 payload 语义（留空不改 / 显式清除）
// 运行：node tests/smtp_settings_ui_checks.cjs
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');

const src = fs.readFileSync(path.join(__dirname, '../src/static/js/settings.js'), 'utf8');
const fieldRowSrc = src.match(/  fieldRow\(label, inner\) \{[\s\S]*?\n  \},/)[0];
const renderSrc = src.match(/  render\(c\) \{[\s\S]*?\n  \},/)[0];
const saveSrc = src.match(/  async save\(\) \{[\s\S]*?\n  \},/)[0];
assert.ok(fieldRowSrc && renderSrc && saveSrc, 'failed to extract fieldRow()/render()/save() from settings.js');

const SETTINGS = {
  fetcher: {},
  scheduler: {},
  zotero: {},
  tracking: {},
  llm: { provider: 'deepseek', model: 'deepseek-chat', providers: {} },
  openalex: { api_key_set: true, api_key_masked: 'SAMP****LE01' },
  web: {},
  relevance_threshold: 4,
  research_topics: [],
  output: {
    email_enabled: true,
    email_recipients: ['me@163.com'],
    email_smtp_server: 'smtp.163.com',
    email_smtp_port: 465,
    email_username: 'daily@163.com',
    email_password_set: true,
    email_password_masked: 'SAMP****DE99',
    feishu_enabled: false,
    feishu_webhook_set: true,
  },
};

// ── 1. 渲染：SMTP 字段存在、回填正确、授权码只显示掩码 ──────────────────
{
  const context = { API: { esc: (s) => String(s) }, console };
  vm.createContext(context);
  vm.runInContext(`globalThis.Settings = { ${fieldRowSrc} ${renderSrc} };`, context);

  const html = context.Settings.render(SETTINGS);

  assert.match(html, /id="s_smtp_server"[^>]*value="smtp\.163\.com"/, 'SMTP 服务器需回填当前值');
  assert.match(html, /id="s_smtp_port"[^>]*value="465"/, 'SMTP 端口需回填当前值');
  assert.match(html, /id="s_smtp_user"[^>]*value="daily@163\.com"/, '发件账号需回填当前值');
  assert.match(html, /id="s_smtp_pass"[^>]*placeholder="已配置（SAMP\*\*\*\*DE99），留空保持不变"/, '授权码输入框只提示掩码');
  assert.match(html, /id="s_smtp_pass_clear"[^>]*type="checkbox"/, '需要「清除」复选框');
  assert.ok(!html.includes('SAMPLEAUTHCODE99'), '页面 HTML 不得出现授权码明文');
  assert.match(html, /id="s_recipients"/, '收件人字段保持存在');
  assert.match(html, /id="s_webhook"[^>]*placeholder="已配置，留空保持不变"/, '飞书 Webhook 提示已配置');
  // 未配置授权码时的占位提示
  const empty = context.Settings.render({ ...SETTINGS, output: { ...SETTINGS.output, email_password_set: false, email_password_masked: '' } });
  assert.match(empty, /id="s_smtp_pass"[^>]*placeholder="邮箱服务商处获取的 SMTP 授权码"/, '未配置时提示获取方式');
}

// ── 2. 保存 payload：三种语义 ─────────────────────────────────────────
function makeSaveContext(fields) {
  const els = new Map();
  for (const [id, v] of Object.entries(fields)) {
    els.set(id, { value: v.value ?? '', checked: v.checked ?? false, innerText: '', innerHTML: '' });
  }
  els.set('settingsMsg', { innerText: '', innerHTML: '' });
  let posted = null;
  const context = {
    document: { getElementById: (id) => els.get(id) },
    API: {
      esc: (s) => String(s),
      post: async (url, payload) => { posted = payload; return { ok: true, changed: ['output.email_smtp_server'] }; },
      setToken: () => {},
    },
    console,
  };
  vm.createContext(context);
  vm.runInContext(
    `globalThis.Settings = { getProvider: () => 'deepseek', getModelValue: () => 'deepseek-chat',
       load: async () => {}, ${saveSrc} };`,
    context,
  );
  return { ctx: context, els, getPosted: () => posted };
}

(async () => {
  // 2a. 首次填写：服务器/端口/账号/授权码一起提交
  {
    const { ctx, getPosted } = makeSaveContext({
      s_smtp_server: { value: 'smtp.qq.com' },
      s_smtp_port: { value: '587' },
      s_smtp_user: { value: 'new@qq.com' },
      s_smtp_pass: { value: 'NEWCODE123456' },
    });
    await ctx.Settings.save();
    const p = getPosted();
    assert.equal(p['output.email_smtp_server'], 'smtp.qq.com');
    assert.equal(p['output.email_smtp_port'], 587);
    assert.equal(p['output.email_username'], 'new@qq.com');
    assert.equal(p['output.email_password'], 'NEWCODE123456');
    assert.ok(!('output.email_password_clear' in p), '未勾选清除时不应提交清除标记');
  }

  // 2b. 只改端口：授权码留空 → 不提交 password（服务端保持原值）
  {
    const { ctx, getPosted } = makeSaveContext({
      s_smtp_server: { value: 'smtp.163.com' },
      s_smtp_port: { value: '994' },
      s_smtp_user: { value: 'daily@163.com' },
      s_smtp_pass: { value: '' },
    });
    await ctx.Settings.save();
    const p = getPosted();
    assert.equal(p['output.email_smtp_port'], 994);
    assert.ok(!('output.email_password' in p), '授权码留空不得提交空字符串覆盖');
    assert.ok(!('output.email_password_clear' in p), '授权码留空不得被当成清除');
  }

  // 2c. 勾选清除：提交 clear 标记，且优先于输入框内容
  {
    const { ctx, getPosted } = makeSaveContext({
      s_smtp_server: { value: 'smtp.163.com' },
      s_smtp_port: { value: '465' },
      s_smtp_user: { value: 'daily@163.com' },
      s_smtp_pass: { value: 'typed-but-should-be-ignored' },
      s_smtp_pass_clear: { checked: true },
    });
    await ctx.Settings.save();
    const p = getPosted();
    assert.equal(p['output.email_password_clear'], true);
    assert.ok(!('output.email_password' in p), '勾选清除时不得同时提交新授权码');
  }

  // 2d. 端口留空 → 不提交，避免提交 NaN/0 触发服务端 400
  {
    const { ctx, getPosted } = makeSaveContext({
      s_smtp_server: { value: 'smtp.163.com' },
      s_smtp_port: { value: '' },
      s_smtp_user: { value: 'daily@163.com' },
    });
    await ctx.Settings.save();
    const p = getPosted();
    assert.ok(!('output.email_smtp_port' in p), '端口为空不应提交');
  }

  // 2e. 服务器/账号留空 → 不提交，服务端保持原值
  {
    const { ctx, getPosted } = makeSaveContext({
      s_smtp_server: { value: '' },
      s_smtp_user: { value: '' },
    });
    await ctx.Settings.save();
    const p = getPosted();
    assert.ok(!('output.email_smtp_server' in p), '服务器留空不应提交');
    assert.ok(!('output.email_username' in p), '账号留空不应提交');
  }

  console.log('smtp settings UI check passed');
})().catch((e) => { console.error(e); process.exit(1); });
