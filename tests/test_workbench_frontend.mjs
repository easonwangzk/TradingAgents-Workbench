import test from 'node:test';
import assert from 'node:assert/strict';

const moduleUrl = new URL('../tradingagents/workbench/static/ui-core.mjs', import.meta.url);
let core;
try {
  core = await import(moduleUrl);
} catch (error) {
  if (error.code !== 'ERR_MODULE_NOT_FOUND' || !error.message.includes('ui-core.mjs')) throw error;
}

const config = {
  defaults: {
    mode: 'analysis', ticker: 'MSFT', analysis_date: '2026-09-08', asset_type: 'stock',
    analysts: ['market', 'social', 'news', 'fundamentals'], debate_rounds: 1, risk_rounds: 1,
    language: 'Chinese', data_vendors: {
      core_stock_apis: 'yfinance', technical_indicators: 'yfinance',
      fundamental_data: 'yfinance', news_data: 'yfinance', macro_data: 'fred',
      prediction_markets: 'polymarket',
    },
  },
  analyst_choices: ['market', 'social', 'news', 'fundamentals'].map(value => ({ value, label: value })),
  vendor_choices: {
    core_stock_apis: ['yfinance', 'alpha_vantage'], technical_indicators: ['yfinance', 'alpha_vantage'],
    fundamental_data: ['yfinance', 'alpha_vantage'], news_data: ['yfinance', 'alpha_vantage'],
    macro_data: ['fred'], prediction_markets: ['polymarket'],
  },
  model: 'gpt-5.6-sol', deployment_ready: true, csrf_token: 'test-process-token',
};
const record = {
  id: 'fcd47836-8555-4dcf-a016-4668182e4d64', request: config.defaults,
  status: 'collecting', created_at: '2026-09-08T08:00:00Z', execution_name: 'research-test',
  events: [], result: null, error: null,
};

test('the actual UI boundary module is available to the application', () => {
  assert.ok(core, 'ui-core.mjs has not been implemented');
});

test('worker debate report paths have readable Chinese section labels', () => {
  const labels = {
    'investment_debate_state.bull_history': '\u591a\u65b9\u89c2\u70b9',
    'investment_debate_state.bear_history': '\u7a7a\u65b9\u89c2\u70b9',
    'investment_debate_state.judge_decision': '\u7814\u7a76\u88c1\u51b3',
    'risk_debate_state.aggressive_history': '\u6fc0\u8fdb\u98ce\u9669\u89c2\u70b9',
    'risk_debate_state.conservative_history': '\u4fdd\u5b88\u98ce\u9669\u89c2\u70b9',
    'risk_debate_state.neutral_history': '\u4e2d\u6027\u98ce\u9669\u89c2\u70b9',
    'risk_debate_state.judge_decision': '\u98ce\u9669\u88c1\u51b3',
  };
  for (const [section, label] of Object.entries(labels)) assert.equal(core.SECTION_LABELS[section], label);
});

test('request uses defaults, normalizes ticker and analysts, and excludes unknown fields', { skip: !core }, () => {
  const request = core.buildRequest({ ticker: ' msft ', analysts: ['news', 'market'], model: 'other' }, config, '2026-09-08');
  assert.deepEqual(request, { ...config.defaults, ticker: 'MSFT', analysts: ['market', 'news'] });
  request.data_vendors.news_data = 'alpha_vantage';
  assert.equal(config.defaults.data_vendors.news_data, 'yfinance');
});

for (const localDay of [7, 9]) {
  test(`request date validation uses frozen UTC when local calendar day is ${localDay}`, { skip: !core }, context => {
    context.mock.timers.enable({ apis: ['Date'], now: new Date('2026-09-08T16:30:00Z') });
    context.mock.method(Date.prototype, 'getFullYear', () => 2026);
    context.mock.method(Date.prototype, 'getMonth', () => 8);
    context.mock.method(Date.prototype, 'getDate', () => localDay);
    assert.equal(core.buildRequest({ analysis_date: '2026-09-08' }, config).analysis_date, '2026-09-08');
    assert.throws(() => core.buildRequest({ analysis_date: '2026-09-09' }, config), error => error.field === 'analysis_date');
  });
}

