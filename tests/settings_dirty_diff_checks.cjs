// 设置页脏字段 diff 检查：save() 只提交相对本次加载快照有变化的字段（丢失更新防护）。
// 运行：node tests/settings_dirty_diff_checks.cjs
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');

const src = fs.readFileSync(path.join(__dirname, '../src/static/js/settings.js'), 'utf8');
const fieldRowSrc = src.match(/  fieldRow\(label, inner\) \{[\s\S]*?\n  \},/)[0];
const renderSrc = src.match(/  render\(c\) \{[\s\S]*?\n  \},/)[0];
const parseGroupsSrc = src.match(/  parseTopicGroups\(text\) \{[\s\S]*?\n  \},/)[0];
const saveSrc = src.match(/  async save\(\) \{[\s\S]*?\n  \},/)[0];
assert.ok(fieldRowSrc && renderSrc && parseGroupsSrc && saveSrc,
  'failed to extract fieldRow()/render()/parseTopicGroups()/save() from settings.js');

const SETTINGS = {
  fetcher: { use_browser: false, max_articles_per_journal: 100, date_filter_days: 3 },
  scheduler: { run_time: '08:00', timezone: 'Asia/Shanghai' },
  zotero: { enabled: false, user_id: '', collection: '', include_note: true, attach_oa_pdf: true },
  tracking: { enabled: true, citation_check_days: 3, author_check_days: 3 },
  llm: { provider: 'deepseek', model: 'deepseek-chat', base_url: 'https://api.deepseek.com', providers: {} },
  openalex: { api_key_set: false, api_key_masked: '' },
  web: { protect_read: false },
  digest: { categories: true, category_limits_configured: false },
  topic_groups: [],
  relevance_threshold: 4,
  research_topics: ['机器学习势函数'],
  output: { email_enabled: false, email_recipients: [], feishu_enabled: false },
};

// 与 render() 的回填表达式保持一致：未触碰的表单值必须等于快照
function formStateFromSettings(c) {
  const out = c.output || {};
  return {
    s_baseurl: { value: c.llm.base_url || '' },
    s_apikey: { value: '' },
    s_threshold: { value: String(c.relevance_threshold ?? 4) },
    s_topics: { value: (c.research_topics || []).join('\n') },
    s_browser: { checked: !!c.fetcher.use_browser },
    s_maxart: { value: String(c.fetcher.max_articles_per_journal ?? 100) },
    s_datedays: { value: String(c.fetcher.date_filter_days ?? 3) },
    s_email: { checked: !!out.email_enabled },
    s_recipients: { value: (out.email_recipients || []).join(', ') },
    s_webhook: { value: '' },
    s_runtime: { value: (c.scheduler || {}).run_time || '08:00' },
    s_timezone: { value: (c.scheduler || {}).timezone || 'UTC' },
    s_smtp_server: { value: out.email_smtp_server || '' },
    s_smtp_port: { value: String(out.email_smtp_port ?? 465) },
    s_smtp_user: { value: out.email_username || '' },
    s_smtp_pass: { value: '' },
    s_smtp_pass_clear: { checked: false },
    s_token: { value: '' },
    s_protect: { checked: !!(c.web && c.web.protect_read) },
    s_zot_on: { checked: !!(c.zotero || {}).enabled },
    s_zot_uid: { value: (c.zotero || {}).user_id || '' },
    s_zot_key: { value: '' },
    s_zot_coll: { value: (c.zotero || {}).collection || '' },
    s_zot_note: { checked: !!(c.zotero || {}).include_note },
    s_zot_pdf: { checked: !!(c.zotero || {}).attach_oa_pdf },
    s_oa_key: { value: '' },
    s_track_on: { checked: !!(c.tracking || {}).enabled },
    s_track_cit: { value: String((c.tracking || {}).citation_check_days ?? 3) },
    s_track_au: { value: String((c.tracking || {}).author_check_days ?? 3) },
    s_digest_cat: { checked: (c.digest || {}).categories !== false },
    s_tgroups: { value: (c.topic_groups || []).map((g) => `${g.name}: ${(g.keywords || []).join(', ')}`).join('\n') },
  };
}

function makeContext({ settings = SETTINGS, provider = 'deepseek', model = 'deepseek-chat', confirmResult = true, overrides = {} } = {}) {
  const els = new Map(Object.entries(formStateFromSettings(settings)));
  for (const [id, v] of Object.entries(overrides)) els.set(id, v);
  const settingsMsg = { innerText: '', innerHTML: '' };
  els.set('settingsMsg', settingsMsg);
  let posted = null;
  let postCount = 0;
  const context = {
    document: { getElementById: (id) => els.get(id) || { value: '', checked: false } },
    API: {
      esc: (s) => String(s),
      post: async (url, payload) => { posted = payload; postCount += 1; return { ok: true, changed: Object.keys(payload) }; },
      setToken: () => {},
    },
    confirm: () => confirmResult,
    console,
  };
  vm.createContext(context);
  vm.runInContext(
    `globalThis.Settings = { ${fieldRowSrc} ${parseGroupsSrc} getProvider: () => ${JSON.stringify(provider)},
       getModelValue: () => ${JSON.stringify(model)},
       load: async () => {}, ${renderSrc} ${saveSrc} };`,
    context,
  );
  context.Settings.render(settings);  // render() 负责写入 this._loaded 快照
  return { ctx: context, msg: settingsMsg, getPosted: () => posted, getPostCount: () => postCount };
}

