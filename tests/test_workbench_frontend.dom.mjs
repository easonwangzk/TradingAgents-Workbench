import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { createRequire } from 'node:module';

const staticUrl = new URL('../tradingagents/workbench/static/', import.meta.url);
const require = createRequire(new URL('package.json', staticUrl));
const { JSDOM } = require('jsdom');
let application;
try {
  application = await import(new URL('app.js', staticUrl));
} catch (error) {
  if (error.code !== 'ERR_MODULE_NOT_FOUND' || !error.message.includes('app.js')) throw error;
}
const defaults = {
  mode: 'analysis', ticker: 'MSFT', analysis_date: '2026-09-08', asset_type: 'stock',
  analysts: ['market', 'social', 'news', 'fundamentals'], debate_rounds: 1, risk_rounds: 1,
  language: 'Chinese', data_vendors: {
    core_stock_apis: 'yfinance', technical_indicators: 'yfinance', fundamental_data: 'yfinance',
    news_data: 'yfinance', macro_data: 'fred', prediction_markets: 'polymarket',
  },
};
const config = {
  defaults, analyst_choices: defaults.analysts.map(value => ({ value, label: value })),
  vendor_choices: {
    core_stock_apis: ['yfinance', 'alpha_vantage'], technical_indicators: ['yfinance', 'alpha_vantage'],
    fundamental_data: ['yfinance', 'alpha_vantage'], news_data: ['yfinance', 'alpha_vantage'],
    macro_data: ['fred', 'alpha_vantage'], prediction_markets: ['polymarket'],
  },
  model: 'gpt-5.6-sol', deployment_ready: true, csrf_token: 'test-csrf-token',
};
const emailProviderOrder = [
  'openai', 'anthropic', 'google', 'azure', 'xai', 'deepseek',
  'qwen', 'qwen-cn', 'glm', 'glm-cn', 'minimax', 'minimax-cn',
  'openrouter', 'mistral', 'kimi', 'groq', 'nvidia', 'openai_compatible',
];
const providerFixture = ({ label, endpoint_kind = 'fixed', base_url, quick = [], deep = [], available_managed_identity = false }) => ({
  label,
  endpoint_kind,
  ...(base_url === undefined ? {} : { base_url }),
  available_managed_identity,
  key_required: true,
  custom_models: true,
  models: {
    quick: quick.map(([optionLabel, value]) => ({ label: optionLabel, value })),
    deep: deep.map(([optionLabel, value]) => ({ label: optionLabel, value })),
  },
});
const emailProviders = {
  openai: providerFixture({ label: 'OpenAI', endpoint_kind: 'approved_origin', base_url: 'https://api.openai.com/v1', quick: [['gpt-5-mini', 'gpt-5-mini']], deep: [['gpt-5', 'gpt-5']] }),
  anthropic: providerFixture({ label: 'Anthropic', quick: [['claude-haiku-4-5', 'claude-haiku-4-5']], deep: [['claude-sonnet-5', 'claude-sonnet-5']] }),
  google: providerFixture({ label: 'Google', quick: [['gemini-3.5-flash', 'gemini-3.5-flash']], deep: [['gemini-3.1-pro-preview', 'gemini-3.1-pro-preview']] }),
  azure: providerFixture({ label: 'Azure OpenAI', endpoint_kind: 'azure', available_managed_identity: true, quick: [['azure-gpt-5-mini', 'azure-gpt-5-mini']], deep: [['azure-gpt-5', 'azure-gpt-5']] }),
  xai: providerFixture({ label: 'xAI', quick: [['grok-4.3', 'grok-4.3']], deep: [['grok-4.20-0309-reasoning', 'grok-4.20-0309-reasoning']] }),
  deepseek: providerFixture({ label: 'DeepSeek', quick: [['deepseek-v4-flash', 'deepseek-v4-flash']], deep: [['deepseek-v4-pro', 'deepseek-v4-pro']] }),
  qwen: providerFixture({ label: 'Qwen', endpoint_kind: 'approved_origin', base_url: 'https://dashscope-intl.aliyuncs.com/compatible-mode/v1', quick: [['qwen3.7-plus', 'qwen3.7-plus']], deep: [['qwen3.7-max', 'qwen3.7-max']] }),
  'qwen-cn': providerFixture({ label: 'Qwen CN', endpoint_kind: 'approved_origin', base_url: 'https://dashscope.aliyuncs.com/compatible-mode/v1', quick: [['qwen3.7-plus', 'qwen3.7-plus']], deep: [['qwen3.7-max', 'qwen3.7-max']] }),
  glm: providerFixture({ label: 'GLM', endpoint_kind: 'approved_origin', base_url: 'https://api.z.ai/api/paas/v4/', quick: [['glm-5.3-flash', 'glm-5.3-flash']], deep: [['glm-5.3', 'glm-5.3']] }),
  'glm-cn': providerFixture({ label: 'GLM CN', endpoint_kind: 'approved_origin', base_url: 'https://open.bigmodel.cn/api/paas/v4/', quick: [['glm-5.3-flash', 'glm-5.3-flash']], deep: [['glm-5.3', 'glm-5.3']] }),
  minimax: providerFixture({ label: 'MiniMax', endpoint_kind: 'approved_origin', base_url: 'https://api.minimax.io/v1', quick: [['MiniMax-M3', 'MiniMax-M3']], deep: [['MiniMax-M2.7', 'MiniMax-M2.7']] }),
  'minimax-cn': providerFixture({ label: 'MiniMax CN', endpoint_kind: 'approved_origin', base_url: 'https://api.minimaxi.com/v1', quick: [['MiniMax-M3', 'MiniMax-M3']], deep: [['MiniMax-M2.7', 'MiniMax-M2.7']] }),
  openrouter: providerFixture({ label: 'OpenRouter', endpoint_kind: 'approved_origin', base_url: 'https://openrouter.ai/api/v1', quick: [['custom', 'custom']], deep: [['custom', 'custom']] }),
  mistral: providerFixture({ label: 'Mistral', endpoint_kind: 'approved_origin', base_url: 'https://api.mistral.ai/v1', quick: [['custom', 'custom']], deep: [['custom', 'custom']] }),
  kimi: providerFixture({ label: 'Kimi', endpoint_kind: 'approved_origin', base_url: 'https://api.moonshot.ai/v1', quick: [['custom', 'custom']], deep: [['custom', 'custom']] }),
  groq: providerFixture({ label: 'Groq', endpoint_kind: 'approved_origin', base_url: 'https://api.groq.com/openai/v1', quick: [['custom', 'custom']], deep: [['custom', 'custom']] }),
  nvidia: providerFixture({ label: 'NVIDIA', endpoint_kind: 'approved_origin', base_url: 'https://integrate.api.nvidia.com/v1', quick: [['custom', 'custom']], deep: [['custom', 'custom']] }),
  openai_compatible: providerFixture({ label: '兼容 OpenAI 接口', endpoint_kind: 'approved_origin', quick: [['custom', 'custom']], deep: [['custom', 'custom']] }),
};
const managedIdentityConfiguration = {
  deployment: 'gpt-5.6-sol',
  endpoint: 'https://fixture-foundry.services.ai.azure.com/',
  model_family: 'gpt-5.6',
  model_version: '2026-07-09',
};
const emailCatalog = {
  providers: emailProviders,
  modes: { api_key: { admin_only: false }, managed_identity: { admin_only: true, available: true, configuration: managedIdentityConfiguration } },
  data_vendors: config.vendor_choices,
  data_keys: ['alpha_vantage', 'fred'],
  approved_origins: ['https://api.openai.com'],
  compatible_paths: ['/v1', '/v1/'],
};
const emailSettings = {
  version: 'settings-v1', revision: 'settings-r1', mode: 'api_key', provider: 'openai',
  quick_model: 'gpt-5-mini', deep_model: 'gpt-5', asset_type: 'stock',
  data_vendors: structuredClone(defaults.data_vendors), data_key_configured: { alpha_vantage: false, fred: false },
  providers: {
    openai: {
      quick_model: 'gpt-5-mini', deep_model: 'gpt-5',
      base_url: 'https://api.openai.com/v1', key_configured: true,
    },
  },
};
const emailBudget = {
  month: '2026-09', limit_microusd: 300000000, spent_microusd: 1250000, reserved_microusd: 500000,
  available_microusd: 298250000, blocked: false, reset_at: '2026-10-01T00:00:00Z',
};
const run = {
  id: 'fcd47836-8555-4dcf-a016-4668182e4d64', request: defaults, status: 'running',
  created_at: '2026-09-08T08:00:00Z', execution_name: 'research-test', events: [], result: null, error: null,
};
const settle = async () => { await new Promise(setImmediate); await new Promise(setImmediate); };

async function setupHome(context, { runs = [], configuration = config, fetchOverride, waitForReady = true, selectFirstRun = false, browserLanguage = 'zh-CN', viewportWidth = 1024 } = {}) {
  const html = await readFile(new URL('index.html', staticUrl), 'utf8');
  const dom = new JSDOM(html, { url: 'http://127.0.0.1:8765/', pretendToBeVisual: true });
  Object.defineProperty(dom.window.navigator, 'language', { value: browserLanguage, configurable: true });
  Object.defineProperty(dom.window, 'innerWidth', { value: viewportWidth, configurable: true });
  const previousDocument = globalThis.document;
  globalThis.document = dom.window.document;
  context.after(() => { globalThis.document = previousDocument; });
  const calls = [];
  const timers = new Map();
  let timerId = 0;
  let currentTime = 0;
  let selected = runs[0] || run;
  const streamRequests = [];
  const clock = {
    setTimeout(task, delay) { timers.set(++timerId, { task, delay, at: currentTime + delay }); return timerId; },
    clearTimeout(id) { timers.delete(id); },
  };
  const advance = async duration => {
    const end = currentTime + duration;
    for (;;) {
      const next = [...timers].filter(([, timer]) => timer.at <= end).sort((first, second) => first[1].at - second[1].at)[0];
      if (!next) break;
      currentTime = next[1].at;
      timers.delete(next[0]);
      await next[1].task();
      await settle();
    }
    currentTime = end;
  };
  const fetchImpl = async (url, options) => {
    calls.push({ url, options });
    if (url.endsWith('/events')) streamRequests.push({ url, options });
    if (fetchOverride) {
      const overridden = await fetchOverride(url, options);
      if (overridden) return overridden;
    }
    if (url.endsWith('/events')) {
      return jsonResponse({ detail: 'stream not mocked' }, 503);
    }
    let payload;
    if (url === '/api/config') payload = configuration;
    else if (url === '/api/runs' && options.method === 'GET') payload = { runs };
    else if (url === '/api/runs') {
      selected = { ...run, request: JSON.parse(options.body), status: 'starting' };
      payload = selected;
    } else if (url.endsWith('/stop')) {
      selected = { ...selected, status: 'stopped' };
      payload = selected;
    } else payload = runs.find(item => url.endsWith(item.id)) || selected;
    return { ok: true, status: options.method === 'POST' ? 202 : 200, json: async () => structuredClone(payload) };
  };
  const workbench = application.mountWorkbench(dom.window.document, { fetchImpl, clock });
  context.after(() => { workbench.dispose(); dom.window.close(); });
  if (waitForReady) await workbench.ready;
  if (selectFirstRun && runs.length) {
    await settle();
    dom.window.document.querySelector('#history-list .history-item').click();
    await settle();
  }
  return { document: dom.window.document, window: dom.window, calls, timers, workbench, advance, streamRequests };
}

const setup = (context, options = {}) => setupHome(context, { ...options, selectFirstRun: true });

test('Market loads for a registered user even when model configuration fails', async context => {
  const { document, calls } = await setupHome(context, { fetchOverride: url => {
    if (url === '/api/me') return jsonResponse({ user: { object_id: 'data-only-guest', email: 'guest@example.test', is_admin: false }, csrf_token: 'market-token' });
    if (url.startsWith('/api/settings') || url === '/api/budget') return jsonResponse({}, 503);
    if (url === '/api/market/watchlist') return jsonResponse({ symbols: [], version: 'v1' });
  } });
  assert.equal(document.querySelector('#market-entry').hidden, false);
  assert.equal(document.querySelector('#start-run').disabled, true);
  document.querySelector('#market-entry').click(); await settle();
  assert.equal(document.querySelector('#market-screen').hidden, false);
  assert.equal(document.querySelector('#workspace-home').hidden, true);
  assert.equal(document.querySelector('#breadcrumb-studio').textContent, '行情');
  assert.ok(calls.some(call => call.url === '/api/market/watchlist'));
  document.querySelector('#research-tasks-entry').click();
  assert.equal(document.querySelector('#market-screen').hidden, true);
  assert.equal(document.querySelector('#workspace-tasks').hidden, false);
});

test('Market prefills the symbol only and preserves date, asset and all other research draft fields', async context => {
  const responses = {
    '/api/me': { user: { object_id: 'guest-market', is_admin: false }, csrf_token: 'market-token' },
    '/api/settings/catalog': emailCatalog, '/api/settings': emailSettings, '/api/budget': emailBudget,
    '/api/market/watchlist': { symbols: ['MSFT'], version: 'v1' },
    '/api/market/snapshot': { selected: 'MSFT', source: 'yfinance', interval: '1d', fetched_at: '2026-10-10T00:00:00Z', quotes: [{ symbol: 'MSFT', name: 'Microsoft', price: 100 }], candles: [], errors: [] },
  };
  const { document, calls } = await setupHome(context, { fetchOverride: url => Object.hasOwn(responses, url) ? jsonResponse(responses[url]) : undefined });
  document.querySelector('#ticker').value = 'USER';
  document.querySelector('#analysis_date').value = '2026-09-01';
  document.querySelector('#asset_type').value = 'etf';
  document.querySelector('#risk_rounds').value = '3';
  document.querySelector('#language').value = 'English';
  document.querySelector('#market-entry').click(); await settle();
  const count = calls.filter(call => call.options.method !== 'GET').length;
  document.querySelector('#market-research').click();
  assert.equal(document.querySelector('#ticker').value, 'MSFT');
  assert.equal(document.querySelector('#analysis_date').value, '2026-09-01');
  assert.equal(document.querySelector('#asset_type').value, 'etf');
  assert.equal(document.querySelector('#risk_rounds').value, '3');
  assert.equal(document.querySelector('#language').value, 'English');
  assert.equal(document.querySelector('#research-drawer').open, true);
  assert.equal(calls.filter(call => call.options.method !== 'GET').length, count);
  assert.equal(document.querySelector('#market-screen').hidden, true);
});

test('browser locale and appearance switching translate UI without changing research fields', async context => {
  const { document, window } = await setupHome(context, { browserLanguage: 'en-US' });
  assert.equal(document.documentElement.lang, 'en');
  assert.equal(document.querySelector('#new-research').textContent.trim(), 'New research');
  document.querySelector('#appearance-button').click();
  assert.match(document.querySelector('#theme-toggle').getAttribute('aria-label'), /Switch to/);
  document.querySelector('#appearance-dialog [data-studio-close]').click();
  document.querySelector('#ticker').value = 'USER-TICKER';
  document.querySelector('#language').value = 'Chinese';
  document.querySelector('[name="analysts"][value="market"]').checked = false;
  document.querySelector('#vendor-news_data').value = 'alpha_vantage';
  document.querySelector('#appearance-button').click();
  const selector = document.querySelector('#appearance-language');
  assert.equal(selector.value, 'auto');
  selector.value = 'zh';
  selector.dispatchEvent(new window.Event('change', { bubbles: true }));
  assert.equal(document.documentElement.lang, 'zh-CN');
  assert.equal(document.querySelector('#new-research').textContent.trim(), '新建研究');
  assert.match(document.querySelector('#theme-toggle').getAttribute('aria-label'), /切换.*主题/);
  assert.equal(document.querySelector('#ticker').value, 'USER-TICKER');
  assert.equal(document.querySelector('#language').value, 'Chinese');
  assert.equal(document.querySelector('[name="analysts"][value="market"]').checked, false);
  assert.equal(document.querySelector('#vendor-news_data').value, 'alpha_vantage');
});

test('the browser entry exposes the real workbench lifecycle', () => {
  assert.equal(typeof application?.mountWorkbench, 'function', 'app.js has not been implemented');
});

test('workspace collapse and mobile report navigation override desktop grid dimensions', async () => {
  const stylesheet = await readFile(new URL('styles.css', staticUrl), 'utf8');
  assert.match(stylesheet, /\.workbench\[data-screen="workspace"\]\[data-sidebar-open="false"\]\s*\{[^}]*grid-template-columns:\s*minmax\(0,\s*1fr\)/);
  assert.match(stylesheet, /@media \(max-width: 850px\)[\s\S]*?\.workspace \.report-layout\s*\{[^}]*display:\s*flex/);
  assert.match(stylesheet, /@media \(max-width: 850px\)[\s\S]*?\.workspace \.report-nav\s*\{[^}]*max-height:\s*128px/);
  assert.match(stylesheet, /@media \(max-width: 850px\)[\s\S]*?\.studio-content\s*\{[^}]*grid-template-columns:\s*minmax\(0,\s*1fr\)/);
});

test('home is visible before delayed config and history and remains interactive', async context => {
  let releaseConfig;
  let releaseHistory;
  const { document, calls, workbench } = await setupHome(context, {
    runs: [run], waitForReady: false,
    fetchOverride: url => {
      if (url === '/api/config') return new Promise(resolve => { releaseConfig = resolve; });
      if (url === '/api/runs') return new Promise(resolve => { releaseHistory = resolve; });
    },
  });
  const initialHomeVisible = !document.querySelector('#workspace-home').hidden && document.documentElement.dataset.screen === 'home';
  document.querySelector('#new-research').click();
  const drawerOpened = document.querySelector('#research-drawer').open;
  document.querySelector('#close-research-drawer').click();
  await settle();
  const historyStartedBeforeConfig = typeof releaseHistory === 'function';
  releaseConfig(jsonResponse(config));
  await settle();
  releaseHistory(jsonResponse({ runs: [run] }));
  await workbench.ready;
  await settle();
  assert.equal(initialHomeVisible, true);
  assert.equal(drawerOpened, true);
  assert.equal(historyStartedBeforeConfig, true);
  assert.equal(document.querySelector('#workspace-home').hidden, false);
  assert.equal(document.querySelectorAll('#history-list .history-item').length, 1);
  assert.equal(calls.some(call => call.url === `/api/runs/${run.id}` || call.url.endsWith('/events')), false);
});

test('home readiness and config controls do not wait for delayed history', async context => {
  let releaseHistory;
  const { document, workbench } = await setupHome(context, {
    runs: [run], waitForReady: false,
    fetchOverride: url => url === '/api/runs' ? new Promise(resolve => { releaseHistory = resolve; }) : undefined,
  });
  let readyBeforeHistory = false;
  void workbench.ready.then(() => { readyBeforeHistory = true; });
  await settle();
  const readyWithoutHistory = readyBeforeHistory;
  document.querySelector('#new-research').click();
  const configuredDraft = document.querySelector('#ticker').value;
  releaseHistory(jsonResponse({ runs: [run] }));
  await workbench.ready;
  await settle();
  assert.equal(readyWithoutHistory, true);
  assert.equal(configuredDraft, defaults.ticker);
});

test('failed history leaves the home visible and the new research drawer usable', async context => {
  const { document, window, calls } = await setupHome(context, {
    fetchOverride: url => url === '/api/runs' ? jsonResponse({ detail: 'unavailable' }, 503) : undefined,
  });
  await settle();
  assert.equal(document.documentElement.dataset.home, 'true');
  assert.equal(document.querySelector('#history-error').hidden, false);
  document.querySelector('#new-research').click();
  assert.equal(document.querySelector('#research-drawer').open, true);
  document.querySelector('#close-research-drawer').click();
  assert.equal(document.querySelector('#workspace-home').hidden, false);
  assert.equal(calls.some(call => call.url.endsWith('/events')), false);
  assert.ok(window);
});

