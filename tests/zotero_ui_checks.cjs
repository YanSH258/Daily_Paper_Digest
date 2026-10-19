const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const path = require('node:path');
const calls = [], alerts = [];
let interrupt = false;
const context = {
  localStorage: {getItem: () => null},
  console, confirm: () => true, alert: message => alerts.push(message),
  document: {readyState: 'loading', addEventListener() {}},
  API: {post: async (url, body) => {
    calls.push(body.ids);
    if (interrupt && calls.length === 2) throw new Error('offline');
    return {ok: true, results: body.ids.map(id => ({id, ok: id !== 2,
      error: id === 2 ? 'failed' : undefined, warnings: id === 3 ? ['PDF 未完成'] : []}))};
  }},
};
vm.createContext(context);
vm.runInContext(fs.readFileSync(path.join(__dirname, '../src/static/js/library.js'), 'utf8') + '\nglobalThis.Library = Library;', context);
const lib = context.Library;
let refreshed = 0;
lib.renderBatchBar = () => {};
lib.load = () => { refreshed++; };
(async () => {
  lib.selection = new Set(Array.from({length: 205}, (_, i) => i + 1));
  await lib.batchPushZotero();
  assert.deepEqual(calls.map(ids => ids.length), [100, 100, 5]);
  assert.deepEqual([...lib.selection], [2, 3]);
  assert.match(alerts[0], /失败 1，待补齐 1/);
  assert.equal(lib.zoteroBusy, false);
  calls.length = 0;
  interrupt = true;
  lib.selection = new Set(Array.from({length: 105}, (_, i) => i + 10));
  await lib.batchPushZotero();
  assert.deepEqual([...lib.selection], [110, 111, 112, 113, 114]);
  assert.match(alerts.at(-1), /推送中断/);
  assert.equal(refreshed, 2);
  assert.equal(lib.zoteroBusy, false);
  console.log('Zotero UI: batching, partial retry, interrupted batch selection passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