test('stock and ETF requests match the Python contract', { skip: !core }, () => {
  assert.equal(core.buildRequest({ asset_type: 'etf', ticker: 'VOO' }, config, '2026-09-08').asset_type, 'etf');
  assert.throws(() => core.buildRequest({ asset_type: 'etf', data_vendors: { fundamental_data: 'alpha_vantage' } }, config, '2026-09-08'), /yfinance/);
  for (const asset_type of ['forex', 'crypto', 'index']) {
    assert.throws(() => core.buildRequest({ asset_type }, config, '2026-09-08'), /asset_type/);
  }
});

test('request rejects invalid identifiers, dates, rounds, modes and analyst selections', { skip: !core }, () => {
  for (const fields of [
    { ticker: 'MSFT;exit' }, { ticker: '' }, { ticker: 'A'.repeat(25) },
    { analysis_date: '2026-02-30' }, { analysis_date: '2026-09-09' },
    { debate_rounds: 0 }, { risk_rounds: 4 }, { debate_rounds: 1.5 }, { risk_rounds: true },
    { analysts: [] }, { analysts: ['market', 'market'] }, { analysts: ['other'] },
    { mode: 'trade' }, { language: 'French' },
  ]) assert.throws(() => core.buildRequest(fields, config, '2026-09-08'));
  assert.equal(core.buildRequest({ ticker: '^gspc', mode: 'connectivity' }, config, '2026-09-08').ticker, '^GSPC');
});

test('vendor options expose single providers and both explicit fallback orders', { skip: !core }, () => {
  assert.deepEqual(core.vendorChains(['yfinance', 'alpha_vantage']), [
    'yfinance', 'alpha_vantage', 'yfinance,alpha_vantage', 'alpha_vantage,yfinance',
  ]);
  assert.deepEqual(core.vendorChains(['fred']), ['fred']);
});

test('vendor chains normalize whitespace and reject unknown, duplicate or empty providers', { skip: !core }, () => {
  const request = core.buildRequest({ data_vendors: { news_data: ' alpha_vantage, yfinance ' } }, config, '2026-09-08');
  assert.equal(request.data_vendors.news_data, 'alpha_vantage,yfinance');
  assert.equal(request.data_vendors.macro_data, 'fred');
  for (const chain of ['', 'fred', 'yfinance,yfinance', 'yfinance,']) {
    assert.throws(() => core.buildRequest({ data_vendors: { news_data: chain } }, config, '2026-09-08'));
  }
  assert.throws(() => core.buildRequest({ data_vendors: { secret: 'secret' } }, config, '2026-09-08'));
});

test('no selected record yields an empty workspace without invented stats or reports', { skip: !core }, () => {
  const view = core.projectRun(null);
  assert.deepEqual(view.reports, []);
  assert.equal(view.stats.input_tokens, null);
  assert.equal(view.decision, '');
  assert.equal(view.active, false);
});

test('analysis connectivity snapshots survive incomplete, malformed and empty final records', () => {
  const first = { provider: 'yfinance', status: 'ok', detail: 'Prices available', elapsed_seconds: 0.2 };
  const second = { provider: 'fred', status: 'error', detail: 'Provider request failed.', elapsed_seconds: 1 };
  const events = [
    { kind: 'connectivity', payload: { connectivity: [first] } },
    { kind: 'connectivity', payload: { connectivity: [first, second] } },
    { kind: 'connectivity', payload: { connectivity: null } },
    { kind: 'connectivity', payload: { connectivity: [null, false] } },
  ];
  for (const status of ['running', 'collecting', 'failed', 'stopped', 'succeeded']) {
    const view = core.projectRun({ ...record, status, events, result: { reports: {}, connectivity: [] } });
    assert.deepEqual(view.connectivity, [first, second]);
    assert.equal(view.decision, '');
  }
  const finalRows = [{ ...second, status: 'ok' }];
  assert.deepEqual(core.projectRun({ ...record, events, result: { connectivity: finalRows } }).connectivity, finalRows);
  assert.deepEqual(core.projectRun({ ...record, events: [...events,
    { kind: 'result', payload: { connectivity: finalRows } },
  ] }).connectivity, finalRows);
  assert.deepEqual(core.projectRun({ ...record, status: 'succeeded', result: { connectivity: [] } }).connectivity, []);
});