test('initial history 401 after config readiness retries without a stale reauthorization warning', async context => {
  let releaseFirstHistory;
  let historyRequests = 0;
  const { document, workbench } = await setupHome(context, {
    runs: [run], waitForReady: false,
    fetchOverride: url => {
      if (url !== '/api/runs') return;
      historyRequests += 1;
      return historyRequests === 1 ? new Promise(resolve => { releaseFirstHistory = resolve; }) : jsonResponse({ runs: [run] });
    },
  });
  await workbench.ready;
  releaseFirstHistory(jsonResponse({ detail: 'login needed' }, 401));
  await settle();
  assert.equal(historyRequests, 2);
  assert.equal(document.querySelector('#reauthorize').hidden, true);
  assert.equal(document.querySelector('#history-error').hidden, true);
  assert.equal(document.documentElement.dataset.home, 'true');
});

test('late history response does not replace an explicitly opened report', async context => {
  let releaseHistory;
  let deferHistory = false;
  const { document, workbench, calls } = await setupHome(context, {
    runs: [run], waitForReady: false,
    fetchOverride: (url, options) => url === '/api/runs' && options.method === 'GET' && deferHistory ? new Promise(resolve => { releaseHistory = resolve; }) : undefined,
  });
  await workbench.ready;
  await settle();
  deferHistory = true;
  document.querySelector('#refresh').click();
  await settle();
  document.querySelector('#history-list .history-item').click();
  await settle();
  assert.equal(document.documentElement.dataset.home, 'false');
  const selectedTitle = document.querySelector('#run-title').textContent;
  releaseHistory(jsonResponse({ runs: [{ ...run, id: 'archived-run', request: { ...defaults, ticker: 'VOO' } }] }));
  await settle();
  assert.equal(document.documentElement.dataset.home, 'false');
  assert.equal(document.querySelector('#run-title').textContent, selectedTitle);
  assert.equal(calls.some(call => call.url === '/api/runs/archived-run'), false);
});

test('history polling updates the list without leaving the unselected home', async context => {
  const { document, advance, calls } = await setupHome(context, { runs: [run] });
  await advance(30000);
  assert.equal(document.documentElement.dataset.home, 'true');
  assert.equal(document.querySelector('#workspace-home').hidden, false);
  assert.equal(document.querySelectorAll('#history-list .history-item').length, 1);
  assert.equal(calls.filter(call => call.url === '/api/runs').length, 2);
  assert.equal(calls.some(call => call.url === `/api/runs/${run.id}` || call.url.endsWith('/events')), false);
});

test('research inspector retains usage totals and token composition without the call distribution chart', async context => {
  const detailed = { ...run, settings: { mode: 'managed_identity', quick_model: 'historic-quick', deep_model: 'historic-deep' }, events: [
    { kind: 'phase', timestamp: '2026-09-28T10:00:00Z', payload: { stage: 'market' } },
    { kind: 'usage', timestamp: '2026-09-28T10:00:01Z', payload: { llm_calls: 3, tool_calls: 2, input_tokens: 1200, output_tokens: 300 } },
  ] };
  const { document, calls } = await setup(context, { runs: [detailed] });
  assert.match(document.querySelector('.flow-section')?.textContent || '', /RESEARCH FLOW \/ 研究进程/);
  assert.equal(document.querySelectorAll('#phase-list li').length, 5);
  assert.ok(document.querySelector('.metric-grid #stat-llm_calls'));
  assert.equal(document.querySelector('#stat-llm_calls').textContent, '3');
  assert.equal(document.querySelector('.activity-chart'), null);
  assert.ok(document.querySelector('#studio-sources'));
  assert.match(document.querySelector('#studio-selected-model').textContent, /historic-deep/);
  assert.equal(document.querySelector('#stat-input_tokens').textContent, '1,200');
  assert.equal(document.querySelector('#token-chart').hidden, false);
  assert.equal(calls.some(call => call.options.method !== 'GET'), false);
});

test('narrow-screen sidebar opens as a modal drawer and restores workspace access when closed', async context => {
  const { document } = await setupHome(context, { viewportWidth: 390 });
  const workbench = document.querySelector('#workbench');
  const toggle = document.querySelector('#sidebar-toggle');
  const backdrop = document.querySelector('#sidebar-backdrop');
  const workspace = document.querySelector('#workspace');

  assert.equal(workbench.dataset.sidebarOpen, 'false');
  assert.equal(toggle.getAttribute('aria-expanded'), 'false');
  assert.equal(toggle.querySelector('[data-lucide]')?.getAttribute('data-lucide'), 'panel-left-open');
  assert.equal(backdrop.hidden, true);
  assert.equal(workspace.inert, false);

  toggle.click();
  assert.equal(workbench.dataset.sidebarOpen, 'true');
  assert.equal(toggle.getAttribute('aria-expanded'), 'true');
  assert.equal(toggle.querySelector('[data-lucide]')?.getAttribute('data-lucide'), 'panel-left-close');
  assert.equal(backdrop.hidden, false);
  assert.equal(workspace.inert, true);

  backdrop.click();
  assert.equal(workbench.dataset.sidebarOpen, 'false');
  assert.equal(toggle.getAttribute('aria-expanded'), 'false');
  assert.equal(toggle.querySelector('[data-lucide]')?.getAttribute('data-lucide'), 'panel-left-open');
  assert.equal(backdrop.hidden, true);
  assert.equal(workspace.inert, false);
});

test('research flow reflects observed worker phases and terminal failures without invented completion', async context => {
  const failed = { ...run, status: 'failed', events: [
    { kind: 'phase', payload: { stage: 'initializing' } },
    { kind: 'phase', payload: { stage: 'market' } },
    { kind: 'error', payload: { message: 'provider failed' } },
  ] };
  const { document } = await setup(context, { runs: [failed] });
  const stages = [...document.querySelectorAll('#phase-list li')];
  assert.equal(stages.length, 5);
  assert.equal(stages.filter(stage => stage.classList.contains('done')).length, 1);
  assert.equal(stages.filter(stage => stage.classList.contains('current')).length, 0);
  assert.equal(stages.filter(stage => stage.classList.contains('failed')).length, 1);
  assert.equal(document.querySelector('#stage-description').textContent, '失败');
});

test('worker trader phase precedes risk and final-decision evidence marks report generation without validation', async context => {
  const collecting = { ...run, status: 'collecting', events: [
    ...['initializing', 'market', 'bull_researcher', 'trader', 'aggressive_risk'].map(stage => ({ kind: 'phase', payload: { stage } })),
    { kind: 'report', payload: { section: 'final_trade_decision', content: 'HOLD' } },
  ], result: { reports: {}, stats: { llm_calls: 7 } } };
  const { document } = await setup(context, { runs: [collecting] });
  const stages = [...document.querySelectorAll('#phase-list li')];
  assert.equal(stages[2].classList.contains('done'), true);
  assert.equal(stages[3].classList.contains('done'), true);
  assert.equal(stages[4].classList.contains('current'), true);
  assert.equal(stages.filter(stage => stage.classList.contains('done')).length, 4);
  assert.equal(stages.filter(stage => stage.classList.contains('current')).length, 1);
});

test('connectivity success does not render or complete a research flow and initializes root workspace screen', async context => {
  const connected = { ...run, request: { ...defaults, mode: 'connectivity' }, status: 'succeeded',
    result: { connectivity: [{ provider: 'foundry', status: 'ok' }], reports: {}, stats: {} }, events: [] };
  const { document } = await setup(context, { runs: [connected] });
  assert.equal(document.documentElement.dataset.screen, 'workspace');
  assert.equal(document.querySelector('.flow-section').hidden, true);
  assert.equal(document.querySelectorAll('#phase-list li').length, 0);
  assert.equal(document.querySelectorAll('#phase-list li.done, #phase-list li.current').length, 0);
});

test('preparation and reconciliation states never animate an observed last phase', async context => {
  for (const status of ['prepared', 'reconciliation_required']) {
    const pending = { ...run, status, events: [{ kind: 'phase', payload: { stage: 'market' } }] };
    const { document } = await setup(context, { runs: [pending] });
    assert.equal(document.querySelectorAll('#phase-list li.current').length, 0);
    assert.equal(document.querySelectorAll('#phase-list li.done').length, 0);
  }
});

test('usage totals and coverage use observed events and distinguish configuration from verification', async context => {
  const observed = { ...run, events: [
    { kind: 'phase', payload: { stage: 'initializing' } },
    { kind: 'usage', payload: { llm_calls: 1, tool_calls: 2, input_tokens: 40, output_tokens: 10 } },
    { kind: 'usage', payload: { llm_calls: 3, tool_calls: 5, input_tokens: 120, output_tokens: 30 } },
  ] };
  const { document } = await setup(context, { runs: [observed] });
  assert.equal(document.querySelector('#stat-llm_calls').textContent, '3');
  assert.equal(document.querySelector('#stat-tool_calls').textContent, '5');
  assert.match(document.querySelector('#studio-sources').textContent, /已配置/);
  assert.match(document.querySelector('#studio-sources').textContent, /尚未验证/);
  assert.doesNotMatch(document.querySelector('#studio-sources').textContent, /已连接/);
  assert.equal(document.querySelectorAll('#stat-llm_calls').length, 1);
});

test('flow stays empty and usage counts stay unknown when a failed run has no evidence', async context => {
  const failed = { ...run, status: 'failed', events: [{ kind: 'error', payload: { message: 'failed early' } }] };
  const { document } = await setup(context, { runs: [failed] });
  assert.equal(document.querySelectorAll('#phase-list li.done').length, 0);
  assert.equal(document.querySelectorAll('#phase-list li.current').length, 0);
  assert.equal(document.querySelectorAll('#phase-list li.failed').length, 0);
  assert.equal(document.querySelector('#stat-llm_calls').textContent, '--');
  assert.match(document.querySelector('#stage-description').textContent, /失败/);
});

test('successful terminal state completes the five stages even when the event stream is sparse', async context => {
  const completed = { ...run, status: 'succeeded', events: [], result: { reports: {}, stats: {} } };
  const { document } = await setup(context, { runs: [completed] });
  assert.equal(document.querySelectorAll('#phase-list li').length, 5);
  assert.equal(document.querySelectorAll('#phase-list li.done').length, 5);
  assert.equal(document.querySelectorAll('#phase-list li.current, #phase-list li.failed').length, 0);
  assert.equal(document.querySelector('#stage-description').textContent, '研究已完成');
  assert.equal(document.querySelector('#stat-llm_calls').textContent, '--');
});

test('task list retains the selected run through refresh and selection returns to its report', async context => {
  const second = { ...run, id: 'da02a113-3ee4-422a-902b-fc958a74620f', request: { ...defaults, ticker: 'VOO' }, status: 'succeeded' };
  const { document, window } = await setup(context, { runs: [run, second] });
  document.querySelector('#workspace-home-button').click();
  assert.equal(document.documentElement.dataset.home, 'true');
  assert.equal(document.querySelector('#workspace-home').hidden, false);
  assert.equal(document.querySelector('#workspace-run').hidden, true);
  assert.equal(document.querySelector('#research-tasks-entry').hasAttribute('aria-current'), false);
  assert.match(document.querySelector('#run-title').textContent, /MSFT/);
  document.querySelector('#refresh').click();
  await settle();
  assert.equal(document.documentElement.dataset.home, 'true');
  document.querySelector('#research-tasks-entry').click();
  assert.equal(document.querySelector('#workspace-tasks').hidden, false);
  document.querySelector('#history-list .history-item').click();
  await settle();
  assert.equal(document.documentElement.dataset.home, 'false');
  assert.equal(document.querySelector('#workspace-home').hidden, true);
  assert.equal(document.querySelector('#workspace-run').hidden, false);
  assert.match(document.querySelector('#run-title').textContent, /MSFT/);
  assert.ok(window);
});

test('task filters preserve query and report return restores the task list without submitting', async context => {
  const completed = { ...run, id: 'completed-run', status: 'succeeded' };
  const failed = { ...run, id: 'failed-run', status: 'failed' };
  const { document, window, calls } = await setupHome(context, { runs: [run, completed, failed] });
  await settle();
  document.querySelector('#research-tasks-entry').click();
  assert.equal(document.querySelectorAll('#history-list .history-item').length, 3);
  document.querySelector('[data-task-filter="completed"]').click();
  assert.equal(document.querySelectorAll('#history-list .history-item').length, 1);
  assert.match(document.querySelector('#history-list').textContent, /已完成/);
  document.querySelector('#history-list .history-item').click();
  await settle();
  assert.equal(document.querySelector('#workspace-run').hidden, false);
  document.querySelector('#return-tasks').click();
  assert.equal(document.querySelector('#workspace-tasks').hidden, false);
  assert.equal(document.querySelector('[data-task-filter="completed"]').getAttribute('aria-pressed'), 'true');
  document.querySelector('[data-task-filter="interrupted"]').click();
  assert.match(document.querySelector('#history-list').textContent, /失败/);
  document.querySelector('[data-task-filter="active"]').click();
  assert.match(document.querySelector('#history-list').textContent, /运行中/);
  const search = document.querySelector('#history-search');
  search.value = 'NO-MATCH';
  search.dispatchEvent(new window.Event('input', { bubbles: true }));
  document.querySelector('[data-task-filter="all"]').click();
  assert.equal(document.querySelectorAll('#history-list .history-item').length, 0);
  assert.equal(search.value, 'NO-MATCH');
  assert.equal(calls.some(call => call.options.method !== 'GET'), false);
});

test('workspace navigation switches between tasks and assistant and closes the mobile drawer', async context => {
  const catalog = structuredClone(emailCatalog);
  catalog.assistant_available = true;
  catalog.modes.managed_identity.available = false;
  catalog.modes.managed_identity.configurations = [];
  const { document, calls } = await setupHome(context, {
    runs: [run], viewportWidth: 390,
    fetchOverride: url => {
      if (url === '/api/me') return jsonResponse({ user: { object_id: 'guest-1', email: 'guest@example.com', name: 'Guest', is_admin: false }, csrf_token: 'fixture-csrf' });
      if (url === '/api/settings/catalog') return jsonResponse(catalog);
      if (url === '/api/settings') return jsonResponse(emailSettings);
      if (url === '/api/budget') return jsonResponse(emailBudget);
      if (url === '/api/assistant/sessions') return jsonResponse({ sessions: [] });
    },
  });
  await settle();
  assert.equal(document.querySelector('#research-assistant-entry').hidden, false);
  document.querySelector('#sidebar-toggle').click();
  document.querySelector('#research-assistant-entry').click();
  await settle();
  assert.equal(document.querySelector('#research-assistant-screen').hidden, false);
  assert.equal(document.querySelector('#workspace-home').hidden, true);
  assert.equal(document.querySelector('#research-tasks-entry').hasAttribute('aria-current'), false);
  assert.equal(document.querySelector('#breadcrumb-studio').textContent, '研究助手');
  assert.equal(document.querySelector('#workbench').dataset.sidebarOpen, 'false');
  assert.equal(document.querySelector('#workspace').inert, false);
  document.querySelector('#sidebar-toggle').click();
  document.querySelector('#research-tasks-entry').click();
  assert.equal(document.querySelector('#research-assistant-screen').hidden, true);
  assert.equal(document.querySelector('#workspace-tasks').hidden, false);
  assert.equal(document.querySelector('#research-tasks-entry').getAttribute('aria-current'), 'page');
  assert.equal(document.querySelector('#research-assistant-entry').hasAttribute('aria-current'), false);
  assert.equal(document.querySelector('#breadcrumb-studio').textContent, '研究任务');
  assert.equal(document.querySelector('#workbench').dataset.sidebarOpen, 'false');
  assert.equal(calls.some(call => call.options.method !== 'GET'), false);
});

test('parameter drawer reuses the live form without leaving the task list', async context => {
  const { document, window, calls } = await setup(context);
  assert.equal(document.querySelector('#workspace-home').hidden, false);
  document.querySelector('#research-tasks-entry').click();
  document.querySelector('#new-research').click();
  const drawer = document.querySelector('#research-drawer');
  assert.equal(drawer.open, true);
  assert.ok(drawer.querySelector('#config-form'));
  assert.ok(drawer.querySelector('#ticker'));
  assert.ok(drawer.querySelector('#start-run'));
  document.querySelector('#close-research-drawer').click();
  assert.equal(drawer.open, false);
  assert.equal(document.activeElement.id, 'new-research');
  assert.equal(document.querySelector('#side-history').hidden, false);
  assert.equal(document.querySelector('#workspace-tasks').hidden, false);
  document.querySelector('#new-research').click();
  assert.equal(drawer.open, true);
  drawer.dispatchEvent(new window.Event('cancel', { cancelable: true }));
  assert.equal(drawer.open, false);
  assert.equal(document.querySelector('#research-tasks-entry').getAttribute('aria-current'), 'page');
  assert.equal(calls.some(call => call.options.method !== 'GET'), false);
  assert.ok(window);
});

test('bottom data-source changes still trigger saved-settings alignment without opening model settings', async context => {
  const { document, window } = await setupHome(context, {
    fetchOverride: url => {
      if (url === '/api/me') return jsonResponse({ user: { object_id: 'owner-1', is_admin: true }, csrf_token: 'fixture-csrf' });
      if (url === '/api/settings/catalog') return jsonResponse(emailCatalog);
      if (url === '/api/settings') return jsonResponse(emailSettings);
      if (url === '/api/budget') return jsonResponse(emailBudget);
    },
  });
  const vendor = document.querySelector('#vendor-news_data');
  vendor.value = 'alpha_vantage';
  vendor.dispatchEvent(new window.Event('change', { bubbles: true }));
  assert.match(document.querySelector('#start-run').title, /不一致/);
  assert.equal(document.querySelector('#start-run').disabled, true);
  assert.equal(document.querySelector('.model-settings').open, false);
});

test('welcome is the initial screen and brand returns to it while advanced settings stay collapsed', async context => {
  const { document, calls } = await setupHome(context, {
    fetchOverride: url => {
      if (url === '/api/me') return jsonResponse({ user: { object_id: 'owner-1', is_admin: true }, csrf_token: 'fixture-csrf' });
      if (url === '/api/settings/catalog') return jsonResponse(emailCatalog);
      if (url === '/api/settings') return jsonResponse(emailSettings);
      if (url === '/api/budget') return jsonResponse(emailBudget);
    },
  });
  assert.equal(document.querySelector('#workspace-home').hidden, false);
  assert.ok(document.querySelector('#platform-scene'));
  assert.equal(document.querySelector('#workspace-tasks').hidden, true);
  document.querySelector('#research-tasks-entry').click();
  document.querySelector('#new-research').click();
  const advanced = document.querySelector('#research-advanced');
  assert.deepEqual([...advanced.children].map(node => node.className), ['model-settings', 'vendor-details']);
  assert.equal(document.querySelector('.model-settings').open, false);
  assert.equal(document.querySelector('.vendor-details').open, false);
  assert.equal(document.querySelector('#model-settings-form').closest('#config-form'), null);
  assert.equal(document.querySelector('.mode-group legend'), null);
  document.querySelector('.model-settings').open = true;
  document.querySelector('.vendor-details').open = true;
  document.querySelector('#close-research-drawer').click();
  document.querySelector('#new-research').click();
  assert.equal(document.querySelector('.model-settings').open, false);
  assert.equal(document.querySelector('.vendor-details').open, false);
  document.querySelector('#close-research-drawer').click();
  document.querySelector('#workspace-home-button').click();
  assert.equal(document.querySelector('#workspace-home').hidden, false);
  assert.equal(document.querySelector('#workspace-tasks').hidden, true);
  assert.equal(document.querySelector('#open-selected-report'), null);
  assert.equal(calls.some(call => call.options.method !== 'GET'), false);
});

