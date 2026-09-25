const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const apiSource = fs.readFileSync(path.join(__dirname, '../src/static/js/api.js'), 'utf8');
const librarySource = fs.readFileSync(path.join(__dirname, '../src/static/js/library.js'), 'utf8');
const trendsSource = fs.readFileSync(path.join(__dirname, '../src/static/js/trends.js'), 'utf8');

const elements = new Map([
  ['trendMonthly', { innerHTML: '' }],
  ['trendJournals', { innerHTML: '' }],
  ['trendVia', { innerHTML: '' }],
  ['trendRead', { innerHTML: '' }],
]);
const response = {
  monthly: [{
    month: '2026-09', total: 5, model_scored: 2, model_relevant: 1,
    manual_scored: 1, unknown_scored: 1,
  }],
  journals: [{
    journal: 'Digital Discovery', total: 5, model_scored: 2, model_relevant: 1,
    manual_scored: 1, unknown_scored: 1,
  }],
  discovered_via: [], read_status: [],
};
const context = {
  console,
  document: { getElementById: (id) => elements.get(id) },
  fetch: async () => { throw new Error('unexpected fetch'); },
  localStorage: { getItem: () => '', setItem: () => {} },
  location: { origin: 'http://127.0.0.1' },
};
vm.createContext(context);
vm.runInContext(`${apiSource}\nglobalThis.API = API;`, context);
context.API.get = async () => response;
vm.runInContext(`${librarySource}\nglobalThis.Library = Library;`, context);
vm.runInContext(`${trendsSource}\nglobalThis.Trends = Trends;`, context);

(async () => {
  assert.equal(context.API.casZoneLabel(2), '2区');
  assert.equal(context.API.casZoneLabel('4'), '4区');
  assert.equal(context.API.casZoneLabel('Q2'), '');
  const journal = context.Library.journalMeta({
    journal: 'Digital Discovery', impact_factor: 7.1, cas_zone: 2,
  }).join(' ');
  assert.match(journal, /中科院 2区/);
  assert.doesNotMatch(journal, /Q2/);

  await context.Trends.load();
  assert.match(elements.get('trendMonthly').innerHTML, /模型相关 1\/2/);
  assert.match(elements.get('trendMonthly').innerHTML, /手动 1/);
  assert.match(elements.get('trendMonthly').innerHTML, /来源未标记 1/);
  assert.match(elements.get('trendJournals').innerHTML, /模型相关 1\/2/);
  console.log('journal zone and score-origin UI checks passed');
})().catch((error) => {
  console.error(error);
  process.exit(1);
});
