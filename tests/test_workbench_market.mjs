import test from 'node:test';
import assert from 'node:assert/strict';
import { createApi } from '../tradingagents/workbench/static/ui-core.mjs';

let market;
try { market = await import('../tradingagents/workbench/static/market-data.mjs'); }
catch (error) { if (error.code !== 'ERR_MODULE_NOT_FOUND') throw error; }
const flush = async () => { await new Promise(setImmediate); await new Promise(setImmediate); };
const snapshot = selected => ({ quotes: [{ symbol: selected, price: 10, volume: null }], candles: [], selected, source: 'yfinance', fetched_at: '2026-10-10T00:00:00Z', interval: '1d', errors: [] });

test('market API is email-only, same-origin, CSRF guarded and versioned', async () => {
  const calls = [];
  const api = createApi({ emailMode: true, token: 'account-token', fetchImpl: async (path, options) => {
    calls.push({ path, options });
    return { ok: true, json: async () => ({ symbols: [], version: 'v1' }) };
  } });
  await api('/api/market/watchlist');
  await api('/api/market/watchlist', { method: 'PUT', body: { symbols: ['MSFT'] }, revision: 'v1' });
  await api('/api/market/snapshot', { method: 'POST', body: { symbols: ['MSFT'], selected: 'MSFT', range: '1m', source: 'yfinance' } });
  assert.equal(calls[1].options.headers['If-Match'], 'v1');
  assert.equal(calls[2].options.headers['X-Workbench-Token'], 'account-token');
  assert.equal(calls[2].options.credentials, 'same-origin');
  for (const path of ['/api/market/secrets', '/api/market/snapshot?key=secret', 'https://example.test/api/market/watchlist']) await assert.rejects(api(path));
  await assert.rejects(createApi({ fetchImpl: async () => assert.fail() })('/api/market/watchlist'));
  await assert.rejects(createApi({ emailMode: true })('/api/market/snapshot', { method: 'POST', body: {} }));
});

function service(context, request, clock) {
  assert.equal(typeof market?.createMarketService, 'function', 'Market service has not been implemented');
  const updates = [];
  const controller = market.createMarketService({ request, clock, onChange: value => updates.push(structuredClone(value)) });
  context.after(() => controller.dispose());
  controller.setSession('account-A');
  return { controller, updates };
}

test('watchlist begins empty without snapshot calls or fabricated symbols', async context => {
  const calls = [];
  const { controller } = service(context, async path => { calls.push(path); return { symbols: [], version: 'v1' }; });
  await controller.open();
  assert.deepEqual(controller.state.symbols, []);
  assert.deepEqual(calls, ['/api/market/watchlist']);
});

test('new account null version loads and creates MSFT without If-Match, then preserves v1', async context => {
  const calls = [];
  const api = createApi({ emailMode: true, token: 'account-token', fetchImpl: async (path, options) => {
    calls.push({ path, options });
    const result = path.endsWith('snapshot') ? snapshot('MSFT') : options.method === 'PUT'
      ? { symbols: ['MSFT'], version: calls.filter(call => call.options.method === 'PUT').length === 1 ? 'v1' : 'v2' }
      : { symbols: [], version: null };
    return { ok: true, json: async () => result };
  } });
  const { controller } = service(context, api);
  await controller.open();
  assert.equal(controller.state.loaded, true);
  assert.equal(controller.state.error, '');
  assert.equal(controller.state.version, null);
  controller.setVisible(false); controller.setVisible(true); await flush();
  assert.equal(calls.length, 1);
  await controller.save(['MSFT']);
  assert.equal(Object.hasOwn(calls[1].options.headers, 'If-Match'), false);
  assert.deepEqual(JSON.parse(calls[1].options.body), { symbols: ['MSFT'] });
  assert.deepEqual(controller.state.symbols, ['MSFT']);
  assert.equal(controller.state.version, 'v1');
  await controller.save(['MSFT']);
  assert.equal(calls.findLast(call => call.options.method === 'PUT').options.headers['If-Match'], 'v1');
});

