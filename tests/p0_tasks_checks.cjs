const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const nodes = new Map();
function node(id) { return {id, textContent: '', innerHTML: '', parentNode: {insertBefore(){}}, appendChild(){}, querySelector(){return node('retry');}}; }
['statusBox', 'runResult', 'taskRefreshBtn'].forEach(id => nodes.set(id, node(id)));
const calls = [];
const context = {document: {readyState:'loading', addEventListener(){}, getElementById:id=>nodes.get(id), createElement:()=>node('new')},
  API: {esc:s=>String(s), errorMessage:e=>String(e), post:async(url,body)=>{calls.push({url,body}); return {ok:true,task_id:'test'};},
        get:async()=>({task:{last_status:'partial',run_count:3,success_count:2,partial_count:1,failure_count:0,
          last_stats:{score_attempted:100,score_deferred:1900,budget_completed:true,
          score_failed_backlog:62,
          rescore_selected:3,rescored_ok:2,rescored_failed:1,dates_completed:4}}})}, console};
vm.createContext(context);
vm.runInContext(fs.readFileSync(require('node:path').join(__dirname,'../src/static/js/tasks.js'),'utf8')+'\nglobalThis.Tasks=Tasks;', context);
(async()=>{
 await context.Tasks.trigger('light','preview');
 assert.equal(calls[0].body.run_mode,'preview');
 assert.match(nodes.get('statusBox').innerHTML,/部分失败/);
 assert.match(nodes.get('statusBox').innerHTML,/partial=1/);
 assert.match(nodes.get('statusBox').innerHTML,/1900/);
 assert.match(nodes.get('statusBox').innerHTML,/本轮预算已完成/);
 assert.match(nodes.get('statusBox').innerHTML,/重评成功=2/);
 assert.match(nodes.get('statusBox').innerHTML,/重评失败（保留原分数）=1/);
 assert.match(nodes.get('statusBox').innerHTML,/补齐发表日期=4/);
 assert.match(nodes.get('statusBox').innerHTML,/待重试评分=62/);
 assert.match(nodes.get('statusBox').innerHTML,/此前失败的评分待重试/);
 await context.Tasks.trigger('light','trial');
 assert.equal(calls[1].body.run_mode,'trial');
 console.log('P0 tasks: preview/trial request routing and deferred result display passed');
})().catch(e=>{console.error(e);process.exit(1);});