test('agent process tab participates in existing workspace tab keyboard navigation', async context => {
  const { document, window, calls } = await setup(context, { runs: [run] });
  const reportsTab = document.querySelector('#reports-tab');
  const processTab = document.querySelector('#agent-process-tab');
  const eventsTab = document.querySelector('#events-tab');
  const processPanel = document.querySelector('#agent-process-panel');

  assert.ok(processTab);
  assert.ok(processPanel);
  assert.equal(processTab.getAttribute('role'), 'tab');
  assert.equal(processTab.getAttribute('aria-controls'), 'agent-process-panel');
  assert.equal(processPanel.getAttribute('role'), 'tabpanel');
  assert.equal(processPanel.hidden, true);

  reportsTab.focus();
  reportsTab.dispatchEvent(new window.KeyboardEvent('keydown', { key: 'ArrowRight', bubbles: true }));
  assert.equal(document.activeElement?.id, 'agent-process-tab');
  assert.equal(processTab.getAttribute('aria-selected'), 'true');
  assert.equal(processPanel.hidden, false);

  processTab.dispatchEvent(new window.KeyboardEvent('keydown', { key: 'ArrowRight', bubbles: true }));
  assert.equal(document.activeElement?.id, 'connectivity-tab');
  assert.equal(document.querySelector('#connectivity-tab').getAttribute('aria-selected'), 'true');

  document.querySelector('#connectivity-tab').dispatchEvent(new window.KeyboardEvent('keydown', { key: 'ArrowRight', bubbles: true }));
  assert.equal(document.activeElement?.id, 'events-tab');
  assert.equal(eventsTab.getAttribute('aria-selected'), 'true');

  assert.equal(calls.some(call => call.options.method !== 'GET'), false);
});

test('production palettes preserve task data and share only the saved appearance preference', async context => {
  const { document, window, calls } = await setup(context, { runs: [run] });
  document.querySelector('#appearance-button').click();
  assert.equal(document.querySelectorAll('[name="appearance-palette"]').length, 5);
  document.querySelector('[name="appearance-palette"][value="pine"]').click();
  assert.equal(document.documentElement.dataset.palette, 'pine');
  assert.equal(window.localStorage.getItem('tradingagents.studio.palette'), 'pine');
  assert.equal(window.localStorage.length, 1);
  document.querySelector('[name="appearance-theme"][value="dark"]').click();
  assert.equal(document.documentElement.dataset.palette, 'pine');
  assert.match(document.querySelector('#run-title').textContent, /MSFT/);
  assert.equal(calls.some(call => call.options.method !== 'GET'), false);
});

test('studio searches other available reports and shows immutable email model settings', async context => {
  const historical = { ...run, settings: { mode: 'api_key', provider: 'openai', quick_model: 'historic-quick', deep_model: 'historic-deep', revision: 'historical-r1' } };
  const other = { ...run, id: 'other-run', request: { ...defaults, ticker: 'VOO' }, status: 'succeeded', result: { reports: { market_report: 'Unique archived fund composition' } } };
  const { document, window, calls } = await setup(context, { runs: [historical, other] });
  assert.match(document.querySelector('#request-values').textContent, /historic-quick/);
  assert.match(document.querySelector('#studio-selected-model').textContent, /historic-deep/);
  assert.doesNotMatch(document.querySelector('#studio-selected-model').textContent, /gpt-5.6-sol/);
  document.querySelector('#command-button').click();
  const query = document.querySelector('#command-query'); query.value = 'archived fund'; query.dispatchEvent(new window.Event('input', { bubbles: true }));
  assert.equal(document.querySelectorAll('.command-result').length, 1);
  assert.match(document.querySelector('.command-result').textContent, /VOO/);
  query.dispatchEvent(new window.KeyboardEvent('keydown', { key: 'Enter', bubbles: true })); await settle();
  assert.match(document.querySelector('#run-title').textContent, /VOO/);
  assert.match(document.querySelector('#report-articles').textContent, /Unique archived/);
  assert.equal(calls.some(call => call.options.method !== 'GET'), false);
});

test('agent process panel renders actual ordered nodes and replay controls without fabricating completion', async context => {
  const running = {
    ...run,
    request: { ...defaults, analysts: ['market', 'social'] },
    events: [
      { kind: 'phase', timestamp: '2026-09-29T10:00:00Z', payload: { stage: 'market' } },
      { kind: 'phase', timestamp: '2026-09-29T10:00:01Z', payload: { stage: 'tool:get_verified_market_snapshot' } },
      { kind: 'report', timestamp: '2026-09-29T10:00:03Z', payload: { section: 'market_report', content: 'market report body' } },
      { kind: 'phase', timestamp: '2026-09-29T10:01:00Z', payload: { stage: 'social' } },
    ],
  };
  const { document, window, calls } = await setup(context, { runs: [running] });
  document.querySelector('#agent-process-tab').click();
  const nodes = [...document.querySelectorAll('#agent-process-graph .agent-process-node')];
  assert.deepEqual(nodes.slice(0, 4).map(node => node.dataset.nodeId), ['market', 'social', 'bull_researcher', 'bear_researcher']);
  assert.equal(nodes.find(node => node.dataset.nodeId === 'market').dataset.status, 'done');
  assert.equal(nodes.find(node => node.dataset.nodeId === 'social').dataset.status, 'active');
  assert.equal(nodes.find(node => node.dataset.nodeId === 'bull_researcher').dataset.status, 'pending');
  assert.equal(document.querySelectorAll('.agent-process-lane').length, 4);
  assert.ok(document.querySelector('.agent-process-connector'));
  assert.match(document.querySelector('#agent-process-graph').textContent, /循环/);
  nodes.find(node => node.dataset.nodeId === 'market').click();
  nodes.find(node => node.dataset.nodeId === 'market').focus();
  document.querySelector('#agent-process-speed').value = '2';
  document.querySelector('#agent-process-speed').dispatchEvent(new window.Event('change'));
  assert.equal(document.activeElement, nodes.find(node => node.dataset.nodeId === 'market'));
  assert.match(document.querySelector('#agent-process-detail-body').textContent, /market report body/);
  assert.ok(document.querySelector('#agent-process-play'));
  assert.ok(document.querySelector('#agent-process-range'));
  assert.equal(calls.some(call => call.options.method !== 'GET'), false);
});

test('agent process playback advances, seeking zero has no timestamp, and changing runs resets replay', async context => {
  const events = ['market', 'social', 'bull_researcher'].map((stage, index) => ({ kind: 'phase', timestamp: `2026-09-29T10:00:0${index}Z`, payload: { stage } }));
  const first = { ...run, events };
  const second = { ...run, id: 'other-run', request: { ...defaults, ticker: 'VOO' }, events: [] };
  const { document, window, advance } = await setup(context, { runs: [first, second] });
  document.querySelector('#agent-process-tab').click();
  document.querySelector('#agent-process-play').click();
  assert.equal(document.querySelector('#agent-process-play').getAttribute('aria-label'), '过程回放暂停');
  await advance(1000);
  assert.equal(document.querySelector('#agent-process-range').value, '2');
  assert.equal(document.querySelector('#agent-process-play').getAttribute('aria-label'), '过程回放暂停');
  const slider = document.querySelector('#agent-process-range');
  slider.value = '0'; slider.dispatchEvent(new window.Event('input', { bubbles: true }));
  assert.equal(document.querySelector('#agent-process-time').textContent, '--');
  document.querySelector('#history-list button:nth-child(2)')?.click();
  await settle();
  assert.equal(document.querySelector('#agent-process-mode').textContent, '实时');
  assert.equal(document.querySelector('#agent-process-range').value, '0');
});

test('email analysis run starts event streaming, merges new events, and stops on page hide', async context => {
  let streamController;
  const encoder = new TextEncoder();
  const responses = {
    '/api/me': { user: { object_id: 'owner-1', email: 'owner@example.com', name: 'Owner User', is_admin: true }, csrf_token: 'email-csrf-1' },
    '/api/settings/catalog': emailCatalog,
    '/api/settings': emailSettings,
    '/api/budget': emailBudget,
  };
  const analysisRun = {
    ...run,
    request: { ...defaults, mode: 'analysis', analysts: ['market'] },
    events: [{ kind: 'phase', timestamp: '2026-09-29T10:00:00Z', payload: { stage: 'market' } }],
  };
  const { document, window, streamRequests } = await setup(context, {
    runs: [analysisRun],
    fetchOverride: (url, options = {}) => {
      if (Object.hasOwn(responses, url)) return jsonResponse(responses[url]);
      if (url === '/api/runs') return jsonResponse({ runs: [analysisRun] });
      if (url === `/api/runs/${run.id}`) return jsonResponse(analysisRun);
      if (url === `/api/runs/${run.id}/events`) {
        streamController = options.signal;
        const stream = new ReadableStream({
          start(controller) {
            controller.enqueue(encoder.encode('event: progress\n'));
            controller.enqueue(encoder.encode('data: {"run_id":"fcd47836-8555-4dcf-a016-4668182e4d64","source":"direct","events":[{"kind":"phase","timestamp":"2026-09-29T10:01:00Z","payload":{"stage":"bull_researcher"}}]}\n\n'));
          },
        });
        return { ok: true, status: 200, body: stream };
      }
    },
  });
  await settle();
  await settle();
  assert.equal(streamRequests.length, 1);
  document.querySelector('#agent-process-tab').click();
  for (let attempt = 0; attempt < 6 && document.querySelector('#agent-process-source').textContent !== '事件更新 / 实时'; attempt += 1) {
    await settle();
  }
  assert.equal(document.querySelector('#agent-process-source').textContent, '事件更新 / 实时');
  assert.equal(document.querySelector('[data-node-id="bull_researcher"]').dataset.status, 'active');
  Object.defineProperty(document, 'hidden', { configurable: true, value: true });
  document.dispatchEvent(new window.Event('visibilitychange'));
  assert.equal(streamController.aborted, true);
});

test('stream rejects malformed progress, keeps updating during replay, and completes without reconnecting', async context => {
  const responses = {
    '/api/me': { user: { object_id: 'owner-1', email: 'owner@example.com', name: 'Owner User', is_admin: true }, csrf_token: 'email-csrf-1' },
    '/api/settings/catalog': emailCatalog, '/api/settings': emailSettings, '/api/budget': emailBudget,
  };
  let streamController;
  const encoder = new TextEncoder();
  const analysisRun = { ...run, events: [] };
  const { document, streamRequests, advance } = await setup(context, { runs: [analysisRun], fetchOverride: (url) => {
    if (Object.hasOwn(responses, url)) return jsonResponse(responses[url]);
    if (url.endsWith('/events')) {
      const stream = new ReadableStream({ start(controller) { streamController = controller; } });
      return { ok: true, status: 200, body: stream };
    }
  } });
  await settle();
  document.querySelector('#agent-process-tab').click();
  streamController.enqueue(encoder.encode(`event: progress\ndata: ${JSON.stringify({ run_id: run.id, source: 'mixed', events: [{ kind: 'phase', timestamp: '2026-09-29T10:00:00Z', payload: { stage: 'market' } }] })}\n\n`));
  await settle();
  assert.equal(document.querySelector('#agent-process-source').textContent, '混合事件 / 含日志补齐');
  document.querySelector('#agent-process-play').click();
  assert.equal(streamRequests.length, 1);
  const replayTime = document.querySelector('#agent-process-time').textContent;
  streamController.enqueue(encoder.encode(`event: progress\ndata: ${JSON.stringify({ run_id: run.id, source: 'mixed', events: [{ kind: 'phase', timestamp: '2026-09-29T09:59:00Z', payload: { stage: 'initializing' } }] })}\n\n`));
  await settle();
  assert.equal(document.querySelector('#agent-process-time').textContent, replayTime);
  streamController.enqueue(encoder.encode(`event: progress\ndata: ${JSON.stringify({ run_id: run.id, source: 'direct', events: [{ kind: 'report', timestamp: '2026-09-29T10:01:00Z', payload: { section: 'final_trade_decision', content: 'HOLD' } }] })}\n\n`));
  await settle();
  document.querySelector('#agent-process-live').click();
  assert.match(document.querySelector('#event-list').textContent, /final_trade_decision/);
  assert.equal(document.querySelector('[data-node-id="portfolio_manager"]').dataset.status, 'done');
  streamController.enqueue(encoder.encode('event: complete\ndata: {}\n\n'));
  await settle(); await advance(6000);
  assert.equal(streamRequests.length, 1);
});

test('studio retains complete request snapshots and filterable event payloads', async context => {
  const detailed = { ...run, events: [
    { kind: 'phase', timestamp: '2026-09-23T10:00:00Z', payload: { stage: 'market', extra: 'keep-value' } },
    { kind: 'error', timestamp: '2026-09-23T10:01:00Z', payload: { message: '<unsafe>', detail: { preserved: true } } },
  ] };
  const { document, window, calls, advance } = await setup(context, { runs: [detailed] });
  const raw = document.querySelector('#request-json');
  assert.ok(raw, 'Original request JSON remains inspectable');
  assert.deepEqual(JSON.parse(raw.textContent), detailed.request);
  const payload = document.querySelector('#event-list details');
  assert.ok(payload);
  assert.match(payload.textContent, /preserved/);
  payload.open = true;
  const filter = document.querySelector('#event-kind');
  filter.value = 'error'; filter.dispatchEvent(new window.Event('change', { bubbles: true }));
  assert.equal(document.querySelectorAll('#event-list li').length, 1);
  assert.equal(document.querySelector('#event-list details').open, true);
  await advance(5000);
  assert.equal(document.querySelector('#event-list details').open, true);
  assert.equal(document.querySelector('#event-list time').getAttribute('datetime'), '2026-09-23T10:01:00Z');
  assert.match(document.querySelector('#event-list pre').textContent, /<unsafe>/);
  assert.equal(document.querySelector('#event-list unsafe'), null);
  assert.equal(calls.some(call => call.options.method !== 'GET'), false);
});

test('research studio appearance and search preserve real runs without submitting actions', async context => {
  const { document, window, calls } = await setup(context, { runs: [run] });
  const appearance = document.querySelector('#appearance-button');
  assert.ok(appearance, 'Production has the approved appearance controls');
  appearance.click();
  assert.equal(document.querySelector('#appearance-dialog').open, true);
  document.querySelector('[name="appearance-theme"][value="dark"]').click();
  assert.equal(document.documentElement.dataset.theme, 'dark');
  document.querySelector('#appearance-motion').click();
  assert.equal(document.documentElement.dataset.motion, 'reduced');
  document.querySelector('[data-studio-close="appearance-dialog"]').click();
  document.querySelector('#command-button').click();
  const query = document.querySelector('#command-query');
  query.value = 'MSFT';
  query.dispatchEvent(new window.Event('input', { bubbles: true }));
  assert.match(document.querySelector('#command-results').textContent, /MSFT/);
  query.dispatchEvent(new window.KeyboardEvent('keydown', { key: 'Enter', bubbles: true, cancelable: true }));
  await settle();
  assert.equal(document.querySelector('#command-dialog').open, false);
  assert.match(document.querySelector('#run-title').textContent, /MSFT/);
  assert.equal(calls.some(call => call.options.method !== 'GET'), false);
});

const publicProfiles = [
  { id: 'aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa', name: 'Research A', revision: 'rev-a', job_id: '/jobs/a', config: { model: 'research-model', endpoint: 'https://research-a.openai.azure.com/' } },
  { id: 'bbbbbbbb-2222-4222-8222-bbbbbbbbbbbb', name: 'Research B', revision: 'rev-b', job_id: '/jobs/b', config: { model: 'another-deployment', endpoint: 'https://research-b.openai.azure.com/' } },
];
const jsonResponse = (payload, status = 200) => ({ ok: status < 400, status, json: async () => structuredClone(payload) });
function publicResponse(url) {
  if (url === '/api/me') return jsonResponse({ user: { name: 'Alice <script>', tenant_id: 'tenant', object_id: 'alice' }, csrf_token: config.csrf_token });
  if (url === '/api/profiles') return jsonResponse({ profiles: publicProfiles });
  if (url === '/api/initializations') return jsonResponse({ initializations: [] });
  if (url.endsWith('/providers')) return jsonResponse({ configured: { alpha_vantage: false, fred: true }, pending: false });
}

test('public account selects validated deployment and submits an owner-free profile envelope', async context => {
  const { document, window, calls } = await setup(context, {
    configuration: { ...config, public_mode: true, model: 'research-model' }, fetchOverride: publicResponse,
  });
  assert.equal(document.querySelector('#profile-select').options.length, 2);
  assert.match(document.querySelector('#account-name').textContent, /Alice <script>/);
  assert.equal(document.querySelector('#account-name script'), null);
  document.querySelector('#profile-select').value = publicProfiles[1].id;
  document.querySelector('#profile-select').dispatchEvent(new window.Event('change', { bubbles: true }));
  await settle();
  assert.equal(document.querySelector('#model-name').textContent, 'another-deployment');
  assert.equal(document.querySelector('#start-run').disabled, false);
  document.querySelector('#config-form').dispatchEvent(new window.Event('submit', { cancelable: true }));
  await settle();
  const submitted = JSON.parse(calls.find(item => item.url === '/api/runs' && item.options.method === 'POST').options.body);
  assert.deepEqual(Object.keys(submitted).sort(), ['profile_id', 'request', 'submission_id']);
  assert.equal(submitted.profile_id, publicProfiles[1].id);
  assert.match(submitted.submission_id, /^[0-9a-f-]{36}$/);
  assert.equal(submitted.request.ticker, 'MSFT');
});

test('public retry retains submission id after an unconfirmed response', async context => {
  let attempts = 0;
  const { document, window, calls } = await setup(context, {
    configuration: { ...config, public_mode: true, model: 'research-model' },
    fetchOverride: (url, options) => {
      if (url === '/api/runs' && options.method === 'POST') {
        attempts += 1;
        return attempts === 1 ? jsonResponse({ detail: 'Unconfirmed' }, 503)
          : jsonResponse({ ...run, status: 'starting', request: JSON.parse(options.body).request });
      }
      return publicResponse(url);
    },
  });
  for (let attempt = 0; attempt < 2; attempt += 1) {
    document.querySelector('#config-form').dispatchEvent(new window.Event('submit', { cancelable: true }));
    await settle();
  }
  const submissions = calls.filter(item => item.url === '/api/runs' && item.options.method === 'POST').map(item => JSON.parse(item.options.body));
  assert.equal(submissions.length, 2);
  assert.equal(submissions[0].submission_id, submissions[1].submission_id);
});

test('public resource editor saves only selected identifiers and selects the saved profile', async context => {
  let profiles = [...publicProfiles];
  const { document, window, calls } = await setup(context, {
    configuration: { ...config, public_mode: true, model: 'research-model' },
    fetchOverride: (url, options) => {
      if (url === '/api/profiles' && options.method === 'GET') return jsonResponse({ profiles });
      if (url === '/api/profiles' && options.method === 'POST') {
        const saved = { ...JSON.parse(options.body), id: 'cccccccc-3333-4333-8333-cccccccccccc', revision: 'new', config: { model: 'my-model' } };
        profiles = [...profiles, saved]; return jsonResponse(saved, 201);
      }
      if (url.endsWith('/cccccccc-3333-4333-8333-cccccccccccc/providers')) return jsonResponse({ configured: { alpha_vantage: true, fred: false }, pending: false });
      return publicResponse(url);
    },
  });
  document.querySelector('#manage-resources').click();
  await settle();
  document.querySelector('#profile-new').click();
  const values = { name: 'Own Job', job_id: '/selected/job', workspace_resource_id: '/selected/logs', foundry_account_id: '/selected/foundry', deployment_name: 'my-model' };
  for (const [name, value] of Object.entries(values)) document.querySelector(`#profile-${name}`).value = value;
  document.querySelector('#profile-form').dispatchEvent(new window.Event('submit', { cancelable: true }));
  await settle();
  const call = calls.find(item => item.url === '/api/profiles' && item.options.method === 'POST');
  assert.deepEqual(JSON.parse(call.options.body), values);
  assert.equal(document.querySelector('#profile-select').value, profiles.at(-1).id);
  assert.equal(document.querySelector('#model-name').textContent, 'my-model');
  assert.equal(document.querySelector('#key-state-alpha_vantage').textContent, '已配置');
  assert.equal(document.querySelector('#key-state-fred').textContent, '未配置');
});

