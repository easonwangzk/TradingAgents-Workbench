import test from 'node:test';
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { readFile } from 'node:fs/promises';
import * as market from '../tradingagents/workbench/static/market-data.mjs';
const staticUrl = new URL('../tradingagents/workbench/static/', import.meta.url);
const require = createRequire(new URL('package.json', staticUrl));
const { JSDOM } = require('jsdom');
const flush = async () => { await new Promise(setImmediate); await new Promise(setImmediate); };

async function fixture(context, { symbols = [], initialVersion = 'v1', beforeRequest = async () => {}, fail = false, hidden = false, chartGate = Promise.resolve() } = {}) {
  assert.equal(typeof market.mountMarket, 'function', 'Market UI has not been implemented');
  const dom = new JSDOM(await readFile(new URL('index.html', staticUrl), 'utf8'), { url: 'https://fixture.test/', pretendToBeVisual: true });
  const document = dom.window.document;
  if (hidden) Object.defineProperty(document, 'hidden', { value: true, configurable: true });
  const calls = [], charts = [], prefills = [];
  let saved = symbols, version = initialVersion === null ? 0 : 1;
  const request = async (path, options = {}) => {
    calls.push({ path, options }); await beforeRequest(path, options);
    if (fail && path.endsWith('snapshot')) throw Object.assign(new Error('Provider unavailable'), { status: 503 });
    if (path.endsWith('watchlist')) {
      if (options.method === 'PUT') { saved = options.body.symbols; version += 1; }
      return { symbols: [...saved], version: version === 0 ? null : `v${version}` };
    }
    return { quotes: saved.map(symbol => ({ symbol, name: symbol === 'MSFT' ? '<img onerror=alert(1)> Microsoft' : 'Vanguard ETF', price: symbol === 'MSFT' ? 100 : 200, change: 1, change_percent: 0.5, currency: 'USD', exchange: 'TEST', as_of: '2026-10-09T20:00:00Z', open: 99, high: 101, low: 98, previous_close: 99, volume: null, ...(symbol === 'VOO' ? { asset_type: 'etf' } : {}) })), candles: [{ time: '2026-10-09', open: 99, high: 101, low: 98, close: 100, volume: null }], selected: options.body.selected, source: options.body.source, fetched_at: '2026-10-10T00:00:00Z', interval: '1d', errors: [] };
  };
  const view = market.mountMarket(document, { request, onPrefill: quote => prefills.push(quote), loadChart: async () => { await chartGate; return { mountMarketChart: () => ({ update: (...args) => charts.push(args), dispose: () => charts.push('disposed'), resize: () => {} }) }; } });
  context.after(() => { view.dispose(); dom.window.close(); });
  view.setSession('account-A');
  return { view, document, window: dom.window, calls, charts, prefills };
}

test('Market is a hidden sibling with compact list and unframed detail controls', async () => {
  const dom = new JSDOM(await readFile(new URL('index.html', staticUrl), 'utf8'));
  assert.ok(dom.window.document.querySelector('#workspace-navigation #market-entry'));
  assert.equal(dom.window.document.querySelector('#market-screen')?.hidden, true);
  assert.equal(dom.window.document.querySelectorAll('[data-market-range]').length, 5);
  assert.equal(dom.window.document.querySelectorAll('[data-market-mode]').length, 2);
  assert.ok(dom.window.document.querySelector('#market-chart'));
  dom.window.close();
});

test('new account null version enables Add and creates MSFT without a revision', async context => {
  const app = await fixture(context, { initialVersion: null });
  await app.view.open();
  assert.equal(app.view.state.loaded, true);
  assert.equal(app.document.querySelector('#market-status').textContent, '');
  assert.equal(app.document.querySelector('#market-add').disabled, false);
  app.document.querySelector('#market-symbol').value = 'MSFT';
  app.document.querySelector('#market-add').click(); await flush();
  const writes = app.calls.filter(call => call.options.method === 'PUT');
  assert.equal(writes.length, 1);
  assert.equal(Object.hasOwn(writes[0].options, 'revision'), false);
  assert.deepEqual(writes[0].options.body, { symbols: ['MSFT'] });
  assert.equal(app.view.state.version, 'v1');
  assert.equal(app.document.querySelector('#market-rows tr').dataset.symbol, 'MSFT');
  assert.equal(app.document.querySelector('#market-symbol').value, '');
});

