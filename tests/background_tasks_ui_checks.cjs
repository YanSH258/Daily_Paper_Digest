const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const path = require('node:path');
const boxes = new Map(['batchTaskList', 'batchTaskNotice'].map(id => [id, {
  id, innerHTML: '', buttons: [], querySelector: () => ({onclick: null}),
  querySelectorAll(selector) {
    if (selector === '[data-batch-retry]') {
      const match = this.innerHTML.match(/data-batch-retry="([^"]+)"/);
      this.buttons = match ? [{dataset: {batchRetry: match[1]}, disabled: false}] : [];
      return this.buttons;
    }
    return [];
  },
}]));
const calls = [], alerts = [];
let failGet = false;
let rows = [{task_id: 'task-1', kind: 'zotero', status: 'running', total: 3, completed: 1,
  results: [{id: 1, ok: true}], pending_ids: [2, 3]}];
let reloads = 0;
const context = {
  console, crypto: {getRandomValues: a => { a.fill(7); return a; }},
  alert: text => alerts.push(text), document: {getElementById: id => boxes.get(id)},
  Library: {selection: new Set([1, 2, 3]), renderBatchBar() {}, load() { reloads++; }},
  API: {
    esc: value => String(value).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/"/g, '&quot;'),
    errorMessage: error => String(error),
    get: async () => { if (failGet) throw new Error('offline'); return {ok: true, items: rows}; },
    post: async (url, body) => { calls.push({url, body}); return {ok: true, task_id: 'task-1', task: rows[0]}; },
  },
};
vm.createContext(context);
vm.runInContext(fs.readFileSync(path.join(__dirname, '../src/static/js/batch_tasks.js'), 'utf8') + '\nglobalThis.BatchTasks = BatchTasks;', context);
const tasks = context.BatchTasks;
(async () => {
  await tasks.submit('zotero', [1, 2, 3]);
  assert.equal(calls[0].url, '/api/batch-tasks');
  assert.match(calls[0].body.request_id, /^[a-f0-9]{8}-[a-f0-9]{4}-4[a-f0-9]{3}-[89ab][a-f0-9]{3}-[a-f0-9]{12}$/);
  assert.match(boxes.get('batchTaskList').innerHTML, /已处理 1 \/ 3/);
  assert.deepEqual([...context.Library.selection], [2, 3]);
  rows = [{...rows[0], status: 'partial', completed: 3, pending_ids: [], results: [
    {id: 1, ok: true}, {id: 2, ok: false, error: '<script>bad</script>'},
    {id: 3, ok: true, warnings: ['PDF 未完成']},
  ]}];
  await tasks.load();
  assert.equal(reloads, 1);
  assert.match(boxes.get('batchTaskList').innerHTML, /仅重试失败/);
  assert.ok(!boxes.get('batchTaskList').innerHTML.includes('<script>'));
  assert.deepEqual([...context.Library.selection], [2, 3]);
  context.Library.selection.add(1);
  await tasks.load();
  assert.ok(context.Library.selection.has(1)); // don't erase a later user selection
  assert.equal(reloads, 1);
  const retryButton = boxes.get('batchTaskList').buttons[0];
  await retryButton.onclick();
  assert.equal(retryButton.disabled, false);
  assert.equal(calls.at(-1).url, '/api/batch-tasks/task-1/retry');
  // Simulate a fresh browser session: recover server task history without resubmission.
  const posts = calls.length;
  tasks.previous.clear();
  tasks.applied.clear();
  rows[0].status = 'interrupted';
  rows[0].pending_ids = [4];
  await tasks.load();
  assert.equal(calls.length, posts);
  assert.match(boxes.get('batchTaskList').innerHTML, /服务曾中断/);
  failGet = true;
  await tasks.load();
  assert.match(boxes.get('batchTaskList').innerHTML, /加载失败/);
  failGet = false;
  await tasks.load();
  assert.match(boxes.get('batchTaskList').innerHTML, /已中断/);
  console.log('Background UI: progress, result escaping, selective retry, reload recovery, error recovery passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