test('public discovery locks subscription and group selection until the current query finishes', async context => {
  let completeGroups;
  const { document } = await setup(context, {
    configuration: { ...config, public_mode: true },
    fetchOverride: url => {
      if (url === '/api/resources/subscriptions') return jsonResponse({ subscriptions: [{ id: 'subscription-a', name: 'A' }, { id: 'subscription-b', name: 'B' }] });
      if (url.startsWith('/api/resources/groups?')) return new Promise(resolve => { completeGroups = resolve; });
      return publicResponse(url);
    },
  });
  document.querySelector('#manage-resources').click(); await settle();
  document.querySelector('#browse-resources').click(); await settle();
  assert.equal(document.querySelector('#resource-subscription').disabled, true);
  assert.equal(document.querySelector('#resource-group').disabled, true);
  completeGroups(jsonResponse({ groups: [] })); await settle();
  assert.equal(document.querySelector('#resource-subscription').disabled, false);
});

test('public provider inputs are write-only and empty fields do not clear existing keys', async context => {
  const { document, window, calls } = await setup(context, {
    configuration: { ...config, public_mode: true, model: 'research-model' }, fetchOverride: publicResponse,
  });
  document.querySelector('#manage-resources').click();
  await settle();
  const alpha = document.querySelector('#key-alpha_vantage');
  const fred = document.querySelector('#key-fred');
  assert.equal(alpha.type, 'password'); assert.equal(fred.value, '');
  alpha.value = 'private-new-alpha';
  document.querySelector('#provider-form').dispatchEvent(new window.Event('submit', { cancelable: true }));
  await settle();
  const call = calls.find(item => item.url.endsWith('/providers') && item.options.method === 'PUT');
  assert.deepEqual(JSON.parse(call.options.body), { alpha_vantage: 'private-new-alpha' });
  assert.equal(alpha.value, ''); assert.equal(fred.value, '');
  assert.equal(document.body.textContent.includes('private-new-alpha'), false);
});

test('public consent failure offers only a known resource authorization URL', async context => {
  const scope = 'https://api.loganalytics.io/.default';
  const { document, window } = await setup(context, {
    configuration: { ...config, public_mode: true, model: 'research-model' },
    fetchOverride: (url, options) => url === '/api/runs' && options.method === 'POST'
      ? jsonResponse({ code: 'authentication_required', scope, detail: 'private-debug' }, 401) : publicResponse(url),
  });
  document.querySelector('#config-form').dispatchEvent(new window.Event('submit', { cancelable: true }));
  await settle();
  const link = document.querySelector('#reauthorize');
  assert.equal(link.hidden, false);
  assert.equal(link.getAttribute('href'), '/auth/login?scope=' + encodeURIComponent(scope));
  assert.equal(document.body.textContent.includes('private-debug'), false);
});

test('public initialization binds confirmation and apply to the reviewed operation', async context => {
  const selection = { subscription_id: '11111111-1111-4111-8111-111111111111', resource_group: 'rg-own', location: 'eastus2', prefix: 'research', foundry_account_id: '/foundry/own', deployment_name: 'own-model' };
  const preview = { id: 'dddddddd-4444-4444-8444-dddddddddddd', status: 'preview', confirmation: 'CREATE rg-own/research', confirmed_at: null, completed_steps: [],
    plan: { selection, resources: [{ kind: 'job', id: '/new/job', name: 'research-job' }], roles: [{ role: 'Cognitive Services OpenAI User', scope: '/foundry/own' }], group: { create: true, id: '/new/rg' }, charges: ['Build and execution charges'], source_sha256: 'a'.repeat(64) } };
  const { document, window, calls, advance } = await setup(context, {
    configuration: { ...config, public_mode: true },
    fetchOverride: (url, options) => {
      if (url === '/api/initializations' && options.method === 'POST') return jsonResponse(preview, 201);
      if (url.endsWith('/confirm')) return jsonResponse({ ...preview, status: 'confirmed', confirmed_at: 123 });
      if (url.endsWith('/apply')) return jsonResponse({ detail: 'Private Azure details' }, 403);
      return publicResponse(url);
    },
  });
  document.querySelector('#manage-resources').click(); await settle();
  for (const [name, value] of Object.entries(selection)) document.querySelector('#init-' + name).value = value;
  document.querySelector('#initialization-form').dispatchEvent(new window.Event('submit', { cancelable: true })); await settle();
  const posted = calls.find(item => item.url === '/api/initializations' && item.options.method === 'POST');
  assert.deepEqual(JSON.parse(posted.options.body), selection);
  assert.match(document.querySelector('#initialization-plan').textContent, /\/new\/job/);
  assert.match(document.querySelector('#initialization-plan').textContent, /Build and execution charges/);
  assert.equal(document.querySelector('#initialization-confirm').disabled, true);
  document.querySelector('#initialization-confirmation').value = preview.confirmation;
  document.querySelector('#initialization-confirmation').dispatchEvent(new window.Event('input'));
  document.querySelector('#initialization-confirm').click(); await settle();
  assert.equal(calls.filter(item => item.url.endsWith('/apply')).length, 0);
  const confirmation = calls.find(item => item.url.endsWith('/confirm'));
  assert.equal(confirmation.url, '/api/initializations/' + preview.id + '/confirm');
  assert.deepEqual(JSON.parse(confirmation.options.body), { confirmation: preview.confirmation });
  document.querySelector('#init-prefix').value = 'changed-after-preview';
  document.querySelector('#initialization-resume').click(); await settle(); await advance(15000);
  const applications = calls.filter(item => item.url.endsWith('/apply'));
  assert.equal(applications.length, 1);
  assert.deepEqual(JSON.parse(applications[0].options.body), {});
  assert.equal(document.querySelector('#resource-error').hidden, false);
  assert.equal(document.body.textContent.includes('Private Azure details'), false);
});

test('public initialization restores confirmed operations without starting them', async context => {
  const saved = { id: 'eeeeeeee-5555-4555-8555-eeeeeeeeeeee', status: 'running', confirmed_at: 123, completed_steps: ['group'], current_step: 'registry',
    plan: { selection: { resource_group: 'rg-saved', prefix: 'saved' }, resources: [], roles: [], charges: [], group: { create: false, id: '/saved/rg' } } };
  const { document, calls, advance } = await setup(context, {
    configuration: { ...config, public_mode: true },
    fetchOverride: url => url === '/api/initializations' ? jsonResponse({ initializations: [saved] }) : publicResponse(url),
  });
  document.querySelector('#manage-resources').click(); await settle(); await advance(15000);
  assert.equal(document.querySelector('#initialization-history').value, saved.id);
  assert.equal(document.querySelector('#initialization-resume').disabled, false);
  assert.equal(calls.filter(item => item.url.endsWith('/apply')).length, 0);
});

test('public reservations block the same Job while allowing a different selected Job', async context => {
  const reserved = { ...run, profile_id: publicProfiles[0].id, profile_snapshot: publicProfiles[0] };
  const { document, window } = await setup(context, { runs: [reserved], configuration: { ...config, public_mode: true }, fetchOverride: publicResponse });
  assert.equal(document.querySelector('#start-run').disabled, true);
  document.querySelector('#profile-select').value = publicProfiles[1].id;
  document.querySelector('#profile-select').dispatchEvent(new window.Event('change'));
  assert.equal(document.querySelector('#start-run').disabled, false);
  assert.equal(document.querySelector('#rerun').disabled, true);
  assert.match(document.querySelector('#run-meta').textContent, /Research A/);
});

test('public uncertain starts reconcile an execution without resubmitting research', async context => {
  const uncertain = { ...run, profile_id: publicProfiles[0].id, profile_snapshot: publicProfiles[0], status: 'failed', start_uncertain: true, execution_name: null };
  const { document, window, calls } = await setup(context, {
    runs: [uncertain], configuration: { ...config, public_mode: true },
    fetchOverride: url => url.endsWith('/reconcile') ? jsonResponse({ ...uncertain, status: 'running', start_uncertain: false, execution_name: 'research-verified' }) : publicResponse(url),
  });
  assert.equal(document.querySelector('#reconcile-form').hidden, false);
  document.querySelector('#reconcile-execution').value = 'research-verified';
  document.querySelector('#reconcile-form').dispatchEvent(new window.Event('submit', { cancelable: true })); await settle();
  const call = calls.find(item => item.url.endsWith('/reconcile'));
  assert.equal(call.url, '/api/runs/' + run.id + '/reconcile');
  assert.deepEqual(JSON.parse(call.options.body), { execution_name: 'research-verified' });
  assert.equal(document.querySelector('#reconcile-form').hidden, true);
  assert.equal(calls.filter(item => item.url === '/api/runs' && item.options.method === 'POST').length, 0);
});

test('email mode loads authenticated settings endpoints and submits owner-free hosted runs', async context => {
  const responses = {
    '/api/me': { user: { object_id: 'owner-1', email: 'owner@example.com', name: 'Owner User', is_admin: true }, csrf_token: 'email-csrf-1' },
    '/api/settings/catalog': emailCatalog,
    '/api/settings': emailSettings,
    '/api/budget': emailBudget,
    '/api/runs': { runs: [] },
  };
  const { document, window, calls } = await setup(context, {
    fetchOverride: (url, options = {}) => {
      if (url === '/api/runs' && options.method === 'POST') {
        return jsonResponse({ ...run, status: 'starting', request: JSON.parse(options.body).request }, 202);
      }
      if (url === `/api/runs/${run.id}/reconcile` && options.method === 'POST') {
        return jsonResponse({ ...run, status: 'running', start_uncertain: false }, 200);
      }
      if (Object.hasOwn(responses, url)) return jsonResponse(responses[url]);
    },
  });

  await settle();
  assert.equal(document.querySelector('#start-run').disabled, false);
  assert.equal(document.querySelector('#account-name')?.textContent, 'Owner User');
  assert.deepEqual(calls.filter(call => call.url !== '/api/runs').slice(0, 4).map(call => call.url), ['/api/me', '/api/settings/catalog', '/api/settings', '/api/budget']);

  document.querySelector('#config-form').dispatchEvent(new window.Event('submit', { cancelable: true }));
  await settle();
  const submitted = JSON.parse(calls.find(item => item.url === '/api/runs' && item.options.method === 'POST').options.body);
  assert.deepEqual(Object.keys(submitted).sort(), ['request', 'submission_id']);
  assert.match(submitted.submission_id, /^[0-9a-f-]{36}$/);
  assert.equal(submitted.request.ticker, 'MSFT');
});

test('email mode hides manual reconciliation and sends no reconcile request', async context => {
  const responses = {
    '/api/me': { user: { object_id: 'owner-1', email: 'owner@example.com', name: 'Owner User', is_admin: true }, csrf_token: 'email-csrf-1' },
    '/api/settings/catalog': emailCatalog,
    '/api/settings': emailSettings,
    '/api/budget': emailBudget,
  };
  const uncertain = { ...run, status: 'reconciliation_required', error_code: 'reconciliation_required', error: 'Reconcile before further action.', start_uncertain: true, execution_name: null };
  let reconcileBody;
  const rerender = await setup(context, {
    runs: [uncertain],
    fetchOverride: (url, options = {}) => {
      if (Object.hasOwn(responses, url)) return jsonResponse(responses[url]);
      if (url === '/api/runs' && options.method === 'GET') return jsonResponse({ runs: [uncertain] });
      if (url === `/api/runs/${run.id}`) return jsonResponse(uncertain);
      if (url === `/api/runs/${run.id}/reconcile` && options.method === 'POST') {
        reconcileBody = options.body;
        return jsonResponse({ ...uncertain, status: 'running', start_uncertain: false });
      }
    },
  });
  assert.equal(rerender.document.querySelector('#reconcile-form').hidden, true);
  assert.doesNotMatch(rerender.document.querySelector('#workspace-error').textContent, /Reconcile/);
  assert.equal(rerender.document.querySelector('#run-status').textContent, '正在恢复');
  rerender.document.querySelector('#reconcile-form').dispatchEvent(new rerender.window.Event('submit', { cancelable: true }));
  await settle();
  await settle();
  assert.equal(reconcileBody, undefined);
});

test('email retry reuses submission id after a lost response', async context => {
  const responses = {
    '/api/me': { user: { object_id: 'owner-1', email: 'owner@example.com', name: 'Owner User', is_admin: true }, csrf_token: 'email-csrf-1' },
    '/api/settings/catalog': emailCatalog,
    '/api/settings': emailSettings,
    '/api/budget': emailBudget,
    '/api/runs': { runs: [] },
  };
  const { document, window, calls } = await setup(context, {
    fetchOverride: (url, options = {}) => {
      if (url === '/api/runs' && options.method === 'POST') return jsonResponse({}, 503);
      if (Object.hasOwn(responses, url)) return jsonResponse(responses[url]);
    },
  });
  document.querySelector('#config-form').dispatchEvent(new window.Event('submit', { cancelable: true }));
  await settle();
  document.querySelector('#config-form').dispatchEvent(new window.Event('submit', { cancelable: true }));
  await settle();
  const submissions = calls.filter(call => call.url === '/api/runs' && call.options.method === 'POST').map(call => JSON.parse(call.options.body));
  assert.equal(submissions.length, 2);
  assert.equal(submissions[0].submission_id, submissions[1].submission_id);
  assert.deepEqual(submissions[0].request, submissions[1].request);
});

test('email prepared run progresses by polling without manual resume or duplicate submission', async context => {
  const responses = {
    '/api/me': { user: { object_id: 'owner-1', email: 'owner@example.com', name: 'Owner User', is_admin: true }, csrf_token: 'email-csrf-1' },
    '/api/settings/catalog': emailCatalog,
    '/api/settings': emailSettings,
    '/api/budget': emailBudget,
  };
  let current = { ...run, status: 'prepared', start_uncertain: false, start_attempted: false, stop_requested: false, execution_name: null };
  const { document, calls } = await setup(context, {
    runs: [current],
    fetchOverride: (url, options = {}) => {
      if (Object.hasOwn(responses, url)) return jsonResponse(responses[url]);
      if (url === '/api/runs') return jsonResponse({ runs: [current] });
      if (url === `/api/runs/${run.id}`) return jsonResponse(current);
    },
  });
  const button = document.querySelector('#resume-run');
  assert.ok(button);
  assert.equal(button.hidden, true);
  assert.equal(button.disabled, true);
  assert.equal(document.querySelector('#run-status').textContent, '正在启动');
  assert.equal(document.querySelector('#start-run').disabled, true);
  assert.equal(document.querySelector('#rerun').disabled, true);
  assert.equal(document.querySelector('#stop-run').disabled, false);
  document.querySelector('#refresh').click(); await settle();
  assert.equal(calls.filter(call => call.options.method === 'POST').length, 0);
  button.click(); button.click(); await settle();
  current = { ...current, status: 'running', start_attempted: true, execution_name: 'research-test' };
  document.querySelector('#refresh').click(); await settle();
  assert.equal(document.querySelector('#run-status').textContent, '运行中');
  assert.equal(button.hidden, true);
  assert.equal(calls.filter(call => call.options.method === 'POST').length, 0);
});

test('email settings save updates settings without starting a research run', async context => {
  const responses = {
    '/api/me': { user: { object_id: 'owner-1', email: 'owner@example.com', name: 'Owner User', is_admin: true }, csrf_token: 'email-csrf-1' },
    '/api/settings/catalog': emailCatalog,
    '/api/settings': emailSettings,
    '/api/budget': emailBudget,
    '/api/runs': { runs: [] },
  };
  const { document, window, calls } = await setup(context, {
    fetchOverride: (url, options = {}) => {
      if (url === '/api/settings' && options.method === 'PUT') {
        return jsonResponse({
          ...emailSettings,
          version: 'settings-v2',
          providers: {
            ...emailSettings.providers,
            openai: {
              ...emailSettings.providers.openai,
              quick_model: 'gpt-5',
            },
          },
        });
      }
      if (Object.hasOwn(responses, url)) return jsonResponse(responses[url]);
    },
  });

  document.querySelector('#settings-quick-model').value = 'custom';
  document.querySelector('#settings-quick-model').dispatchEvent(new window.Event('change', { bubbles: true }));
  document.querySelector('#settings-quick-custom').value = 'gpt-5';
  document.querySelector('#model-settings-form').dispatchEvent(new window.Event('submit', { bubbles: true, cancelable: true }));
  await settle();

  assert.equal(calls.filter(call => call.url === '/api/settings' && call.options.method === 'PUT').length, 1);
  assert.equal(calls.filter(call => call.url === '/api/runs' && call.options.method === 'POST').length, 0);
});

test('email settings save uses the current CSRF token after password rotation', async context => {
  let csrfToken = 'email-csrf-1';
  const { document, window, calls } = await setup(context, {
    fetchOverride: (url, options = {}) => {
      if (url === '/api/me') {
        return jsonResponse({ user: { object_id: 'owner-1', email: 'owner@example.com', name: 'Owner User', is_admin: true }, csrf_token: csrfToken });
      }
      if (url === '/api/settings/catalog') return jsonResponse(emailCatalog);
      if (url === '/api/settings' && options.method !== 'PUT') return jsonResponse(emailSettings);
      if (url === '/api/budget') return jsonResponse(emailBudget);
      if (url === '/api/runs') return jsonResponse({ runs: [] });
      if (url === '/auth/password' && options.method === 'POST') {
        csrfToken = 'email-csrf-2';
        return jsonResponse({ user: { object_id: 'owner-1', email: 'owner@example.com', name: 'Owner User', is_admin: true }, csrf_token: csrfToken });
      }
      if (url === '/api/settings' && options.method === 'PUT') return jsonResponse({ ...emailSettings, version: 'settings-v2' });
    },
  });

  document.querySelector('#account-panel-toggle').click();
  await settle();
  document.querySelector('#account-current-password').value = 'old-password';
  document.querySelector('#account-new-password').value = 'new-password';
  document.querySelector('#password-form').dispatchEvent(new window.Event('submit', { bubbles: true, cancelable: true }));
  await settle();
  await settle();

  document.querySelector('#model-settings-form').dispatchEvent(new window.Event('submit', { bubbles: true, cancelable: true }));
  await settle();

  const settingsWrite = calls.findLast(call => call.url === '/api/settings' && call.options.method === 'PUT');
  assert.equal(settingsWrite.options.headers['X-Workbench-Token'], 'email-csrf-2');
});

