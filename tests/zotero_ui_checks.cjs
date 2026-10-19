const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const path = require('node:path');
const calls = [], alerts = [];
let fail = false;
const context = {
  localStorage: {getItem: () => null},
  console, confirm: () => true, alert: message => alerts.push(message),
  document: {readyState: 'loading', addEventListener() {}, getElementById: () => null},
  BatchTasks: {submit: async (kind, ids) => {
    calls.push({kind, ids});
    if (fail) throw new Error('offline');
    return {task_id: 'test'};
  }},
};
vm.createContext(context);
vm.runInContext(fs.readFileSync(path.join(__dirname, '../src/static/js/library.js'), 'utf8') + '\nglobalThis.Library = Library;', context);
const lib = context.Library;
(async () => {
  lib.selection = new Set(Array.from({length: 205}, (_, i) => i + 1));
  await lib.batchPushZotero();
  assert.equal(calls.length, 1);
  assert.equal(calls[0].kind, 'zotero');
  assert.equal(calls[0].ids.length, 205);
  assert.equal(lib.selection.size, 205); // selection changes only after server results
  assert.equal(lib.zoteroBusy, false);
  fail = true;
  await lib.batchPushZotero();
  assert.equal(lib.selection.size, 205);
  assert.match(alerts.at(-1), /提交失败/);
  assert.equal(lib.zoteroBusy, false);
  fail = false;
  lib.lastItems = [{id: 7}, {id: 8}];
  await lib.translateTitles();
  assert.equal(calls.at(-1).kind, 'translate');
  assert.deepEqual(Array.from(calls.at(-1).ids), [7, 8]);
  console.log('Library background UI: submit 205 papers without truncation, preserve selection, delegate translation');
})().catch(error => { console.error(error); process.exitCode = 1; });