test('hidden-first null watchlist enables Add on foreground and resets it on logout', async context => {
  const app = await fixture(context, { initialVersion: null, hidden: true });
  await app.view.open();
  assert.equal(app.view.state.loaded, false);
  assert.equal(app.document.querySelector('#market-add').disabled, true);
  Object.defineProperty(app.document, 'hidden', { value: false, configurable: true });
  app.document.dispatchEvent(new app.window.Event('visibilitychange')); await flush();
  assert.equal(app.document.querySelector('#market-add').disabled, false);
  app.window.dispatchEvent(new app.window.Event('pagehide'));
  app.window.dispatchEvent(new app.window.Event('pageshow')); await flush();
  assert.equal(app.calls.length, 1);
  app.view.setSession(null);
  assert.equal(app.view.state.loaded, false);
  assert.equal(app.document.querySelector('#market-add').disabled, true);
});

test('failed watchlist load leaves Add disabled until foreground successfully reloads', async context => {
  let fail = true;
  const app = await fixture(context, { initialVersion: null, beforeRequest: async path => {
    if (fail && path.endsWith('watchlist')) throw new Error('watchlist unavailable');
  } });
  await app.view.open();
  assert.equal(app.document.querySelector('#market-add').disabled, true);
  assert.equal(app.view.state.loaded, false);
  assert.match(app.document.querySelector('#market-status').textContent, /watchlist unavailable/);
  fail = false;
  app.window.dispatchEvent(new app.window.Event('pagehide'));
  app.window.dispatchEvent(new app.window.Event('pageshow')); await flush();
  assert.equal(app.document.querySelector('#market-add').disabled, false);
  assert.equal(app.document.querySelector('#market-status').textContent, '');
});

test('empty list has no invented defaults, supports add/delete/search/sort and exact quote fields', async context => {
  const app = await fixture(context);
  app.document.querySelector('#market-entry').click(); await flush();
  assert.match(app.document.querySelector('#market-empty').textContent, /自选列表为空/);
  assert.equal(app.calls.length, 1);
  const add = async value => { app.document.querySelector('#market-symbol').value = value; app.document.querySelector('#market-add-form').dispatchEvent(new app.window.Event('submit', { cancelable: true })); await flush(); };
  await add('msft'); await add('voo');
  assert.equal(app.document.querySelectorAll('#market-rows tr').length, 2);
  assert.equal(app.document.querySelector('#market-rows img'), null);
  assert.equal(app.document.querySelector('[data-market-field="volume"]').textContent, '--');
  const search = app.document.querySelector('#market-search');
  search.value = 'Vanguard'; search.dispatchEvent(new app.window.Event('input'));
  assert.equal(app.document.querySelectorAll('#market-rows tr').length, 1);
  search.value = ''; search.dispatchEvent(new app.window.Event('input'));
  app.document.querySelector('[data-market-sort="price"]').click();
  assert.equal(app.document.querySelector('#market-rows tr').dataset.symbol, 'MSFT');
  app.document.querySelector('[data-market-sort="price"]').click();
  assert.equal(app.document.querySelector('#market-rows tr').dataset.symbol, 'VOO');
  app.document.querySelector('[data-market-delete="VOO"]').click(); await flush();
  assert.equal(app.document.querySelectorAll('#market-rows tr').length, 1);
  assert.equal(app.calls.some(call => /runs|assistant|settings/.test(call.path)), false);
});

test('chart switches modes/ranges/source, shows dated daily provenance, prefills without submission', async context => {
  const app = await fixture(context, { symbols: ['MSFT', 'VOO'] });
  app.document.querySelector('#market-entry').click(); await flush();
  app.document.querySelector('[data-market-select="VOO"]').click(); await flush();
  assert.equal(app.document.querySelector('#market-screen').dataset.marketView, 'detail');
  app.document.querySelector('[data-market-mode="candles"]').click(); await flush();
  assert.equal(app.charts.at(-1)[1], 'candles');
  app.document.querySelector('[data-market-range="5y"]').click(); await flush();
  const source = app.document.querySelector('#market-source'); source.value = 'alpha_vantage'; source.dispatchEvent(new app.window.Event('change')); await flush();
  assert.equal(app.calls.at(-1).options.body.source, 'alpha_vantage');
  assert.equal(app.calls.at(-1).options.body.range, '5y');
  assert.match(app.document.querySelector('#market-provenance').textContent, /Alpha Vantage/);
  assert.match(app.document.querySelector('#market-provenance').textContent, /日线/);
  const count = app.calls.length;
  app.document.querySelector('#market-research').click();
  assert.equal(app.calls.length, count);
  assert.equal(app.prefills[0].symbol, 'VOO');
  assert.equal(app.prefills[0].asset_type, 'etf');
  app.document.querySelector('#market-back').click();
  assert.equal(app.document.querySelector('#market-screen').dataset.marketView, 'list');
});