test('password rotation reloads history despite a pending home request without changing navigation', async context => {
  let releaseOldHistory;
  let releaseNewHistory;
  let historyRequests = 0;
  let csrfToken = 'email-csrf-1';
  const freshRun = { ...run, id: 'new-session-run', request: { ...defaults, ticker: 'FRESH' } };
  const { document, window, workbench, calls } = await setupHome(context, {
    waitForReady: false,
    fetchOverride: (url, options = {}) => {
      if (url === '/api/me') return jsonResponse({ user: { object_id: 'owner-1', email: 'owner@example.com', name: 'Owner User', is_admin: true }, csrf_token: csrfToken });
      if (url === '/api/settings/catalog') return jsonResponse(emailCatalog);
      if (url === '/api/settings') return jsonResponse(emailSettings);
      if (url === '/api/budget') return jsonResponse(emailBudget);
      if (url === '/api/runs' && options.method === 'GET') {
        historyRequests += 1;
        return new Promise(resolve => {
          if (historyRequests === 1) releaseOldHistory = resolve;
          else releaseNewHistory = resolve;
        });
      }
      if (url === '/auth/password' && options.method === 'POST') {
        csrfToken = 'email-csrf-2';
        return jsonResponse({ user: { object_id: 'owner-1', email: 'owner@example.com', name: 'Owner User', is_admin: true }, csrf_token: csrfToken });
      }
    },
  });
  await workbench.ready;
  assert.equal(historyRequests, 1);
  assert.equal(document.documentElement.dataset.home, 'true');

  document.querySelector('#account-panel-toggle').click();
  await settle();
  document.querySelector('#account-current-password').value = 'old-password';
  document.querySelector('#account-new-password').value = 'new-password';
  document.querySelector('#password-form').dispatchEvent(new window.Event('submit', { bubbles: true, cancelable: true }));
  await settle();
  await settle();
  assert.equal(csrfToken, 'email-csrf-2');
  assert.equal(historyRequests, 2, 'new session should request history without waiting for old session');

  releaseOldHistory(jsonResponse({ runs: [run] }));
  await settle();
  assert.equal(document.querySelectorAll('#history-list .history-item').length, 0);
  assert.equal(document.documentElement.dataset.home, 'true');
  document.querySelector('#new-research').click();
  assert.equal(document.documentElement.dataset.home, 'true');
  window.document.dispatchEvent(new window.Event('visibilitychange'));
  await settle();
  assert.equal(historyRequests, 2, 'stale completion must not release the new history flight');

  releaseNewHistory(jsonResponse({ runs: [freshRun] }));
  await settle();
  assert.equal(document.querySelector('#history-list .history-item strong')?.textContent, 'FRESH');
  assert.equal(document.querySelectorAll('#history-list .history-item').length, 1);
  assert.equal(document.querySelector('#research-tasks-entry .count'), null);
  assert.equal(document.documentElement.dataset.home, 'true');
  assert.equal(document.querySelector('#research-drawer').open, true);
  assert.equal(calls.filter(call => call.url === '/api/runs' && call.options.method === 'GET').length, 2);
});

test('email mode renders all 18 supported providers and keeps managed identity owner-only', async context => {
  const { document, calls } = await setup(context, {
    fetchOverride: url => {
      if (url === '/api/me') return jsonResponse({ user: { object_id: 'guest-1', email: 'guest@example.com', name: 'Guest User', is_admin: false }, csrf_token: 'email-csrf-guest' });
      if (url === '/api/settings/catalog') return jsonResponse(emailCatalog);
      if (url === '/api/settings') return jsonResponse(emailSettings);
      if (url === '/api/runs') return jsonResponse({ runs: [] });
    },
  });

  assert.deepEqual([...document.querySelector('#settings-provider').options].map(option => option.value), emailProviderOrder);
  assert.equal(document.querySelector('#settings-mode option[value="managed_identity"]').disabled, true);
  assert.equal(calls.some(call => call.url === '/api/budget'), false);
  document.querySelector('#account-panel-toggle').click();
  await settle();
  assert.equal(document.querySelector('.budget-section').hidden, true);
});

test('email mode renders the current-model label in the live DOM', async context => {
  const { document } = await setup(context, {
    fetchOverride: url => {
      if (url === '/api/me') return jsonResponse({ user: { object_id: 'guest-1', email: 'guest@example.com', name: 'Guest User', is_admin: false }, csrf_token: 'email-csrf-guest' });
      if (url === '/api/settings/catalog') return jsonResponse(emailCatalog);
      if (url === '/api/settings') return jsonResponse(emailSettings);
      if (url === '/api/runs') return jsonResponse({ runs: [] });
    },
  });

  assert.equal(document.querySelector('.model-line span').textContent, '当前模型');
});

test('email managed identity shows the actual fixed Foundry deployment without an API key', async context => {
  const responses = {
    '/api/me': { user: { object_id: 'owner-1', is_admin: true }, csrf_token: 'email-csrf-1' },
    '/api/settings/catalog': emailCatalog,
    '/api/settings': { ...emailSettings, mode: 'managed_identity' },
    '/api/budget': emailBudget,
    '/api/runs': { runs: [] },
  };
  const { document } = await setup(context, {
    fetchOverride: url => Object.hasOwn(responses, url) ? jsonResponse(responses[url]) : undefined,
  });

  assert.match(document.querySelector('.model-line').textContent, /gpt-5\.6-sol/);
  const provider = document.querySelector('#settings-provider');
  assert.deepEqual([...provider.options].map(option => option.textContent), ['Azure Foundry (托管身份)']);
  assert.equal(provider.disabled, true);
  for (const kind of ['quick', 'deep']) {
    const model = document.querySelector(`#settings-${kind}-model`);
    assert.deepEqual([...model.options].map(option => option.value), ['gpt-5.6-sol']);
    assert.equal(model.value, 'gpt-5.6-sol');
    assert.equal(model.disabled, true);
    assert.equal(document.querySelector(`#settings-${kind}-custom`).hidden, true);
  }
  assert.equal(document.querySelector('#settings-base-url-wrap').hidden, false);
  assert.equal(document.querySelector('#settings-base-url').value, 'https://fixture-foundry.services.ai.azure.com/');
  assert.equal(document.querySelector('#settings-base-url').readOnly, true);
  assert.equal(document.querySelector('#settings-api-version-wrap').hidden, true);
  assert.equal(document.querySelector('#settings-key-state').textContent, '无需 API Key');
  assert.equal(document.querySelector('#settings-api-key').hidden, true);
  assert.equal(document.querySelector('#settings-api-key').disabled, true);
  assert.equal(document.querySelector('#settings-clear-key').hidden, true);
  assert.equal(document.querySelector('#start-run').disabled, false);
});

test('email managed identity save excludes Foundry fields and retains BYOK profiles', async context => {
  const saved = { ...emailSettings, mode: 'managed_identity' };
  const responses = {
    '/api/me': { user: { object_id: 'owner-1', is_admin: true }, csrf_token: 'email-csrf-1' },
    '/api/settings/catalog': emailCatalog,
    '/api/settings': saved,
    '/api/budget': emailBudget,
    '/api/runs': { runs: [] },
  };
  const { document, window, calls } = await setup(context, {
    fetchOverride: (url, options = {}) => {
      if (url === '/api/settings' && options.method === 'PUT') return jsonResponse({ ...saved, version: 'settings-v2' });
      if (Object.hasOwn(responses, url)) return jsonResponse(responses[url]);
    },
  });

  document.querySelector('#model-settings-form').dispatchEvent(new window.Event('submit', { bubbles: true, cancelable: true }));
  await settle();
  const patch = JSON.parse(calls.findLast(call => call.url === '/api/settings' && call.options.method === 'PUT').options.body);
  assert.deepEqual(patch, { mode: 'managed_identity', asset_type: 'stock', data_vendors: defaults.data_vendors });
  assert.match(document.querySelector('.model-line').textContent, /gpt-5\.6-sol/);
  assert.equal(document.querySelector('#settings-quick-model').disabled, true);
  document.querySelector('#settings-mode').value = 'api_key';
  document.querySelector('#settings-mode').dispatchEvent(new window.Event('change', { bubbles: true }));
  assert.deepEqual([...document.querySelector('#settings-provider').options].map(option => option.value), emailProviderOrder);
  assert.equal(document.querySelector('#settings-provider').value, 'openai');
  assert.equal(document.querySelector('#settings-quick-model').value, 'gpt-5-mini');
  assert.equal(document.querySelector('#settings-deep-model').value, 'gpt-5');
  assert.equal(document.querySelector('#settings-base-url').value, 'https://api.openai.com/v1');
  assert.equal(calls.filter(call => call.url === '/api/runs' && call.options.method === 'POST').length, 0);
});

test('email managed identity allows multiple deployments, saves the selected luna/terra pair, and keeps provider locks', async context => {
  const catalog = {
    ...emailCatalog,
    modes: {
      ...emailCatalog.modes,
      managed_identity: {
        admin_only: true,
        available: true,
        configuration: { deployment: 'sol', endpoint: 'https://fixture-foundry.services.ai.azure.com/', model_family: 'gpt-5.6', model_version: '2026-07-09' },
        configurations: [
          { deployment: 'sol', endpoint: 'https://fixture-foundry.services.ai.azure.com/', model_family: 'gpt-5.6', model_version: '2026-07-09' },
          { deployment: 'luna', endpoint: 'https://luna.services.ai.azure.com/', model_family: 'gpt-5.6', model_version: '2026-07-09' },
          { deployment: 'terra', endpoint: 'https://terra.services.ai.azure.com/', model_family: 'gpt-5.6', model_version: '2026-07-09' },
        ],
      },
    },
  };
  const saved = {
    ...emailSettings,
    mode: 'managed_identity',
    managed_models: { quick_model: 'luna', deep_model: 'terra' },
    quick_model: 'luna',
    deep_model: 'terra',
  };
  const responses = {
    '/api/me': { user: { object_id: 'owner-1', is_admin: true }, csrf_token: 'email-csrf-1' },
    '/api/settings/catalog': catalog,
    '/api/settings': saved,
    '/api/budget': emailBudget,
    '/api/runs': { runs: [] },
  };
  const { document, window, calls } = await setup(context, {
    fetchOverride: (url, options = {}) => {
      if (url === '/api/settings' && options.method === 'PUT') return jsonResponse({ ...saved, version: 'settings-v2' });
      if (Object.hasOwn(responses, url)) return jsonResponse(responses[url]);
    },
  });

  const quick = document.querySelector('#settings-quick-model');
  const deep = document.querySelector('#settings-deep-model');
  assert.deepEqual([...quick.options].map(option => option.value), ['sol', 'luna', 'terra']);
  assert.deepEqual([...deep.options].map(option => option.value), ['sol', 'luna', 'terra']);
  assert.equal(quick.disabled, false);
  assert.equal(deep.disabled, false);
  assert.equal(document.querySelector('#settings-quick-custom').hidden, true);
  assert.equal(document.querySelector('#settings-deep-custom').hidden, true);
  assert.equal(quick.value, 'luna');
  assert.equal(deep.value, 'terra');
  assert.match(document.querySelector('.model-line').textContent, /terra/);
  assert.equal(document.querySelector('#settings-provider').disabled, true);
  assert.equal(document.querySelector('#settings-base-url').readOnly, true);
  assert.equal(document.querySelector('#settings-api-key').hidden, true);
  assert.equal(document.querySelector('#settings-clear-key').hidden, true);

  quick.value = 'luna'; deep.value = 'terra';
  document.querySelector('#model-settings-form').dispatchEvent(new window.Event('submit', { bubbles: true, cancelable: true }));
  await settle();
  const patch = JSON.parse(calls.findLast(call => call.url === '/api/settings' && call.options.method === 'PUT').options.body);
  assert.deepEqual(patch, {
    mode: 'managed_identity',
    asset_type: 'stock',
    data_vendors: defaults.data_vendors,
    managed_models: { quick_model: 'luna', deep_model: 'terra' },
  });
  assert.equal(document.querySelector('#settings-quick-model').value, 'luna');
  assert.equal(document.querySelector('#settings-deep-model').value, 'terra');
});

test('email managed identity save replaces unavailable selections with the sole visible deployment', async context => {
  const saved = { ...emailSettings, mode: 'managed_identity', managed_models: { quick_model: 'expired-luna', deep_model: 'expired-terra' } };
  const responses = {
    '/api/me': { user: { object_id: 'owner-1', is_admin: true }, csrf_token: 'email-csrf-1' },
    '/api/settings/catalog': emailCatalog,
    '/api/settings': saved,
    '/api/budget': emailBudget,
    '/api/runs': { runs: [] },
  };
  const { document, window, calls } = await setup(context, {
    fetchOverride: (url, options = {}) => {
      if (url === '/api/settings' && options.method === 'PUT') return jsonResponse({ ...saved, ...JSON.parse(options.body), version: 'settings-v2' });
      if (Object.hasOwn(responses, url)) return jsonResponse(responses[url]);
    },
  });
  assert.equal(document.querySelector('#settings-quick-model').value, 'gpt-5.6-sol');
  assert.equal(document.querySelector('#settings-deep-model').disabled, true);
  document.querySelector('#model-settings-form').dispatchEvent(new window.Event('submit', { bubbles: true, cancelable: true }));
  await settle();
  const patch = JSON.parse(calls.findLast(call => call.url === '/api/settings' && call.options.method === 'PUT').options.body);
  assert.deepEqual(patch.managed_models, { quick_model: 'gpt-5.6-sol', deep_model: 'gpt-5.6-sol' });
  assert.equal(calls.filter(call => call.url === '/api/runs' && call.options.method === 'POST').length, 0);
});

test('email managed identity toggle restores all BYOK providers and the selected Azure profile', async context => {
  const azureProfile = {
    quick_model: 'private-quick', deep_model: 'private-deep',
    base_url: 'https://byok.example.com/', api_version: '2025-04-01-preview', key_configured: true,
  };
  const settings = { ...emailSettings, providers: { ...emailSettings.providers, azure: azureProfile } };
  const responses = {
    '/api/me': { user: { object_id: 'owner-1', is_admin: true }, csrf_token: 'email-csrf-1' },
    '/api/settings/catalog': emailCatalog,
    '/api/settings': settings,
    '/api/budget': emailBudget,
    '/api/runs': { runs: [] },
  };
  const { document, window, calls } = await setup(context, {
    fetchOverride: (url, options = {}) => {
      if (url === '/api/settings' && options.method === 'PUT') return jsonResponse({ ...settings, provider: 'azure', version: 'settings-v2' });
      if (Object.hasOwn(responses, url)) return jsonResponse(responses[url]);
    },
  });
  document.querySelector('#settings-provider').value = 'azure';
  document.querySelector('#settings-provider').dispatchEvent(new window.Event('change', { bubbles: true }));
  document.querySelector('#settings-api-key').value = 'transient-byok-key';
  document.querySelector('#settings-mode').value = 'managed_identity';
  document.querySelector('#settings-mode').dispatchEvent(new window.Event('change', { bubbles: true }));
  assert.deepEqual([...document.querySelector('#settings-provider').options].map(option => option.textContent), ['Azure Foundry (托管身份)']);
  assert.equal(document.querySelector('#settings-quick-model').value, 'gpt-5.6-sol');
  assert.equal(document.querySelector('#settings-api-key').value, '');

  document.querySelector('#settings-mode').value = 'api_key';
  document.querySelector('#settings-mode').dispatchEvent(new window.Event('change', { bubbles: true }));
  assert.deepEqual([...document.querySelector('#settings-provider').options].map(option => option.value), emailProviderOrder);
  assert.equal(document.querySelector('#settings-provider').value, 'azure');
  assert.equal(document.querySelector('#settings-provider').disabled, false);
  assert.equal(document.querySelector('#settings-quick-custom').value, 'private-quick');
  assert.equal(document.querySelector('#settings-deep-custom').value, 'private-deep');
  assert.equal(document.querySelector('#settings-quick-model').disabled, false);
  assert.equal(document.querySelector('#settings-quick-custom').disabled, false);
  assert.equal(document.querySelector('#settings-deep-model').disabled, false);
  assert.equal(document.querySelector('#settings-base-url').value, 'https://byok.example.com/');
  assert.equal(document.querySelector('#settings-base-url').readOnly, false);
  assert.equal(document.querySelector('#settings-api-version-wrap').hidden, false);
  assert.equal(document.querySelector('#settings-api-version').value, '2025-04-01-preview');
  assert.equal(document.querySelector('#settings-key-state').textContent, '已配置');
  assert.equal(document.querySelector('#settings-api-key').hidden, false);
  assert.equal(document.querySelector('#settings-api-key').disabled, false);
  assert.equal(document.querySelector('#settings-clear-key').hidden, false);
  document.querySelector('#model-settings-form').dispatchEvent(new window.Event('submit', { bubbles: true, cancelable: true }));
  await settle();
  const patch = JSON.parse(calls.findLast(call => call.url === '/api/settings' && call.options.method === 'PUT').options.body);
  assert.deepEqual(patch, {
    mode: 'api_key', asset_type: 'stock', data_vendors: defaults.data_vendors,
    provider: 'azure', quick_model: 'private-quick', deep_model: 'private-deep',
    base_url: 'https://byok.example.com/', api_version: '2025-04-01-preview',
  });
});

test('email failed save restores the server-authoritative model selection instead of the stale DOM value', async context => {
  const responses = {
    '/api/me': { user: { object_id: 'owner-1', email: 'owner@example.com', name: 'Owner User', is_admin: true }, csrf_token: 'email-csrf-1' },
    '/api/settings/catalog': emailCatalog,
    '/api/settings': emailSettings,
    '/api/budget': emailBudget,
    '/api/runs': { runs: [] },
  };
  const { document, window } = await setup(context, {
    fetchOverride: (url, options = {}) => {
      if (url === '/api/settings' && options.method === 'PUT') return jsonResponse({ ok: false, status: 409, detail: 'state changed' }, 409);
      if (Object.hasOwn(responses, url)) return jsonResponse(responses[url]);
    },
  });

  document.querySelector('#settings-provider').value = 'azure';
  document.querySelector('#settings-provider').dispatchEvent(new window.Event('change', { bubbles: true }));
  document.querySelector('#settings-quick-model').value = 'custom';
  document.querySelector('#settings-quick-model').dispatchEvent(new window.Event('change', { bubbles: true }));
  document.querySelector('#settings-quick-custom').value = 'stale-quick';
  document.querySelector('#settings-deep-model').value = 'custom';
  document.querySelector('#settings-deep-model').dispatchEvent(new window.Event('change', { bubbles: true }));
  document.querySelector('#settings-deep-custom').value = 'stale-deep';
  document.querySelector('#model-settings-form').dispatchEvent(new window.Event('submit', { bubbles: true, cancelable: true }));
  await settle();

  assert.equal(document.querySelector('#settings-provider').value, 'openai');
  assert.equal(document.querySelector('#settings-quick-model').value, 'gpt-5-mini');
  assert.equal(document.querySelector('#settings-deep-model').value, 'gpt-5');
  assert.equal(document.querySelector('#settings-quick-custom').hidden, true);
  assert.equal(document.querySelector('#settings-deep-custom').hidden, true);
});

test('email provider switches prefer the saved provider model when model ids overlap across providers', async context => {
  const catalog = {
    ...emailCatalog,
    providers: {
      ...emailCatalog.providers,
      openai: providerFixture({ label: 'OpenAI', endpoint_kind: 'approved_origin', base_url: 'https://api.openai.com/v1', quick: [['gpt-5-mini', 'gpt-5-mini']], deep: [['gpt-5', 'gpt-5']] }),
      azure: providerFixture({ label: 'Azure OpenAI', endpoint_kind: 'azure', available_managed_identity: true, quick: [['gpt-5-mini', 'gpt-5-mini'], ['gpt-5-pro', 'gpt-5-pro']], deep: [['gpt-5', 'gpt-5'], ['gpt-5-pro', 'gpt-5-pro']] }),
    },
  };
  const settings = {
    ...emailSettings,
    provider: 'openai',
    providers: {
      ...emailSettings.providers,
      openai: { ...emailSettings.providers.openai, quick_model: 'gpt-5-mini', deep_model: 'gpt-5' },
      azure: { quick_model: 'gpt-5-pro', deep_model: 'gpt-5-pro', base_url: 'https://byok.example.com/', api_version: '2025-04-01-preview', key_configured: true },
    },
  };
  const responses = {
    '/api/me': { user: { object_id: 'owner-1', is_admin: true }, csrf_token: 'email-csrf-1' },
    '/api/settings/catalog': catalog,
    '/api/settings': settings,
    '/api/budget': emailBudget,
    '/api/runs': { runs: [] },
  };
  const { document, window } = await setup(context, {
    fetchOverride: url => Object.hasOwn(responses, url) ? jsonResponse(responses[url]) : undefined,
  });

  document.querySelector('#settings-provider').value = 'azure';
  document.querySelector('#settings-provider').dispatchEvent(new window.Event('change', { bubbles: true }));

  assert.equal(document.querySelector('#settings-provider').value, 'azure');
  assert.equal(document.querySelector('#settings-quick-model').value, 'gpt-5-pro');
  assert.equal(document.querySelector('#settings-deep-model').value, 'gpt-5-pro');
  assert.equal(document.querySelector('#settings-quick-custom').hidden, true);
  assert.equal(document.querySelector('#settings-deep-custom').hidden, true);

  document.querySelector('#settings-mode').value = 'managed_identity';
  document.querySelector('#settings-mode').dispatchEvent(new window.Event('change', { bubbles: true }));
  assert.equal(document.querySelector('#settings-quick-model').value, 'gpt-5.6-sol');
  document.querySelector('#settings-mode').value = 'api_key';
  document.querySelector('#settings-mode').dispatchEvent(new window.Event('change', { bubbles: true }));
  assert.equal(document.querySelector('#settings-provider').value, 'azure');
  assert.equal(document.querySelector('#settings-quick-model').value, 'gpt-5-pro');
  assert.equal(document.querySelector('#settings-deep-model').value, 'gpt-5-pro');
});

