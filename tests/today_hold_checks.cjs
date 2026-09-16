const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
function loadToday(extra = {}) {
  const els = new Map();
  for (const id of ["todayTitle","todayOverview","todayTop","todayNotable","todayBrowse",
                    "todayProcessing","todayTopWrap","todayNotableWrap","todayBrowseWrap","todayProcessingWrap"]) {
    els.set(id, {textContent:'', innerHTML:'', hidden:false, scrollIntoView(){}, children: [], appendChild(c){ this.children.push(c); }});
  }
  const ctx = {document: {getElementById: id => els.get(id),
    createElement: () => ({textContent:'', innerHTML:'', className:'', children: [], appendChild(c){ this.children.push(c); }})},
    API: {esc: s => String(s), cleanTitle: t => String(t), errorMessage: e => String(e)},
    Detail: {}, location: {origin: 'http://127.0.0.1'}, console};
  ctx.data = {date: '2026-09-15', total: 386,
    counts: {top: 12, notable: 13, browse: 100, processing: 261, queued: 45, failed: 143, quarantined: 73},
    buckets: {top: [], notable: [], browse: []},
    processing: [
      {id: 1, title: 'failed one', journal: 'J', score_status: 'failed', hold_reason: '评分失败'},
      {id: 2, title: 'queued one', journal: 'J', score_status: '', hold_reason: '排队等评分预算'},
      {id: 3, title: 'needs date', journal: 'J', score_status: '', processing_status: 'needs_date', hold_reason: '日期待核验'},
    ],
    task: {running: false}, ...extra};
  vm.createContext(ctx);
  const src = 'globalThis.__d = ' + JSON.stringify(ctx.data) + ';\n' +
    fs.readFileSync(require('node:path').join(__dirname,'../src/static/js/today.js'),'utf8') +
    '\nToday.data = globalThis.__d; Today.render();';
  vm.runInContext(src, ctx);
  return {ctx, els};
}
(async()=>{
  const {els} = loadToday();
  const overview = els.get('todayOverview').innerHTML;
  assert.match(overview, /<b>188<\/b><span>待评分\/失败重试</);   // 45 queued + 143 failed
  assert.match(overview, /另有 73 篇已隔离/);
  const list = (els.get('todayProcessing').children || []).map(c => c.innerHTML).join('\n');
  assert.match(list, /评分失败·将重试/);
  assert.match(list, /排队等评分预算/);
  assert.match(list, /日期待核验/);
  console.log('today hold-reason UI check passed');
})().catch(e=>{console.error(e);process.exit(1);});