test('collecting remains active; repeated section events replace text without summing usage snapshots', { skip: !core }, () => {
  const view = core.projectRun({ ...record, events: [
    { kind: 'phase', payload: { stage: 'risk' }, timestamp: '2026-09-08T08:01:00Z' },
    { kind: 'report', payload: { section: 'market_report', content: 'first' } },
    { kind: 'report', payload: { section: 'market_report', content: '<img src=x onerror=alert(1)>' } },
    { kind: 'report', payload: { section: '__proto__', content: 'still a section' } },
    { kind: 'usage', payload: { llm_calls: 2, input_tokens: 20, output_tokens: 0 } },
    { kind: 'usage', payload: { llm_calls: 3, input_tokens: 40 } },
  ] });
  assert.equal(view.active, true);
  assert.equal(view.stage, 'risk');
  assert.deepEqual(view.reports, [
    { section: 'market_report', content: '<img src=x onerror=alert(1)>' },
    { section: '__proto__', content: 'still a section' },
  ]);
  assert.deepEqual(view.stats, { llm_calls: 3, tool_calls: null, input_tokens: 40, output_tokens: 0 });
  assert.equal(view.decision, '');
});

test('final result overrides provisional content and retains every returned section', { skip: !core }, () => {
  const view = core.projectRun({ ...record, status: 'succeeded', events: [
    { kind: 'report', payload: { section: 'market_report', content: 'partial' } },
    { kind: 'usage', payload: { input_tokens: 100 } },
  ], result: {
    decision: 'HOLD', reports: { market_report: 'final', custom_section: 'custom' },
    stats: { llm_calls: 4, tool_calls: 5, input_tokens: 0, output_tokens: 30 },
    connectivity: [{ provider: 'fred', status: 'unconfigured', detail: 'Missing key', elapsed_seconds: 0 }],
  } });
  assert.equal(view.active, false);
  assert.deepEqual(view.reports.map(item => item.content), ['final', 'custom']);
  assert.equal(view.stats.input_tokens, 0);
  assert.equal(view.connectivity[0].status, 'unconfigured');
  assert.equal(view.decision, 'HOLD');
});

test('result and error events are visible before the result field is persisted', { skip: !core }, () => {
  const view = core.projectRun({ ...record, events: [
    { kind: 'result', payload: { decision: 'WAIT', reports: { risk: 'text' }, stats: { tool_calls: 7 }, connectivity: [] } },
    { kind: 'error', payload: { message: 'provider unavailable' } },
  ] });
  assert.equal(view.decision, 'WAIT');
  assert.equal(view.error, 'provider unavailable');
  assert.equal(view.stats.tool_calls, 7);
  assert.equal(view.active, true);
});

test('malformed event payloads and invalid token counts do not crash or fabricate metrics', { skip: !core }, () => {
  const view = core.projectRun({ ...record, events: [null, { kind: 'phase' },
    { kind: 'usage', payload: { input_tokens: -3, tool_calls: '5', output_tokens: Infinity } },
    { kind: 'report', payload: { section: 'bad', content: {} } },
  ] });
  assert.deepEqual(view.reports, []);
  assert.equal(view.stats.input_tokens, null);
});