test('email provider switch clears pending secrets and uses the new provider defaults', async context => {
  const responses = {
    '/api/me': { user: { object_id: 'owner-1', email: 'owner@example.com', name: 'Owner User', is_admin: true }, csrf_token: 'email-csrf-1' },
    '/api/settings/catalog': emailCatalog,
    '/api/settings': emailSettings,
    '/api/budget': emailBudget,
    '/api/runs': { runs: [] },
  };
  const { document, window, calls } = await setup(context, {
    fetchOverride: (url, options = {}) => {
      if (url === '/api/settings' && options.method === 'PUT') return jsonResponse({ ...emailSettings, version: 'settings-v2' });
      if (Object.hasOwn(responses, url)) return jsonResponse(responses[url]);
    },
  });

  document.querySelector('#settings-api-key').value = 'openai-secret';
  document.querySelector('#settings-provider').value = 'azure';
  document.querySelector('#settings-provider').dispatchEvent(new window.Event('change', { bubbles: true }));
  document.querySelector('#model-settings-form').dispatchEvent(new window.Event('submit', { bubbles: true, cancelable: true }));
  await settle();

  const patch = JSON.parse(calls.findLast(call => call.url === '/api/settings' && call.options.method === 'PUT').options.body);
  assert.equal(patch.provider, 'azure');
  assert.equal(patch.api_key, undefined);
  assert.equal(patch.quick_model, 'azure-gpt-5-mini');
  assert.equal(patch.deep_model, 'azure-gpt-5');
});

test('email key deletion stays bound to the provider that requested it', async context => {
  const responses = {
    '/api/me': { user: { object_id: 'owner-1', email: 'owner@example.com', name: 'Owner User', is_admin: true }, csrf_token: 'email-csrf-1' },
    '/api/settings/catalog': emailCatalog,
    '/api/settings': emailSettings,
    '/api/budget': emailBudget,
    '/api/runs': { runs: [] },
  };
  const { document, window, calls } = await setup(context, {
    fetchOverride: (url, options = {}) => {
      if (url === '/api/settings' && options.method === 'PUT') return jsonResponse({ ...emailSettings, version: 'settings-v2' });
      if (Object.hasOwn(responses, url)) return jsonResponse(responses[url]);
    },
  });

  document.querySelector('#settings-clear-key').click();
  document.querySelector('#settings-provider').value = 'azure';
  document.querySelector('#settings-provider').dispatchEvent(new window.Event('change', { bubbles: true }));
  document.querySelector('#model-settings-form').dispatchEvent(new window.Event('submit', { bubbles: true, cancelable: true }));
  await settle();

  const patch = JSON.parse(calls.findLast(call => call.url === '/api/settings' && call.options.method === 'PUT').options.body);
  assert.equal(patch.provider, 'azure');
  assert.equal(patch.api_key, undefined);
});

test('email settings save disables the panel while pending and clears secrets on bounded conflict', async context => {
  let resolveSave;
  const { document, window } = await setup(context, {
    fetchOverride: (url, options = {}) => {
      if (url === '/api/me') return jsonResponse({ user: { object_id: 'owner-1', email: 'owner@example.com', name: 'Owner User', is_admin: true }, csrf_token: 'email-csrf-1' });
      if (url === '/api/settings/catalog') return jsonResponse(emailCatalog);
      if (url === '/api/settings' && options.method !== 'PUT') return jsonResponse(emailSettings);
      if (url === '/api/budget') return jsonResponse(emailBudget);
      if (url === '/api/runs') return jsonResponse({ runs: [] });
      if (url === '/api/settings' && options.method === 'PUT') return new Promise(resolve => { resolveSave = resolve; });
    },
  });

  document.querySelector('#settings-api-key').value = 'transient-secret';
  document.querySelector('#model-settings-form').dispatchEvent(new window.Event('submit', { bubbles: true, cancelable: true }));
  assert.equal(document.querySelector('#settings-provider').disabled, true);
  assert.equal(document.querySelector('#settings-save').disabled, true);
  resolveSave({ ok: false, status: 409, json: async () => ({ detail: 'private stack trace' }) });
  await settle();
  await settle();

  assert.equal(document.querySelector('#settings-api-key').value, '');
  assert.equal(document.querySelector('#settings-save').disabled, false);
  assert.match(document.querySelector('#settings-message').textContent, /状态已变化|凭据/);
  assert.equal(document.body.textContent.includes('private stack trace'), false);
});

test('first successful email settings save enables research without a manual refresh', async context => {
  const initialSettings = {
    ...emailSettings,
    version: null,
    revision: null,
    quick_model: 'gpt-5-mini',
    deep_model: 'gpt-5',
    model_key_configured: false,
    providers: {
      openai: {
        ...emailSettings.providers.openai,
        key_configured: false,
      },
    },
  };
  const { document, window } = await setup(context, {
    fetchOverride: (url, options = {}) => {
      if (url === '/api/me') return jsonResponse({ user: { object_id: 'owner-1', email: 'owner@example.com', name: 'Owner User', is_admin: true }, csrf_token: 'email-csrf-1' });
      if (url === '/api/settings/catalog') return jsonResponse(emailCatalog);
      if (url === '/api/settings' && options.method !== 'PUT') return jsonResponse(initialSettings);
      if (url === '/api/budget') return jsonResponse(emailBudget);
      if (url === '/api/runs') return jsonResponse({ runs: [] });
      if (url === '/api/settings' && options.method === 'PUT') {
        return jsonResponse({
          ...initialSettings,
          version: 'settings-v2',
          revision: 'settings-r2',
          quick_model: 'gpt-5.6-terra',
          deep_model: 'gpt-5.6',
          model_key_configured: true,
          providers: {
            openai: {
              quick_model: 'gpt-5.6-terra',
              deep_model: 'gpt-5.6',
              base_url: 'https://api.openai.com/v1',
              key_configured: true,
            },
          },
        });
      }
    },
  });

  assert.equal(document.querySelector('#start-run').disabled, true);
  document.querySelector('#settings-api-key').value = 'fresh-secret';
  document.querySelector('#settings-quick-model').value = 'custom';
  document.querySelector('#settings-quick-model').dispatchEvent(new window.Event('change', { bubbles: true }));
  document.querySelector('#settings-quick-custom').value = 'gpt-5.6-terra';
  document.querySelector('#settings-deep-model').value = 'custom';
  document.querySelector('#settings-deep-model').dispatchEvent(new window.Event('change', { bubbles: true }));
  document.querySelector('#settings-deep-custom').value = 'gpt-5.6';
  document.querySelector('#model-settings-form').dispatchEvent(new window.Event('submit', { bubbles: true, cancelable: true }));
  await settle();
  await settle();

  assert.equal(document.querySelector('#start-run').disabled, false);
  assert.equal(document.querySelector('#model-name').textContent, 'gpt-5.6');
  assert.equal(document.querySelector('#service-state').textContent, '设置就绪');
});

test('legacy account disable confirms before revisioned PATCH and preserves permissions', async context => {
  const member = { object_id: 'bbbbbbbb-2222-4222-8222-bbbbbbbbbbbb', email: 'guest@example.com',
    name: 'Guest User', is_admin: false, disabled: false,
    permissions: { research: false, assistant: true, market: false } };
  const { document, window, calls } = await setup(context, {
    fetchOverride: (url, options = {}) => {
      if (url === '/api/me') return jsonResponse({ user: { email: 'owner@example.com', is_admin: true }, csrf_token: 'email-csrf-1' });
      if (url === '/api/settings/catalog') return jsonResponse(emailCatalog);
      if (url === '/api/settings') return jsonResponse(emailSettings);
      if (url === '/api/budget') return jsonResponse(emailBudget);
      if (url === '/api/runs') return jsonResponse({ runs: [] });
      if (url === '/api/admin/users') return jsonResponse({ users: [member] });
      if (url === '/api/admin/accounts') return jsonResponse({ accounts: [member], version: 'registry-v1' });
      if (url === '/api/admin/approved-origins') return jsonResponse({ version: 'origins-v1', approved_origins: [] });
      if (url === `/api/admin/accounts/${member.object_id}` && options.method === 'PATCH') return jsonResponse({ account: { ...member, disabled: true }, version: 'registry-v2' });
    },
  });
  const confirmations = [];
  let accepted = false;
  window.confirm = message => { confirmations.push(message); return accepted; };
  document.querySelector('#account-panel-toggle').click(); await settle();
  const disable = document.querySelector('#user-list .danger-button');
  disable.click(); await settle();
  assert.equal(confirmations.length, 1);
  assert.match(confirmations[0], /guest@example.com/);
  assert.equal(calls.some(call => call.url.endsWith('/disable') || call.options.method === 'PATCH'), false);
  accepted = true;
  disable.click(); await settle();
  const writes = calls.filter(call => call.options.method === 'PATCH');
  assert.equal(writes.length, 1);
  assert.equal(writes[0].url, `/api/admin/accounts/${member.object_id}`);
  assert.equal(writes[0].options.headers['If-Match'], 'registry-v1');
  assert.equal(writes[0].options.headers['X-Workbench-Token'], 'email-csrf-1');
  assert.deepEqual(JSON.parse(writes[0].options.body), { disabled: true, permissions: member.permissions });
  assert.equal(calls.some(call => call.url.endsWith('/disable')), false);
});

test('admin invitation result offers a same-origin fragment link and revoke clears it', async context => {
  const revoked = [];
  const { document, window, calls } = await setup(context, {
    fetchOverride: (url, options = {}) => {
      if (url === '/api/me') return jsonResponse({ user: { object_id: 'owner-1', email: 'owner@example.com', name: 'Owner User', is_admin: true }, csrf_token: 'email-csrf-1' });
      if (url === '/api/settings/catalog') return jsonResponse(emailCatalog);
      if (url === '/api/settings') return jsonResponse(emailSettings);
      if (url === '/api/budget') return jsonResponse(emailBudget);
      if (url === '/api/runs') return jsonResponse({ runs: [] });
      if (url === '/api/admin/accounts') return jsonResponse({ version: 'accounts-v1', accounts: [{ object_id: 'guest-1', email: 'guest+qa&ops@example.com', name: 'Guest User', is_admin: false, disabled: false }] });
      if (url === '/api/admin/approved-origins') return jsonResponse({ version: 'origins-v1', approved_origins: ['https://api.openai.com'] });
      if (url === '/api/admin/invitations' && options.method === 'POST') {
        return jsonResponse({
          token_id: 'invite-token-1',
          email: 'guest+qa&ops@example.com',
          expires_at: 1,
          fragment: '/auth/redeem#token=invite-token-secret&email=guest%2Bqa%26ops%40example.com',
        });
      }
      if (url === '/api/admin/invitations/invite-token-1' && options.method === 'DELETE') {
        revoked.push(url);
        return jsonResponse({ revoked: true });
      }
    },
  });

  document.querySelector('#account-panel-toggle').click();
  await settle();
  document.querySelector('#invite-email').value = 'guest+qa&ops@example.com';
  document.querySelector('#invite-form').dispatchEvent(new window.Event('submit', { bubbles: true, cancelable: true }));
  await settle();
  await settle();

  const result = document.querySelector('#invite-result');
  const link = result.querySelector('a');
  const revoke = [...result.querySelectorAll('button')].find(button => button.textContent.includes('撤销'));
  assert.ok(link);
  assert.equal(link.href, 'http://127.0.0.1:8765/auth/redeem#token=invite-token-secret&email=guest%2Bqa%26ops%40example.com');
  assert.match(link.textContent, /guest\+qa&ops@example\.com/);
  assert.ok(revoke);
  revoke.click();
  await settle();
  assert.deepEqual(revoked, ['/api/admin/invitations/invite-token-1']);
  assert.equal(document.querySelector('#invite-result').textContent.trim(), '');
  assert.equal(calls.some(call => call.url === '/api/admin/invitations/invite-token-1' && call.options.method === 'DELETE'), true);
});

test('closing the account dialog clears pending invitation secrets and late responses cannot repopulate it', async context => {
  let releaseInvite;
  const { document, window } = await setup(context, {
    fetchOverride: (url, options = {}) => {
      if (url === '/api/me') return jsonResponse({ user: { object_id: 'owner-1', email: 'owner@example.com', name: 'Owner User', is_admin: true }, csrf_token: 'email-csrf-1' });
      if (url === '/api/settings/catalog') return jsonResponse(emailCatalog);
      if (url === '/api/settings') return jsonResponse(emailSettings);
      if (url === '/api/budget') return jsonResponse(emailBudget);
      if (url === '/api/runs') return jsonResponse({ runs: [] });
      if (url === '/api/admin/accounts') return jsonResponse({ version: 'accounts-v1', accounts: [] });
      if (url === '/api/admin/approved-origins') return jsonResponse({ version: 'origins-v1', approved_origins: [] });
      if (url === '/api/admin/invitations' && options.method === 'POST') return new Promise(resolve => { releaseInvite = resolve; });
    },
  });

  document.querySelector('#account-panel-toggle').click();
  await settle();
  document.querySelector('#invite-email').value = 'guest@example.com';
  document.querySelector('#invite-form').dispatchEvent(new window.Event('submit', { bubbles: true, cancelable: true }));
  document.querySelector('#account-dialog').dispatchEvent(new window.Event('cancel', { bubbles: false, cancelable: true }));
  await settle();
  assert.equal(document.querySelector('#invite-email').value, '');
  releaseInvite(jsonResponse({ token_id: 'late-token', email: 'guest@example.com', expires_at: 1, fragment: '/auth/redeem#token=late&email=guest%40example.com' }));
  await settle();
  await settle();
  assert.equal(document.querySelector('#invite-result').textContent.trim(), '');
});

test('stale admin users and origin responses from a closed dialog cannot win over a newer open', async context => {
  const adminLoads = [];
  const { document, window } = await setup(context, {
    fetchOverride: (url, options = {}) => {
      if (url === '/api/me') return jsonResponse({ user: { object_id: 'owner-1', email: 'owner@example.com', name: 'Owner User', is_admin: true }, csrf_token: 'email-csrf-1' });
      if (url === '/api/settings/catalog') return jsonResponse(emailCatalog);
      if (url === '/api/settings') return jsonResponse(emailSettings);
      if (url === '/api/budget') return jsonResponse(emailBudget);
      if (url === '/api/runs') return jsonResponse({ runs: [] });
      if (url === '/api/admin/accounts') {
        const entry = {};
        adminLoads.push(entry);
        return new Promise(resolve => { entry.users = resolve; });
      }
      if (url === '/api/admin/approved-origins') {
        const entry = adminLoads.at(-1);
        return new Promise(resolve => { entry.origins = resolve; });
      }
    },
  });

  document.querySelector('#account-panel-toggle').click();
  await settle();
  assert.equal(adminLoads.length, 1);

  document.querySelector('#account-dialog').dispatchEvent(new window.Event('cancel', { bubbles: false, cancelable: true }));
  await settle();

  document.querySelector('#account-panel-toggle').click();
  await settle();
  assert.equal(adminLoads.length, 2);

  adminLoads[0].users(jsonResponse({ version: 'accounts-stale', accounts: [{ object_id: 'guest-stale', email: 'stale@example.com', name: 'Stale User', is_admin: false, disabled: false }] }));
  adminLoads[0].origins(jsonResponse({ version: 'origins-stale', approved_origins: ['https://stale.example.com'] }));
  await settle();
  await settle();

  assert.equal(document.querySelector('#users-count').textContent, '0');
  assert.equal(document.querySelector('#approved-origins').value, '');

  adminLoads[1].users(jsonResponse({ version: 'accounts-current', accounts: [{ object_id: 'guest-current', email: 'current@example.com', name: 'Current User', is_admin: false, disabled: false }] }));
  adminLoads[1].origins(jsonResponse({ version: 'origins-current', approved_origins: ['https://api.openai.com'] }));
  await settle();
  await settle();

  assert.equal(document.querySelector('#users-count').textContent, '1');
  assert.match(document.querySelector('#user-list').textContent, /Current User/);
  assert.equal(document.querySelector('#user-list').textContent.includes('Stale User'), false);
  assert.equal(document.querySelector('#approved-origins').value, 'https://api.openai.com');
});

test('logout clears dialog secrets and leaves pending controls disabled after session teardown', async context => {
  let releaseLogout;
  const { document, window } = await setup(context, {
    fetchOverride: (url, options = {}) => {
      if (url === '/api/me') return jsonResponse({ user: { object_id: 'owner-1', email: 'owner@example.com', name: 'Owner User', is_admin: true }, csrf_token: 'email-csrf-1' });
      if (url === '/api/settings/catalog') return jsonResponse(emailCatalog);
      if (url === '/api/settings') return jsonResponse(emailSettings);
      if (url === '/api/budget') return jsonResponse(emailBudget);
      if (url === '/api/runs') return jsonResponse({ runs: [] });
      if (url === '/api/admin/accounts') return jsonResponse({ version: 'accounts-v1', accounts: [] });
      if (url === '/api/admin/approved-origins') return jsonResponse({ version: 'origins-v1', approved_origins: [] });
      if (url === '/auth/logout' && options.method === 'POST') return new Promise(resolve => { releaseLogout = resolve; });
    },
  });

  document.querySelector('#account-panel-toggle').click();
  await settle();
  document.querySelector('#invite-email').value = 'guest@example.com';
  document.querySelector('#account-current-password').value = 'old';
  document.querySelector('#account-new-password').value = 'new';
  document.querySelector('#account-sign-out').click();
  releaseLogout(jsonResponse({ signed_out: true }));
  await settle();
  await settle();

  assert.equal(document.querySelector('#invite-email').value, '');
  assert.equal(document.querySelector('#account-current-password').value, '');
  assert.equal(document.querySelector('#account-new-password').value, '');
  assert.equal(document.querySelector('#start-run').disabled, true);
});