test('late snapshot and chart imports never reopen hidden UI or corrupt another page', async context => {
  const deferred = Promise.withResolvers();
  const app = await fixture(context, { symbols: ['MSFT'], beforeRequest: async path => { if (path.endsWith('snapshot')) await deferred.promise; } });
  app.document.querySelector('#market-entry').click(); await flush();
  app.view.close();
  app.document.documentElement.dataset.screen = 'assistant';
  app.document.querySelector('#breadcrumb-studio').textContent = 'Research assistant';
  deferred.resolve(); await flush();
  assert.equal(app.document.querySelector('#market-screen').hidden, true);
  assert.equal(app.document.documentElement.dataset.screen, 'assistant');
  assert.equal(app.document.querySelector('#breadcrumb-studio').textContent, 'Research assistant');
  assert.equal(app.charts.length, 0);
  app.view.setSession(null);
  assert.equal(app.document.querySelector('#market-entry').hidden, true);
  assert.equal(app.document.querySelectorAll('#market-rows tr').length, 0);
});

test('provider failure remains honest and refresh is available without fake prices', async context => {
  const app = await fixture(context, { symbols: ['MSFT'], fail: true });
  app.document.querySelector('#market-entry').click(); await flush();
  assert.match(app.document.querySelector('#market-status').textContent, /Provider unavailable/);
  assert.equal(app.document.querySelector('[data-market-field="price"]').textContent, '--');
  assert.equal(app.document.querySelector('#market-refresh').disabled, false);
});

test('late failed edit cannot show an error or clear the next account draft', async context => {
  const deferred = Promise.withResolvers();
  const app = await fixture(context, { beforeRequest: async (path, options) => { if (options.method === 'PUT') await deferred.promise; } });
  await app.view.open();
  app.document.querySelector('#market-symbol').value = 'MSFT';
  app.document.querySelector('#market-add-form').dispatchEvent(new app.window.Event('submit', { cancelable: true }));
  await flush();
  app.view.setSession('account-B');
  app.document.querySelector('#market-symbol').value = 'NEXT-DRAFT';
  deferred.reject(new Error('old private error'));
  await flush();
  assert.equal(app.document.querySelector('#market-symbol').value, 'NEXT-DRAFT');
  assert.equal(app.document.querySelector('#market-status').textContent.includes('old private'), false);
});

test('current 401 immediately clears Market data and hides the account entry', async context => {
  const app = await fixture(context, { symbols: ['MSFT'], beforeRequest: async path => {
    if (path.endsWith('snapshot')) throw Object.assign(new Error('expired'), { status: 401 });
  } });
  await app.view.open();
  assert.equal(app.document.querySelector('#market-screen').hidden, true);
  assert.equal(app.document.querySelector('#market-entry').hidden, true);
  assert.equal(app.document.querySelectorAll('#market-rows tr').length, 0);
  assert.equal(app.document.querySelector('#market-selected').textContent, '--');
});

test('initially hidden page does not load Market until it becomes visible', async context => {
  const app = await fixture(context, { hidden: true });
  await app.view.open();
  assert.equal(app.calls.length, 0);
  assert.equal(app.view.state.loading, false);
  Object.defineProperty(app.document, 'hidden', { value: false, configurable: true });
  app.document.dispatchEvent(new app.window.Event('visibilitychange'));
  await flush();
  assert.equal(app.calls.length, 1);
  assert.equal(app.document.querySelector('#market-add').disabled, false);
});

test('late chart engine import is discarded after close and a new account', async context => {
  const deferred = Promise.withResolvers();
  const app = await fixture(context, { symbols: ['MSFT'], chartGate: deferred.promise });
  await app.view.open();
  app.view.close(); app.view.setSession('account-B');
  deferred.resolve(); await flush();
  assert.equal(app.charts.length, 0);
  assert.equal(app.document.querySelector('#market-screen').hidden, true);
});