(async () => {
  // 1. 什么都没改：不发请求（旧版全量提交会把别的窗口/手改配置覆盖回去）
  {
    const { ctx, msg, getPostCount, getPosted } = makeContext();
    await ctx.Settings.save();
    assert.equal(getPostCount(), 0, '无改动时不得发保存请求');
    assert.equal(getPosted(), null);
    assert.match(msg.innerHTML, /没有修改任何设置/);
  }

  // 2. 旧页面只改 API Key：payload 只含 llm.api_key，研究方向等不得随单回写
  {
    const { ctx, getPosted, getPostCount } = makeContext({
      overrides: { s_apikey: { value: 'sk-new-third-party' } },
    });
    await ctx.Settings.save();
    assert.equal(getPostCount(), 1, '有改动时应发一次保存请求');
    assert.deepEqual(Object.keys(getPosted()).sort(), ['llm.api_key'], '只应提交 API Key 一个字段');
    assert.equal(getPosted()['llm.api_key'], 'sk-new-third-party');
  }

  // 3. 只改研究方向：仅提交 research_topics
  {
    const { ctx, getPosted } = makeContext({
      overrides: { s_topics: { value: 'cof\n除铀\n过氧化氢\n光催化' } },
    });
    await ctx.Settings.save();
    assert.deepEqual(Object.keys(getPosted()).sort(), ['research_topics']);
    // payload 由 vm realm 构造，数组原型不同，需先转到宿主 realm 再比较
    assert.deepEqual(Array.from(getPosted()['research_topics']), ['cof', '除铀', '过氧化氢', '光催化']);
  }

  // 4. 清空研究方向 + 确认：提交空数组（显式清空语义）
  {
    const { ctx, getPosted, getPostCount } = makeContext({
      overrides: { s_topics: { value: '' } }, confirmResult: true,
    });
    await ctx.Settings.save();
    assert.equal(getPostCount(), 1);
    assert.deepEqual(Array.from(getPosted()['research_topics']), []);
  }

  // 5. 清空研究方向 + 取消确认：不发请求
  {
    const { ctx, msg, getPosted, getPostCount } = makeContext({
      overrides: { s_topics: { value: '' } }, confirmResult: false,
    });
    await ctx.Settings.save();
    assert.equal(getPostCount(), 0);
    assert.equal(getPosted(), null);
    assert.match(msg.innerText, /已取消保存/);
  }

  // 6. 提供方名为空（选了「新建」没输入名称）：不发请求且提示明确
  {
    const { ctx, msg, getPostCount, getPosted } = makeContext({
      provider: '', overrides: { s_apikey: { value: 'sk-x' } },
    });
    await ctx.Settings.save();
    assert.equal(getPostCount(), 0);
    assert.equal(getPosted(), null);
    assert.match(msg.innerHTML, /提供方名不能为空/);
  }

  // 7. 提供方名含非法字符：不发请求且提示命名规则
  {
    const { ctx, msg, getPostCount, getPosted } = makeContext({
      provider: 'api.gptsapi', overrides: { s_apikey: { value: 'sk-x' } },
    });
    await ctx.Settings.save();
    assert.equal(getPostCount(), 0);
    assert.equal(getPosted(), null);
    assert.match(msg.innerHTML, /提供方名/);
  }

  // 8. 密钥类字段不受 diff 影响：输入 OpenAlex key 即提交
  {
    const { ctx, getPosted } = makeContext({
      overrides: { s_oa_key: { value: 'oa-key-123' } },
    });
    await ctx.Settings.save();
    assert.equal(getPosted()['openalex.api_key'], 'oa-key-123');
  }

  // 9. 只改子方向分组：仅提交 topic_groups（对象数组）
  {
    const { ctx, getPosted } = makeContext({
      overrides: { s_tgroups: { value: '催化 / 反应机理: catalysis, photocatalysis' } },
    });
    await ctx.Settings.save();
    assert.deepEqual(Object.keys(getPosted()).sort(), ['topic_groups']);
    const g = getPosted()['topic_groups'];
    assert.equal(g.length, 1);
    assert.equal(g[0].name, '催化 / 反应机理');
    assert.deepEqual(Array.from(g[0].keywords), ['catalysis', 'photocatalysis']);
  }

  // 10. 关闭领域类别选择：仅提交 digest.categories=false
  {
    const { ctx, getPosted } = makeContext({
      overrides: { s_digest_cat: { checked: false } },
    });
    await ctx.Settings.save();
    assert.deepEqual(Object.keys(getPosted()).sort(), ['digest.categories']);
    assert.equal(getPosted()['digest.categories'], false);
  }

  // 11. 分组行缺关键词：不发请求且给出明确提示
  {
    const { ctx, msg, getPostCount, getPosted } = makeContext({
      overrides: { s_tgroups: { value: '只写了名字' } },
    });
    await ctx.Settings.save();
    assert.equal(getPostCount(), 0);
    assert.equal(getPosted(), null);
    assert.match(msg.innerHTML, /至少需要一个关键词|缺少/);
  }

  console.log('settings dirty-diff UI check passed');
})();