test('auth redeem mount strips fragment immediately, preserves plus and ampersand email, and sanitizes errors', async context => {
  const html = await readFile(new URL('../tradingagents/workbench/static/index.html', import.meta.url), 'utf8');
  const authDoc = new JSDOM(html.replace('<body>', '<body class="auth-shell"><main class="auth-card"><form id="email-redeem-form" class="auth-form" method="post" action="/auth/redeem" novalidate><p id="auth-message" class="error-message" role="alert" hidden></p><p id="auth-fragment-status" class="auth-copy" role="status">正在验证链接…</p><label for="password">新密码<input id="password" name="password" type="password"></label><button id="email-redeem-submit" type="submit">提交</button></form></main>'), {
    url: 'http://127.0.0.1:8765/auth/redeem#token=test-token&email=guest%2Bqa%26ops%40example.com', pretendToBeVisual: true,
  });
  const previousDocument = globalThis.document;
  globalThis.document = authDoc.window.document;
  context.after(() => { globalThis.document = previousDocument; authDoc.window.close(); });
  authDoc.window.fetch = async () => ({ ok: false, status: 409, json: async () => ({ detail: 'private trace' }) });
  application ??= await import(new URL('app.js', staticUrl));
  const { mountAuthPage } = await import(new URL('account-ui.js', staticUrl));
  mountAuthPage(authDoc.window.document);

  assert.equal(authDoc.window.location.hash, '');
  assert.match(authDoc.window.document.querySelector('#auth-fragment-status').textContent, /guest\+qa&ops@example\.com/);
  authDoc.window.document.querySelector('#password').value = 'new-password';
  authDoc.window.document.querySelector('#email-redeem-form').dispatchEvent(new authDoc.window.Event('submit', { bubbles: true, cancelable: true }));
  await settle();
  await settle();
  assert.equal(authDoc.window.document.querySelector('#password').value, '');
  assert.equal(authDoc.window.document.body.textContent.includes('private trace'), false);
  assert.match(authDoc.window.document.querySelector('#auth-message').textContent, /状态已变化|刷新/);
});

test('owner budget refresh uses fresh snapshots on explicit refresh and account open', async context => {
  let budgetReads = 0;
  const budgets = [
    { ...emailBudget, available_microusd: 298250000, reset_at: '2026-10-01T00:00:00Z' },
    { ...emailBudget, available_microusd: 250000000, reserved_microusd: 48250000, reset_at: '2026-10-01T00:00:00Z' },
    { ...emailBudget, available_microusd: 200000000, spent_microusd: 50000000, reset_at: '2026-10-01T00:00:00Z' },
  ];
  const { document } = await setup(context, {
    fetchOverride: (url, options = {}) => {
      if (url === '/api/me') return jsonResponse({ user: { object_id: 'owner-1', email: 'owner@example.com', name: 'Owner User', is_admin: true }, csrf_token: 'email-csrf-1' });
      if (url === '/api/settings/catalog') return jsonResponse(emailCatalog);
      if (url === '/api/settings') return jsonResponse(emailSettings);
      if (url === '/api/budget') return jsonResponse(budgets[Math.min(budgetReads++, budgets.length - 1)]);
      if (url === '/api/runs') return jsonResponse({ runs: [] });
      if (url === '/api/admin/accounts') return jsonResponse({ version: 'accounts-v1', accounts: [] });
      if (url === '/api/admin/approved-origins') return jsonResponse({ version: 'origins-v1', approved_origins: [] });
    },
  });

  assert.match(document.querySelector('#budget-available').textContent, /\$298\.25/);
  document.querySelector('#refresh').click();
  await settle();
  await settle();
  assert.match(document.querySelector('#budget-available').textContent, /\$250\.00/);
  document.querySelector('#account-panel-toggle').click();
  await settle();
  assert.match(document.querySelector('#budget-available').textContent, /\$200\.00/);
});

test('late config and history responses after logout cannot restore the prior email session state', async context => {
  let releaseConfig;
  let releaseHistory;
  let loggedOut = false;
  const { document } = await setup(context, {
    fetchOverride: (url, options = {}) => {
      if (url === '/api/me') return jsonResponse({ user: { object_id: 'owner-1', email: 'owner@example.com', name: 'Owner User', is_admin: true }, csrf_token: 'email-csrf-1' });
      if (url === '/api/settings/catalog') return jsonResponse(emailCatalog);
      if (url === '/api/settings') {
        if (loggedOut) return new Promise(resolve => { releaseConfig = resolve; });
        return jsonResponse(emailSettings);
      }
      if (url === '/api/budget') return jsonResponse(emailBudget);
      if (url === '/api/runs') {
        if (loggedOut && options.method !== 'POST') return new Promise(resolve => { releaseHistory = resolve; });
        return jsonResponse({ runs: [run] });
      }
      if (url === '/auth/logout' && options.method === 'POST') {
        loggedOut = true;
        return jsonResponse({ signed_out: true });
      }
    },
  });

  document.querySelector('#refresh').click();
  document.querySelector('#account-panel-toggle').click();
  await settle();
  document.querySelector('#account-sign-out').click();
  await settle();
  await settle();
  releaseConfig?.(jsonResponse(emailSettings));
  releaseHistory?.(jsonResponse({ runs: [run] }));
  await settle();
  await settle();

  assert.equal(document.querySelector('#account-name').textContent, '');
  assert.equal(document.querySelector('#start-run').disabled, true);
  assert.match(document.querySelector('#run-title').textContent, /尚未选择任务/);
});

test('auth login mount guards double submit and pagehide prevents late restore', async context => {
  const html = `<!doctype html><html lang="zh-CN"><body class="auth-shell"><main class="auth-card"><form id="email-login-form" class="auth-form" method="post" action="/auth/login" novalidate><p id="auth-message" class="error-message" role="alert" hidden></p><label for="email">邮箱<input id="email" name="email" type="email"></label><label for="password">密码<input id="password" name="password" type="password"></label><button id="email-login-submit" type="submit">登录</button></form></main></body></html>`;
  const authDoc = new JSDOM(html, { url: 'http://127.0.0.1:8765/auth/login', pretendToBeVisual: true });
  const previousDocument = globalThis.document;
  globalThis.document = authDoc.window.document;
  context.after(() => { globalThis.document = previousDocument; authDoc.window.close(); });
  let submits = 0;
  let release;
  authDoc.window.fetch = async () => {
    submits += 1;
    return new Promise(resolve => { release = resolve; });
  };
  const { mountAuthPage } = await import(new URL('account-ui.js', staticUrl));
  mountAuthPage(authDoc.window.document);

  authDoc.window.document.querySelector('#email').value = 'owner@example.com';
  authDoc.window.document.querySelector('#password').value = 'secret';
  const submitEvent = new authDoc.window.Event('submit', { bubbles: true, cancelable: true });
  authDoc.window.document.querySelector('#email-login-form').dispatchEvent(submitEvent);
  authDoc.window.document.querySelector('#email-login-form').dispatchEvent(new authDoc.window.Event('submit', { bubbles: true, cancelable: true }));
  assert.equal(submits, 1);
  authDoc.window.dispatchEvent(new authDoc.window.Event('pagehide'));
  release({ ok: false, status: 409, json: async () => ({ detail: 'private failure' }) });
  await settle();
  await settle();
  assert.equal(authDoc.window.document.querySelector('#password').value, '');
  assert.equal(authDoc.window.document.querySelector('#auth-message').textContent, '');
});

test('auth register sends exact payloads, excludes confirmation, and uses manual resend cooldown', async context => {
  const html = `<!doctype html><html lang="zh-CN"><body class="auth-shell"><main class="auth-card" data-registration-available="true"><p id="auth-message" class="error-message" role="alert" hidden></p><p id="register-status" role="status"></p><form id="email-register-code-form" method="post" action="/auth/register/code"><input id="email" type="email"><button id="register-send-code" type="submit">发送验证码</button></form><form id="email-register-form" method="post" action="/auth/register"><input id="code"><input id="password" type="password"><input id="password-confirm" type="password"><button id="email-register-submit" type="submit">注册账户</button></form></main></body></html>`;
  const authDoc = new JSDOM(html, { url: 'http://127.0.0.1:8765/auth/register', pretendToBeVisual: true });
  const previousDocument = globalThis.document;
  globalThis.document = authDoc.window.document;
  context.after(() => { globalThis.document = previousDocument; authDoc.window.close(); });
  const calls = [];
  authDoc.window.fetch = async (url, options) => {
    calls.push({ url, options });
    return url.endsWith('/code')
      ? { ok: true, status: 200, json: async () => ({ accepted: true, expires_in: 600, retry_after: 60 }) }
      : { ok: true, status: 200, json: async () => ({ user: { email: 'new@example.com' }, csrf_token: 'csrf' }) };
  };
  const { mountAuthPage } = await import(new URL('account-ui.js', staticUrl));
  mountAuthPage(authDoc.window.document);

  authDoc.window.document.querySelector('#email').value = ' new@example.com ';
  authDoc.window.document.querySelector('#email-register-code-form').dispatchEvent(new authDoc.window.Event('submit', { bubbles: true, cancelable: true }));
  authDoc.window.document.querySelector('#email-register-code-form').dispatchEvent(new authDoc.window.Event('submit', { bubbles: true, cancelable: true }));
  assert.equal(authDoc.window.document.querySelector('#email').disabled, true);
  assert.equal(authDoc.window.document.querySelector('#email-register-submit').disabled, true);
  await settle();
  assert.equal(calls.filter(call => call.url === '/auth/register/code').length, 1);
  assert.deepEqual(JSON.parse(calls[0].options.body), { email: 'new@example.com' });
  assert.equal(authDoc.window.document.querySelector('#register-send-code').disabled, true);

  authDoc.window.document.querySelector('#code').value = '123456';
  authDoc.window.document.querySelector('#password').value = 'fixture-password-2026';
  authDoc.window.document.querySelector('#password-confirm').value = 'fixture-password-2026';
  authDoc.window.document.querySelector('#email-register-form').dispatchEvent(new authDoc.window.Event('submit', { bubbles: true, cancelable: true }));
  await settle();
  const registerCall = calls.find(call => call.url === '/auth/register');
  assert.deepEqual(JSON.parse(registerCall.options.body), { email: 'new@example.com', code: '123456', password: 'fixture-password-2026' });
  assert.equal(JSON.parse(registerCall.options.body).password_confirm, undefined);
  assert.equal(authDoc.window.document.querySelector('#password').value, '');
  assert.equal(authDoc.window.document.querySelector('#code').value, '');
});

test('auth register failure clears secrets, applies 429 cooldown, and pagehide ignores late responses', async context => {
  const html = `<!doctype html><html lang="zh-CN"><body class="auth-shell"><main class="auth-card" data-registration-available="true"><p id="auth-message" class="error-message" role="alert" hidden></p><p id="register-status" role="status"></p><form id="email-register-code-form" method="post" action="/auth/register/code"><input id="email" type="email"><button id="register-send-code" type="submit">发送验证码</button></form><form id="email-register-form" method="post" action="/auth/register"><input id="code"><input id="password" type="password"><input id="password-confirm" type="password"><button id="email-register-submit" type="submit">注册账户</button></form></main></body></html>`;
  const authDoc = new JSDOM(html, { url: 'http://127.0.0.1:8765/auth/register', pretendToBeVisual: true });
  const previousDocument = globalThis.document;
  globalThis.document = authDoc.window.document;
  context.after(() => { globalThis.document = previousDocument; authDoc.window.close(); });
  let release;
  const calls = [];
  authDoc.window.fetch = async (url, options) => {
    calls.push({ url, options });
    if (url.endsWith('/code')) return { ok: false, status: 429, headers: { get: name => name.toLowerCase() === 'retry-after' ? '60' : null }, json: async () => ({ detail: 'private rate detail' }) };
    return new Promise(resolve => { release = resolve; });
  };
  const { mountAuthPage } = await import(new URL('account-ui.js', staticUrl));
  mountAuthPage(authDoc.window.document);

  authDoc.window.document.querySelector('#email').value = 'new@example.com';
  authDoc.window.document.querySelector('#email-register-code-form').dispatchEvent(new authDoc.window.Event('submit', { bubbles: true, cancelable: true }));
  await settle();
  assert.equal(authDoc.window.document.querySelector('#register-send-code').disabled, true);
  assert.equal(authDoc.window.document.body.textContent.includes('private rate detail'), false);

  authDoc.window.document.querySelector('#code').value = '123456';
  authDoc.window.document.querySelector('#password').value = 'fixture-password-2026';
  authDoc.window.document.querySelector('#password-confirm').value = 'fixture-password-2026';
  authDoc.window.document.querySelector('#email-register-form').dispatchEvent(new authDoc.window.Event('submit', { bubbles: true, cancelable: true }));
  release({ ok: false, status: 401, json: async () => ({ detail: 'private auth detail' }) });
  await settle();
  assert.match(authDoc.window.document.querySelector('#auth-message').textContent, /验证码错误或已失效/);
  assert.equal(authDoc.window.document.querySelector('#password').value, '');
  authDoc.window.document.querySelector('#code').value = '123456';
  authDoc.window.document.querySelector('#password').value = 'fixture-password-2026';
  authDoc.window.document.querySelector('#password-confirm').value = 'fixture-password-2026';
  authDoc.window.document.querySelector('#email-register-form').dispatchEvent(new authDoc.window.Event('submit', { bubbles: true, cancelable: true }));
  authDoc.window.dispatchEvent(new authDoc.window.Event('pagehide'));
  release({ ok: false, status: 401, json: async () => ({ detail: 'private auth detail' }) });
  await settle();
  await settle();
  assert.equal(authDoc.window.document.querySelector('#password').value, '');
  assert.equal(authDoc.window.document.querySelector('#code').value, '');
  assert.equal(authDoc.window.document.querySelector('#auth-message').textContent, '');
  assert.equal(calls.filter(call => call.url === '/auth/register/code').length, 1);
  assert.equal(calls.filter(call => call.url === '/auth/register').length, 2);
});

test('budget exhausted terminal runs surface the budget error while keeping partial reports visible', async context => {
  const exhausted = {
    ...run,
    status: 'failed',
    error: 'The workbench model budget is exhausted. Available reports are partial.',
    error_code: 'budget_exhausted',
    result: { decision: null, reports: { market_report: 'Partial research.' }, stats: {}, connectivity: [] },
    events: [
      { kind: 'report', payload: { section: 'market_report', content: 'Partial research.' } },
      { kind: 'error', payload: { code: 'budget_exhausted', message: 'Hosted worker provider budget is exhausted.' } },
    ],
  };
  const { document } = await setup(context, { runs: [exhausted] });
  assert.match(document.querySelector('#workspace-error').textContent, /budget/i);
  assert.match(document.querySelector('#report-articles').textContent, /Partial research\./);
  assert.equal(document.querySelector('#download-markdown').disabled, false);
});

test('initial form follows config and shows no fabricated reports, usage or connectivity', { skip: !application }, async context => {
  const { document } = await setup(context);
  assert.equal(document.querySelector('#ticker').value, 'MSFT');
  assert.equal(document.querySelector('#model-name').textContent, 'gpt-5.6-sol');
  assert.equal(document.querySelector('#asset_type option[value=crypto]').disabled, true);
  assert.equal(document.querySelector('#stat-input_tokens').textContent, '--');
  assert.equal(document.querySelectorAll('.report-article').length, 0);
  assert.equal(document.querySelectorAll('#provider-rows tr').length, 0);
  assert.equal(document.querySelector('#start-run').disabled, false);
});

test('date input maximum and default submission agree with UTC after local midnight', { skip: !application }, async context => {
  context.mock.timers.enable({ apis: ['Date'], now: new Date('2026-09-08T16:30:00Z') });
  context.mock.method(Date.prototype, 'getFullYear', () => 2026);
  context.mock.method(Date.prototype, 'getMonth', () => 8);
  context.mock.method(Date.prototype, 'getDate', () => 9);
  const { document, window, calls } = await setup(context);
  assert.equal(document.querySelector('#analysis_date').max, '2026-09-08');
  assert.equal(document.querySelector('#analysis_date').value, '2026-09-08');
  document.querySelector('#config-form').dispatchEvent(new window.Event('submit', { cancelable: true }));
  await settle();
  const submitted = calls.find(call => call.options.method === 'POST');
  assert.equal(JSON.parse(submitted.options.body).analysis_date, '2026-09-08');
});

test('submitting draft sends normalized config, explicit chain and CSRF without a model field', { skip: !application }, async context => {
  const { document, window, calls } = await setup(context);
  document.querySelector('#ticker').value = ' goog ';
  document.querySelector('#vendor-news_data').value = 'alpha_vantage,yfinance';
  assert.equal(document.querySelector('#vendor-macro_data').value, 'fred');
  document.querySelector('#vendor-macro_data').value = 'alpha_vantage';
  document.querySelector('#config-form').dispatchEvent(new window.Event('submit', { bubbles: true, cancelable: true }));
  await settle();
  const call = calls.find(item => item.url === '/api/runs' && item.options.method === 'POST');
  assert.ok(call);
  const request = JSON.parse(call.options.body);
  assert.equal(request.ticker, 'GOOG');
  assert.equal(request.data_vendors.news_data, 'alpha_vantage,yfinance');
  assert.equal(request.data_vendors.macro_data, 'alpha_vantage');
  assert.equal(request.model, undefined);
  assert.equal(call.options.headers['X-Workbench-Token'], 'test-csrf-token');
  assert.match(document.querySelector('#run-title').textContent, /GOOG/);
});

test('connectivity action overrides mode without discarding the rest of the draft', { skip: !application }, async context => {
  const { document, calls } = await setup(context);
  document.querySelector('#risk_rounds').value = '3';
  document.querySelector('#test-connectivity').click();
  await settle();
  const request = JSON.parse(calls.find(item => item.options.method === 'POST').options.body);
  assert.equal(request.mode, 'connectivity');
  assert.equal(request.risk_rounds, 3);
});

test('ETF selection submits the fund asset type', async context => {
  const { document, window, calls } = await setup(context);
  assert.equal(document.querySelector('#asset_type option[value=etf]').disabled, false);
  document.querySelector('#vendor-fundamental_data').value = 'alpha_vantage';
  document.querySelector('#asset_type').value = 'etf';
  document.querySelector('#asset_type').dispatchEvent(new window.Event('change', { bubbles: true }));
  assert.equal(document.querySelector('#vendor-fundamental_data').value, 'yfinance');
  assert.equal(document.querySelector('#vendor-fundamental_data').disabled, true);
  document.querySelector('#ticker').value = 'VOO';
  document.querySelector('#config-form').dispatchEvent(new window.Event('submit', { cancelable: true }));
  await settle();
  const request = JSON.parse(calls.find(item => item.options.method === 'POST').options.body);
  assert.equal(request.asset_type, 'etf');
  assert.equal(request.ticker, 'VOO');
});

for (const status of ['running', 'failed', 'stopped', 'succeeded']) {
  test(`analysis data tab retains preflight rows for ${status} history`, async context => {
    const rows = [
      { provider: 'yfinance', status: 'ok', detail: 'Native prices available.', elapsed_seconds: 0.2 },
      { provider: 'fred', status: 'error', detail: '<script>provider error</script>', elapsed_seconds: 1 },
    ];
    const record = { ...run, status, events: [
      { kind: 'connectivity', timestamp: '2026-09-08T08:00:01Z', payload: { connectivity: rows } },
    ], result: status === 'succeeded' ? { decision: 'Hold', reports: {}, stats: {}, connectivity: rows } : null };
    const { document } = await setup(context, { runs: [record] });
    document.querySelector('[data-view="connectivity"]').click();
    assert.equal(document.querySelector('#provider-table').hidden, false);
    assert.equal(document.querySelectorAll('#provider-rows tr').length, 2);
    assert.match(document.querySelector('#provider-rows').textContent, /Yahoo Finance/);
    assert.match(document.querySelector('#provider-rows').textContent, /FRED/);
    assert.equal(document.querySelector('#provider-rows script'), null);
    assert.equal(document.querySelector('#download-markdown').disabled, false);
    assert.match(document.querySelector('#event-list').textContent, /\u8fde\u901a/);
  });
}

test('stop targets selected run; rerun preserves the historical request instead of the edited form', { skip: !application }, async context => {
  const { document, calls } = await setup(context, { runs: [run] });
  document.querySelector('#stop-run').click();
  await settle();
  const stopped = calls.find(item => item.url.endsWith('/stop'));
  assert.equal(stopped.url, `/api/runs/${run.id}/stop`);
  assert.equal(stopped.options.headers['Content-Type'], 'application/json');
  assert.equal(stopped.options.body, '{}');
  assert.equal(document.querySelector('#stop-run').disabled, true);
  document.querySelector('#ticker').value = 'AAPL';
  document.querySelector('#rerun').click();
  await settle();
  const submitted = calls.filter(item => item.url === '/api/runs' && item.options.method === 'POST');
  assert.equal(JSON.parse(submitted.at(-1).options.body).ticker, 'MSFT');
});

