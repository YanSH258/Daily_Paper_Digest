// 手动添加 DOI 补全的前端提示检查：三种来源必须给出不同且如实的文案
// 运行：node tests/manual_add_ui_checks.cjs
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');

const src = fs.readFileSync(path.join(__dirname, '../src/static/js/library.js'), 'utf8');
const fetchByDoi = src.match(/  async fetchByDoi\(\) \{[\s\S]*?\n  \},/)[0];
assert.ok(fetchByDoi, 'failed to extract fetchByDoi() from library.js');

function run(response) {
  const els = new Map();
  for (const id of ['manualAddMsg', 'maDoi', 'maTitle', 'maJournal', 'maDate', 'maAuthors', 'maUrl', 'maAbstract']) {
    els.set(id, { value: '', innerHTML: '', textContent: '' });
  }
  els.get('maDoi').value = '10.1038/s41467-026-77704-9';
  const context = {
    document: { getElementById: (id) => els.get(id) },
    API: {
      esc: (s) => String(s),
      post: async () => response,
    },
    console,
  };
  vm.createContext(context);
  vm.runInContext(`globalThis.Library = { ${fetchByDoi} };`, context);
  return context.Library.fetchByDoi().then(() => els);
}

(async () => {
  // 1. Crossref 回退命中（用户这次遇到的情况）
  let els = await run({
    ok: true,
    fetched_from: 'crossref',
    fetched_from_openalex: false,
    article: {
      title: 'Hydride-induced palladium self-diffusion and surface restructuring on Pd(111)',
      journal: 'Nature Communications',
      pub_date: '2026-09-15',
      authors: 'Raju Lipin, Matthias Vandichel',
      url: 'https://doi.org/10.1038/s41467-026-77704-9',
      abstract: 'Reaction conditions affect the electrocatalyst surface.',
    },
  });
  assert.match(els.get('manualAddMsg').innerHTML, /已从 Crossref 补全/);
  assert.equal(els.get('maTitle').value, 'Hydride-induced palladium self-diffusion and surface restructuring on Pd(111)');
  assert.equal(els.get('maJournal').value, 'Nature Communications');
  assert.equal(els.get('maDate').value, '2026-09-15');
  assert.equal(els.get('maAuthors').value, 'Raju Lipin, Matthias Vandichel');
  assert.ok(els.get('maAbstract').value.includes('Reaction conditions'));

  // 2. OpenAlex 命中
  els = await run({
    ok: true, fetched_from: 'openalex', fetched_from_openalex: true,
    article: { title: 'T', journal: 'J', pub_date: '2026-09-11', authors: 'A', url: 'u', abstract: 'x' },
  });
  assert.match(els.get('manualAddMsg').innerHTML, /已从 OpenAlex 补全/);

  // 3. 两家合并
  els = await run({
    ok: true, fetched_from: 'openalex+crossref', fetched_from_openalex: true,
    article: { title: 'T', journal: 'J', pub_date: '2026-09-11', authors: 'A', url: 'u', abstract: 'x' },
  });
  assert.match(els.get('manualAddMsg').innerHTML, /已从 OpenAlex 与 Crossref 补全/);

  // 4. arXiv 预印本
  els = await run({
    ok: true, fetched_from: 'arxiv', fetched_from_openalex: false,
    article: {
      title: 'Machine learning interatomic potentials for solid-state precipitation',
      journal: 'cond-mat.mtrl-sci',
      pub_date: '2026-01-19',
      authors: 'Lorenzo Piersante, Anirudh Raju Natarajan',
      url: 'https://arxiv.org/abs/2601.12984v1',
      abstract: 'MLIPs are routinely used.',
    },
  });
  assert.match(els.get('manualAddMsg').innerHTML, /已从 arXiv 补全/);
  assert.equal(els.get('maJournal').value, 'cond-mat.mtrl-sci');
  assert.equal(els.get('maUrl').value, 'https://arxiv.org/abs/2601.12984v1');

  // 5. 都没找到：如实说明并提示手填，不能显示成功
  els = await run({ ok: true, fetched_from: '', fetched_from_openalex: false, article: {} });
  assert.match(els.get('manualAddMsg').innerHTML, /没查到这篇的元数据/);
  assert.ok(!/class="ok"/.test(els.get('manualAddMsg').innerHTML), '未补全时不得显示成功样式');

  console.log('manual add DOI lookup UI check passed');
})().catch((e) => { console.error(e); process.exit(1); });
