const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const nodes = new Map();
function node(id) { return {id, textContent: '', innerHTML: '', appendChild(){}}; }
['blockListBox', 'blCount'].forEach(id => nodes.set(id, node(id)));
const items = Array.from({length: 500}, (_, i) => ({id: i + 1, title: `blocked ${i + 1}`}));
const context = {document: {getElementById: id => nodes.get(id)},
  API: {esc: s => String(s), get: async () => ({items}),
        del: async () => ({ok: true})}, console};
vm.createContext(context);
vm.runInContext('globalThis.Settings={' + fs.readFileSync(require('node:path').join(__dirname,'../src/static/js/settings.js'),'utf8').match(/async loadBlocklist\(\) \{[\s\S]*?\n  \},/)[0] + '};', context);
(async()=>{
  await context.Settings.loadBlocklist();
  const html = nodes.get('blockListBox').innerHTML;
  assert.ok(html.startsWith('\n        <details>'), 'list must render inside a collapsed <details>');
  assert.ok(!/<details[^>]*\bopen\b/.test(html), 'must be collapsed by default');
  assert.match(nodes.get('blCount').textContent, /\(500\)/);
  assert.match(html, /展开查看前 50 条（其余 450 条略）/);
  assert.equal((html.match(/解除/g) || []).length, 50);
  console.log('blocklist collapsed UI check passed');
})().catch(e=>{console.error(e);process.exit(1);});