test('all final report sections render Markdown safely, including unknown section names', { skip: !application }, async context => {
  const completed = { ...run, status: 'succeeded', result: {
    decision: '<img src=x onerror=alert(1)>', reports: {
      market_report: '# Market\n\n**Evidence**\n<script>globalThis.pwned=1</script>\n<img src="https://example.com/tracker" onerror="alert(1)">\n\n[bad](javascript:alert(1))',
      custom_section: 'custom report',
    }, stats: { input_tokens: 0, output_tokens: 25, llm_calls: 2, tool_calls: 1 },
    connectivity: [{ provider: 'fred', status: 'unconfigured', detail: '<script>bad</script>', elapsed_seconds: 0 }],
  } };
  const { document } = await setup(context, { runs: [completed] });
  assert.equal(document.querySelectorAll('.report-article').length, 2);
  assert.equal(document.querySelector('.report-body h1').textContent, 'Market');
  assert.equal(document.querySelector('.report-body strong').textContent, 'Evidence');
  assert.equal(document.querySelectorAll('.report-body script, .report-body img, .report-body [onerror]').length, 0);
  assert.equal(document.querySelector('.report-body a')?.getAttribute('href'), null);
  assert.equal(document.querySelector('#stat-input_tokens').textContent, '0');
  assert.match(document.querySelector('#provider-rows').textContent, /<script>bad<\/script>/);
  assert.equal(document.querySelector('#provider-rows script'), null);
  assert.equal(document.querySelector('#download-json').disabled, false);
});

test('deployment not ready disables writes but retains history and report access', { skip: !application }, async context => {
  const { document } = await setup(context, { configuration: { ...config, deployment_ready: false }, runs: [{ ...run, status: 'stopped' }] });
  assert.equal(document.querySelector('#start-run').disabled, true);
  assert.equal(document.querySelector('#rerun').disabled, true);
  assert.match(document.querySelector('#run-title').textContent, /MSFT/);
});

test('history searches and reports errors without replacing saved report content', { skip: !application }, async context => {
  const { document, window } = await setup(context, { runs: [
    { ...run, status: 'succeeded' },
    { ...run, id: 'da02a113-3ee4-422a-902b-fc958a74620f', status: 'failed', request: { ...defaults, ticker: 'AAPL' } },
  ] });
  document.querySelector('#history-search').value = '失败';
  document.querySelector('#history-search').dispatchEvent(new window.Event('input', { bubbles: true }));
  assert.equal(document.querySelectorAll('.history-item').length, 1);
  assert.match(document.querySelector('.history-item').textContent, /AAPL/);
});

for (const status of ['running', 'reconciliation_required']) {
  test(`${status} run has separate polling timers; visibility and disposal clean up both`, { skip: !application }, async context => {
    const { document, window, timers, workbench } = await setup(context, { runs: [{ ...run, status }] });
    assert.deepEqual([...timers.values()].map(item => item.delay).sort((first, second) => first - second), [5000, 30000]);
    Object.defineProperty(document, 'hidden', { configurable: true, value: true });
    document.dispatchEvent(new window.Event('visibilitychange'));
    assert.equal(timers.size, 0);
    Object.defineProperty(document, 'hidden', { configurable: true, value: false });
    document.dispatchEvent(new window.Event('visibilitychange'));
    await settle();
    assert.equal(timers.size, 2);
    workbench.dispose();
    assert.equal(timers.size, 0);
  });
}

test('uncertain execution automatically refreshes the original run without write requests', { skip: !application }, async context => {
  let detail = { ...run, status: 'reconciliation_required', start_uncertain: true, execution_name: null,
    error: 'Hosted execution is unconfirmed. Reconcile before further action.' };
  const { document, calls, timers, advance } = await setup(context, { runs: [detail], fetchOverride: async url => {
    if (url === `/api/runs/${run.id}`) return { ok: true, status: 200, json: async () => structuredClone(detail) };
  } });
  assert.match(document.querySelector('.history-item').textContent, /\u5f85\u6838\u5bf9/);
  await advance(5000);
  assert.equal(calls.filter(call => call.url === `/api/runs/${run.id}`).length, 2);
  assert.match(document.querySelector('.history-item').textContent, /\u5f85\u6838\u5bf9/);
  detail = { ...detail, status: 'running', start_uncertain: false, execution_name: run.execution_name, error: null };
  await advance(5000);
  assert.match(document.querySelector('.history-item').textContent, /\u8fd0\u884c\u4e2d/);
  detail = { ...detail, status: 'succeeded', result: {
    decision: 'HOLD', reports: { market_report: 'Completed research' }, stats: {}, connectivity: [],
  } };
  await advance(5000);
  assert.match(document.querySelector('.history-item').textContent, /\u5df2\u5b8c\u6210/);
  assert.match(document.querySelector('#report-articles').textContent, /Completed research/);
  assert.deepEqual([...timers.values()].map(item => item.delay), [30000]);
  await advance(5000);
  assert.equal(calls.filter(call => call.url === `/api/runs/${run.id}`).length, 4);
  assert.ok(calls.every(call => call.options.method === 'GET'));
});

test('terminal run never schedules a single-run poll', { skip: !application }, async context => {
  const { timers } = await setup(context, { runs: [{ ...run, status: 'failed', error: 'safe error' }] });
  assert.deepEqual([...timers.values()].map(item => item.delay), [30000]);
});

for (const status of ['failed', 'stopped', 'succeeded']) {
  test(`history tick refreshes selected ${status} detail without replacing draft or selection`, { skip: !application }, async context => {
    let detail = { ...run, status };
    const runs = [detail];
    const { document, calls, timers, advance } = await setup(context, { runs, fetchOverride: async url => {
      if (url === `/api/runs/${run.id}`) return { ok: true, status: 200, json: async () => structuredClone(detail) };
    } });
    document.querySelector('#ticker').value = 'NVDA';
    document.querySelector('#vendor-news_data').value = 'alpha_vantage,yfinance';
    runs.unshift({ ...run, id: 'da02a113-3ee4-422a-902b-fc958a74620f', status: 'succeeded',
      created_at: '2026-09-08T09:00:00Z', request: { ...defaults, ticker: 'AAPL' } });
    detail = { ...detail, events: [{ kind: 'report', timestamp: '2026-09-08T09:00:00Z',
      payload: { section: 'market_report', content: 'Late partial evidence' } }] };
    await advance(30000);
    assert.match(document.querySelector('#report-articles').textContent, /Late partial evidence/);
    assert.match(document.querySelector('#run-title').textContent, /MSFT/);
    assert.equal(document.querySelector('#ticker').value, 'NVDA');
    assert.equal(document.querySelector('#vendor-news_data').value, 'alpha_vantage,yfinance');
    assert.equal(calls.filter(call => call.url === `/api/runs/${run.id}`).length, 2);
    assert.deepEqual([...timers.values()].map(item => item.delay), [30000]);
    await advance(30000);
    assert.equal(calls.filter(call => call.url === `/api/runs/${run.id}`).length, 3);
  });
}

test('accepted stop stays reserved in the browser until history detail confirms terminal execution', { skip: !application }, async context => {
  let detail = { ...run, status: 'stopped', stop_pending: true };
  const { document, advance } = await setup(context, { runs: [detail], fetchOverride: async url => {
    if (url === `/api/runs/${run.id}`) return { ok: true, status: 200, json: async () => structuredClone(detail) };
  } });
  assert.equal(document.querySelector('#start-run').disabled, true);
  assert.equal(document.querySelector('#rerun').disabled, true);
  assert.equal(document.querySelector('#stop-run').disabled, true);
  detail = { ...detail, stop_pending: false };
  await advance(30000);
  assert.equal(document.querySelector('#start-run').disabled, false);
});

test('five-second run polling cannot starve the independent 30-second history refresh', { skip: !application }, async context => {
  const { calls, advance } = await setup(context, { runs: [run] });
  await advance(30000);
  assert.equal(calls.filter(call => call.url === '/api/runs').length, 2);
  assert.equal(calls.filter(call => call.url === `/api/runs/${run.id}`).length, 7);
});

test('refresh rotates CSRF even with an unfinished draft and preserves that draft', { skip: !application }, async context => {
  let configReads = 0;
  const { document, window, calls } = await setup(context, { fetchOverride: async url => {
    if (url !== '/api/config') return;
    configReads += 1;
    return { ok: true, status: 200, json: async () => ({ ...config, csrf_token: configReads === 1 ? 'first' : 'rotated' }) };
  } });
  document.querySelector('#ticker').value = '';
  document.querySelector('#refresh').click();
  await settle();
  assert.equal(document.querySelector('#ticker').value, '');
  assert.equal(document.querySelector('#service-state').textContent, '部署就绪');
  document.querySelector('#ticker').value = 'NVDA';
  document.querySelector('#config-form').dispatchEvent(new window.Event('submit', { cancelable: true }));
  await settle();
  assert.equal(calls.find(call => call.options.method === 'POST').options.headers['X-Workbench-Token'], 'rotated');
});

test('failed config refresh disables new writes without discarding the draft or history', { skip: !application }, async context => {
  let configReads = 0;
  const { document } = await setup(context, { fetchOverride: async url => {
    if (url !== '/api/config') return;
    if (++configReads > 1) return { ok: false, status: 503 };
  } });
  document.querySelector('#ticker').value = 'NVDA';
  document.querySelector('#refresh').click();
  await settle();
  assert.equal(document.querySelector('#start-run').disabled, true);
  assert.equal(document.querySelector('#ticker').value, 'NVDA');
  assert.equal(document.querySelector('#service-state').textContent, '连接异常');
});

test('invalid form blocks submission and shows a field error', { skip: !application }, async context => {
  const { document, window, calls } = await setup(context);
  document.querySelectorAll('[name="analysts"]').forEach(input => { input.checked = false; });
  document.querySelector('#config-form').dispatchEvent(new window.Event('submit', { cancelable: true }));
  await settle();
  assert.equal(calls.filter(call => call.options.method === 'POST').length, 0);
  assert.equal(document.querySelector('#form-error').hidden, false);
  assert.match(document.querySelector('#form-error').textContent, /分析师/);
});

test('section navigation shows a single section and restores the full report', { skip: !application }, async context => {
  const completed = { ...run, status: 'succeeded', result: { decision: 'HOLD', reports: { market_report: 'market', news_report: 'news' }, stats: {}, connectivity: [] } };
  const { document } = await setup(context, { runs: [completed] });
  document.querySelector('.section-button[data-section="news_report"]').click();
  assert.equal(document.querySelector('.report-article[data-section="market_report"]').hidden, true);
  assert.equal(document.querySelector('.report-article[data-section="news_report"]').hidden, false);
  document.querySelector('.section-button[data-section="all"]').click();
  assert.equal(document.querySelectorAll('.report-article[hidden]').length, 0);
  assert.equal(document.querySelector('#decision').hidden, false);
});

test('exports fetch exact same-origin URLs and download the returned JSON and Markdown', { skip: !application }, async context => {
  const completed = { ...run, status: 'succeeded', result: { decision: 'HOLD', reports: { market_report: 'report' }, stats: {}, connectivity: [] } };
  const { document, window, calls, advance } = await setup(context, { runs: [completed], fetchOverride: async url => {
    if (!url.includes('/download?')) return;
    const contents = url.endsWith('json') ? '{"decision":"HOLD"}' : '# HOLD\n\nreport';
    return { ok: true, status: 200, blob: async () => new Blob([contents]) };
  } });
  const blobs = [];
  const downloads = [];
  const revoked = [];
  window.URL.createObjectURL = blob => { blobs.push(blob); return `blob:workbench/${blobs.length}`; };
  window.URL.revokeObjectURL = url => revoked.push(url);
  document.addEventListener('click', event => {
    if (event.target.tagName !== 'A') return;
    event.preventDefault();
    downloads.push({ name: event.target.download, href: event.target.href });
  });
  document.querySelector('#download-json').click();
  await settle();
  document.querySelector('#download-markdown').click();
  await settle();
  assert.deepEqual(calls.filter(call => call.url.includes('/download?')).map(call => [call.url, call.options.credentials]), [
    [`/api/runs/${run.id}/download?format=json`, 'same-origin'],
    [`/api/runs/${run.id}/download?format=markdown`, 'same-origin'],
  ]);
  assert.deepEqual(downloads.map(download => download.name), [`${run.id}.json`, `${run.id}.md`]);
  assert.equal(await blobs[0].text(), '{"decision":"HOLD"}');
  assert.equal(await blobs[1].text(), '# HOLD\n\nreport');
  await advance(1000);
  assert.equal(revoked.length, 2);
});

test('a late response for an old selection cannot overwrite the newly selected run', { skip: !application }, async context => {
  let hold = false;
  let release;
  const first = { ...run, status: 'succeeded' };
  const second = { ...run, id: 'da02a113-3ee4-422a-902b-fc958a74620f', status: 'succeeded', request: { ...defaults, ticker: 'AAPL' } };
  const { document } = await setup(context, { runs: [first, second], fetchOverride: async url => {
    if (hold && url === `/api/runs/${first.id}`) return new Promise(resolve => { release = () => resolve({ ok: true, status: 200, json: async () => first }); });
  } });
  hold = true;
  document.querySelector('#refresh').click();
  await settle();
  [...document.querySelectorAll('.history-item')].find(button => button.textContent.includes('AAPL')).click();
  await settle();
  release();
  await settle();
  assert.match(document.querySelector('#run-title').textContent, /AAPL/);
  assert.match(document.querySelector('#request-values').textContent, /da02a113/);
});

test('an unavailable history refresh keeps the selected report and offers an explicit error state', { skip: !application }, async context => {
  let failHistory = false;
  const completed = { ...run, status: 'succeeded', result: { decision: 'HOLD', reports: { market_report: 'persisted evidence' }, stats: {}, connectivity: [] } };
  const { document } = await setup(context, { runs: [completed], fetchOverride: async url => {
    if (failHistory && url === '/api/runs') return { ok: false, status: 503 };
  } });
  failHistory = true;
  document.querySelector('#refresh').click();
  await settle();
  assert.equal(document.querySelector('#history-error').hidden, false);
  assert.match(document.querySelector('.report-body').textContent, /persisted evidence/);
});

test('live locale switch updates rendered run chrome but never report contents or event payloads', async context => {
  const reportRun = { ...run, status: 'succeeded', result: { reports: { market_report: '新建研究 USER-REPORT' } }, events: [{ kind: 'error', timestamp: '2026-09-08T08:00:00Z', payload: { message: '新建研究 USER-EVENT' } }] };
  const { document, window } = await setup(context, { browserLanguage: 'en-US', runs: [reportRun] });
  const statusBefore = document.querySelector('#history-list .status').textContent;
  assert.notEqual(statusBefore, '已完成');
  assert.match(document.querySelector('#run-title').textContent, /Research analysis/);
  assert.equal(document.querySelector('#section-list button').textContent, 'All sections');
  assert.equal(document.querySelector('#phase-list .flow-label').textContent, 'Data preparation');
  assert.equal(document.querySelector('#agent-process-panel h2').textContent, 'Agent process');
  document.querySelector('#command-button').click();
  assert.equal(document.querySelector('#command-title').textContent, 'Global search');
  document.querySelector('#command-dialog [data-studio-close]').click();
  document.querySelector('#appearance-button').click();
  const selector = document.querySelector('#appearance-language');
  selector.value = 'zh'; selector.dispatchEvent(new window.Event('change', { bubbles: true }));
  assert.equal(document.querySelector('#history-list .status').textContent, '已完成');
  assert.match(document.querySelector('#report-articles').textContent, /新建研究 USER-REPORT/);
  assert.match(document.querySelector('#event-list pre').textContent, /新建研究 USER-EVENT/);
});

test('ETF data tooltip follows locale without changing its provider', async context => {
  const { document, window } = await setupHome(context, { browserLanguage: 'en-US' });
  const asset = document.querySelector('#asset_type');
  asset.value = 'etf';
  asset.dispatchEvent(new window.Event('change', { bubbles: true }));
  const vendor = document.querySelector('#vendor-fundamental_data');
  assert.equal(vendor.title, 'ETF fund data uses the native Yahoo Finance API');
  assert.equal(vendor.value, 'yfinance');
  assert.equal(vendor.disabled, true);
  document.querySelector('#appearance-button').click();
  const selector = document.querySelector('#appearance-language');
  selector.value = 'zh'; selector.dispatchEvent(new window.Event('change', { bubbles: true }));
  assert.equal(vendor.title, 'ETF 基金资料使用 Yahoo Finance 原生接口');
  assert.equal(vendor.value, 'yfinance');
});

test('service connection state changes language without retrying failed requests', async context => {
  const { document, window, calls } = await setupHome(context, {
    browserLanguage: 'en-US',
    fetchOverride: url => url === '/api/config' ? jsonResponse({ detail: 'offline' }, 503) : undefined,
  });
  assert.equal(document.querySelector('#service-state').textContent, 'Connection error');
  const before = calls.length;
  document.querySelector('#appearance-button').click();
  const selector = document.querySelector('#appearance-language');
  selector.value = 'zh'; selector.dispatchEvent(new window.Event('change', { bubbles: true }));
  assert.equal(document.querySelector('#service-state').textContent, '连接异常');
  assert.equal(document.querySelector('#workspace-home').hidden, false);
  assert.equal(calls.length, before);
});

test('public resource controls switch locale without changing resource profile values', async context => {
  const { document, window } = await setupHome(context, {
    browserLanguage: 'en-US', configuration: { ...config, public_mode: true, model: 'research-model' }, fetchOverride: publicResponse,
  });
  assert.equal(document.querySelector('#resource-heading').textContent, 'Azure and Foundry');
  assert.equal(document.querySelector('.initialization-section h3').textContent, 'On-demand initialization');
  assert.equal(document.querySelector('#initialization-status').textContent, 'No initialization records');
  assert.equal(document.querySelector('#profile-select').value, publicProfiles[0].id);
  document.querySelector('#appearance-button').click();
  const selector = document.querySelector('#appearance-language');
  selector.value = 'zh'; selector.dispatchEvent(new window.Event('change', { bubbles: true }));
  assert.equal(document.querySelector('#resource-heading').textContent, 'Azure 与 Foundry');
  assert.equal(document.querySelector('#profile-select').value, publicProfiles[0].id);
});

test('workspace vendor, usage labels and search count follow live language without changing run data', async context => {
  const observed = { ...run, events: [{ kind: 'usage', payload: { llm_calls: 3, tool_calls: 0, input_tokens: 0, output_tokens: 0 } }] };
  const { document, window, calls } = await setup(context, { runs: [observed], browserLanguage: 'en-US' });
  assert.match(document.querySelector('label[for="vendor-news_data"]').textContent, /News data/);
  assert.equal(document.querySelector('#stat-llm_calls').textContent, '3');
  assert.equal(document.querySelector('.activity-chart'), null);
  document.querySelector('#command-button').click();
  assert.match(document.querySelector('#command-count').textContent, /results · history and loaded reports/);
  const before = calls.length;
  const selector = document.querySelector('#appearance-language');
  selector.value = 'zh'; selector.dispatchEvent(new window.Event('change', { bubbles: true }));
  assert.match(document.querySelector('label[for="vendor-news_data"]').textContent, /新闻数据/);
  assert.equal(document.querySelector('#stat-llm_calls').textContent, '3');
  assert.match(document.querySelector('#command-count').textContent, /项结果 · 历史任务与已加载报告/);
  selector.value = 'en'; selector.dispatchEvent(new window.Event('change', { bubbles: true }));
  assert.equal(document.querySelector('#stat-input_tokens').textContent, '0');
  assert.equal(calls.length, before);
  assert.equal(observed.request.ticker, 'MSFT');
});