test('watchlist versions reject missing, empty, nonstring and oversized values without phantom loading', async context => {
  for (const version of [undefined, '', 1, false, {}, 'v'.repeat(257)]) {
    let response = { symbols: ['PRIVATE'], version };
    const calls = [];
    const { controller } = service(context, async (path, options) => { calls.push({ path, options }); return response; });
    await controller.open();
    assert.equal(controller.state.loaded, false);
    assert.deepEqual(controller.state.symbols, []);
    await controller.save(['MSFT']);
    assert.equal(calls.length, 1);
    response = { symbols: [], version: null };
    controller.setVisible(false); controller.setVisible(true); await flush();
    assert.equal(controller.state.loaded, true);
    assert.equal(controller.state.error, '');
  }
});

test('reopening an existing watchlist with a failed GET disables edits until a successful load', async context => {
  let fail = false;
  const calls = [];
  const { controller } = service(context, async (path, options) => {
    calls.push({ path, options });
    if (fail) throw new Error('read failed');
    return { symbols: [], version: 'v1' };
  });
  await controller.open();
  fail = true; await controller.open();
  assert.equal(controller.state.loaded, false);
  await controller.save(['MSFT']);
  assert.equal(calls.length, 2);
  fail = false; controller.setVisible(false); controller.setVisible(true); await flush();
  assert.equal(controller.state.loaded, true);
});

test('PUT requires a bounded nonempty response version and leaves the prior record intact on failure', async context => {
  for (const version of [null, undefined, '', 1, 'v'.repeat(257)]) {
    const { controller } = service(context, async (path, options) => options.method === 'PUT'
      ? { symbols: ['MSFT'], version } : { symbols: [], version: 'v1' });
    await controller.open(); await controller.save(['MSFT']);
    assert.deepEqual(controller.state.symbols, []);
    assert.equal(controller.state.version, 'v1');
    assert.notEqual(controller.state.error, '');
    assert.equal(controller.state.loading, false);
    assert.equal(controller.state.saving, false);
  }
});

for (const status of [409, 412]) test(`watchlist conflict ${status} preserves revision and never retries PUT`, async context => {
  const calls = [];
  const { controller } = service(context, async (path, options) => {
    calls.push({ path, options });
    if (options.method === 'PUT') throw Object.assign(new Error('conflict'), { status });
    return { symbols: [], version: 'v1' };
  });
  await controller.open(); await controller.save(['MSFT']); await flush();
  assert.equal(calls.length, 2);
  assert.equal(calls[1].options.revision, 'v1');
  assert.equal(controller.state.version, 'v1');
  assert.deepEqual(controller.state.symbols, []);
  assert.match(controller.state.error, /其他窗口/);
});

for (const operation of ['GET', 'PUT']) test(`hidden cancellation of ${operation} permits a fresh load without stale state or automatic PUT retry`, async context => {
  const deferred = Promise.withResolvers();
  const calls = [];
  const { controller } = service(context, async (path, options) => {
    calls.push({ path, options });
    if (calls.length === (operation === 'GET' ? 1 : 2)) return deferred.promise;
    return { symbols: [], version: null };
  });
  if (operation === 'GET') void controller.open();
  else { await controller.open(); void controller.save(['MSFT']); }
  await flush();
  assert.equal(controller.state.loading, true);
  controller.setVisible(false);
  assert.equal(controller.state.loading, false);
  assert.equal(controller.state.saving, false);
  assert.equal(controller.state.loaded, false);
  controller.setVisible(true); await flush();
  assert.equal(controller.state.loaded, true);
  deferred.resolve({ symbols: ['STALE'], version: 'old' }); await flush();
  assert.deepEqual(controller.state.symbols, []);
  assert.equal(controller.state.version, null);
  assert.equal(calls.filter(call => call.options.method === 'PUT').length, operation === 'PUT' ? 1 : 0);
});

test('account switch and logout reset loaded independently of revision', async context => {
  const { controller } = service(context, async () => ({ symbols: [], version: null }));
  await controller.open();
  assert.equal(controller.state.loaded, true);
  controller.setSession('account-B');
  assert.equal(controller.state.loaded, false);
  await controller.open(); controller.setSession(null);
  assert.equal(controller.state.loaded, false);
});

test('256-character existing revision is accepted and passed unchanged to PUT', async context => {
  const calls = [];
  const { controller } = service(context, async (path, options) => {
    calls.push({ path, options });
    return { symbols: [], version: 'v'.repeat(256) };
  });
  await controller.open(); await controller.save([]);
  assert.equal(controller.state.error, '');
  assert.equal(calls[1].options.revision, 'v'.repeat(256));
});