test('history searches ticker, date, mode, execution, id and Chinese status without reordering input', { skip: !core }, () => {
  const runs = [record, { ...record, id: 'second', status: 'failed', request: { ...config.defaults, ticker: 'AAPL' } }];
  assert.equal(core.filterRuns(runs, 'msft')[0].id, record.id);
  assert.equal(core.filterRuns(runs, '失败')[0].id, 'second');
  assert.equal(core.filterRuns(runs, '2026-09-08').length, 2);
  assert.equal(core.filterRuns(runs, 'nothing').length, 0);
  assert.equal(runs.length, 2);
});

test('run polling includes uncertain execution; history has its own 30-second cadence', { skip: !core }, () => {
  for (const status of ['starting', 'running', 'collecting', 'preparing', 'prepared', 'reconciliation_required']) {
    assert.deepEqual(core.pollDelays(status, true), { run: 5000, history: 30000 });
    assert.deepEqual(core.pollDelays(status, false), { run: null, history: null });
  }
  for (const status of ['succeeded', 'failed', 'stopped', undefined]) {
    assert.deepEqual(core.pollDelays(status, true), { run: null, history: 30000 });
  }
  assert.equal(core.projectRun({ ...record, status: 'reconciliation_required' }).active, false);
});

test('API writes use the per-process header and exact JSON; reads remain same origin', { skip: !core }, async () => {
  const calls = [];
  const api = core.createApi({ token: config.csrf_token, fetchImpl: async (url, options) => {
    calls.push({ url, options });
    return { ok: true, status: 202, json: async () => record };
  } });
  assert.equal((await api('/api/runs', { method: 'POST', body: config.defaults })).id, record.id);
  assert.equal(calls[0].options.headers['X-Workbench-Token'], 'test-process-token');
  assert.equal(calls[0].options.credentials, 'same-origin');
  assert.deepEqual(JSON.parse(calls[0].options.body), config.defaults);
  await api('/api/runs');
  assert.equal(calls[1].options.headers['X-Workbench-Token'], undefined);
  await assert.rejects(api('https://example.com/api/runs'), /same-origin/);
  await assert.rejects(api('//example.com/api/runs'), /same-origin/);
});

test('email API allows only same-origin assistant session routes and PATCH renames', { skip: !core }, async () => {
  const calls = [];
  const api = core.createApi({ token: config.csrf_token, emailMode: true, fetchImpl: async (url, options) => {
    calls.push({ url, options });
    return { ok: true, status: 200, json: async () => ({ id: record.id }) };
  } });
  const path = `/api/assistant/sessions/${record.id}`;
  await api(path);
  await api(path, { method: 'PATCH', body: { title: 'MSFT history' } });
  assert.equal(calls[1].options.headers['X-Workbench-Token'], 'test-process-token');
  assert.deepEqual(JSON.parse(calls[1].options.body), { title: 'MSFT history' });
  await assert.rejects(api('/api/assistant/sessions/not-a-uuid'), /same-origin/);
  await assert.rejects(api('https://attacker.example/api/assistant/sessions'), /same-origin/);
});

test('API rejects missing CSRF before a write and reports bounded errors without raw server bodies', { skip: !core }, async () => {
  const noToken = core.createApi({ fetchImpl: async () => { throw new Error('must not send'); } });
  await assert.rejects(noToken('/api/runs', { method: 'POST', body: {} }), /令牌/);
  const api = core.createApi({ fetchImpl: async () => ({ ok: false, status: 503, json: async () => ({ detail: 'private stack' }) }) });
  await assert.rejects(api('/api/runs'), error => error.status === 503 && !error.message.includes('private stack'));
});

test('download URLs encode run identifiers and accept only declared export formats', { skip: !core }, () => {
  assert.equal(core.downloadUrl(record.id, 'markdown'), `/api/runs/${record.id}/download?format=markdown`);
  assert.throws(() => core.downloadUrl('../escape', 'json'));
  assert.throws(() => core.downloadUrl(record.id, 'html'));
});