test('paused auto refresh does not block a deferred initial watchlist load', async context => {
  const calls = [];
  const { controller } = service(context, async path => { calls.push(path); return { symbols: [], version: null }; });
  controller.setVisible(false); controller.setPaused(true);
  await controller.open();
  assert.equal(calls.length, 0);
  controller.setVisible(true); await flush();
  assert.equal(controller.state.loaded, true);
  assert.equal(controller.state.paused, true);
  assert.deepEqual(calls, ['/api/market/watchlist']);
});

test('watchlist save normalizes symbols, enforces max12 and uses optimistic version', async context => {
  const calls = [];
  const { controller } = service(context, async (path, options) => {
    calls.push({ path, options });
    if (path.endsWith('snapshot')) return snapshot(options.body.selected);
    return { symbols: options?.body?.symbols || [], version: options?.body ? 'v2' : 'v1' };
  });
  await controller.open();
  await controller.save([' msft ', 'voo']);
  assert.deepEqual(controller.state.symbols, ['MSFT', 'VOO']);
  assert.equal(calls[1].options.revision, 'v1');
  assert.deepEqual(calls[2].options.body, { symbols: ['MSFT', 'VOO'], selected: 'MSFT', range: '1m', source: 'yfinance' });
  await assert.rejects(controller.save(Array.from({ length: 13 }, (_, index) => `S${index}`)));
  await assert.rejects(controller.save(['MSFT', 'msft']));
  await assert.rejects(controller.save(['bad symbol']));
});

test('selection, range and provider fence out-of-order snapshots and preserve nulls', async context => {
  const pending = [];
  const { controller } = service(context, async (path, options) => {
    if (path.endsWith('watchlist')) return { symbols: ['MSFT', 'VOO'], version: 'v1' };
    const deferred = Promise.withResolvers();
    pending.push({ ...deferred, body: options.body });
    return deferred.promise;
  });
  void controller.open();
  await flush();
  void controller.select('VOO');
  await flush();
  pending[1].resolve(snapshot('VOO'));
  await flush();
  pending[0].resolve(snapshot('MSFT'));
  await flush();
  assert.equal(controller.state.snapshot.selected, 'VOO');
  assert.equal(controller.state.snapshot.quotes[0].volume, null);
  void controller.configure({ range: '5y', source: 'alpha_vantage' });
  await flush();
  assert.equal(pending[2].body.range, '5y');
  assert.equal(pending[2].body.source, 'alpha_vantage');
  await assert.rejects(controller.configure({ range: 'realtime' }));
});

for (const operation of ['close', 'logout', 'switch']) test(`late success and errors cannot restore data after ${operation}`, async context => {
  const deferred = Promise.withResolvers();
  const { controller, updates } = service(context, async () => deferred.promise);
  void controller.open();
  if (operation === 'close') controller.close();
  else controller.setSession(operation === 'logout' ? null : 'account-B');
  const count = updates.length;
  deferred.resolve({ symbols: ['PRIVATE'], version: 'old' });
  await flush();
  assert.equal(updates.length, count);
  assert.deepEqual(controller.state.symbols, []);
});

test('auto refresh is bounded, single-flight and only visible while unpaused', async context => {
  const timers = new Map();
  let sequence = 0;
  const clock = { setTimeout: (task, delay) => { assert.equal(delay, 60000); timers.set(++sequence, task); return sequence; }, clearTimeout: id => timers.delete(id) };
  let reads = 0;
  const { controller } = service(context, async (path, options) => {
    if (path.endsWith('watchlist')) return { symbols: ['MSFT'], version: 'v1' };
    reads += 1;
    return snapshot(options.body.selected);
  }, clock);
  await controller.open();
  assert.equal(timers.size, 1);
  controller.setVisible(false);
  assert.equal(timers.size, 0);
  controller.setVisible(true);
  controller.setPaused(true);
  assert.equal(timers.size, 0);
  controller.setPaused(false);
  const [id, tick] = [...timers][0]; timers.delete(id); await tick();
  assert.equal(reads, 2);
  assert.equal(timers.size, 1);
  controller.close();
  assert.equal(timers.size, 0);
});