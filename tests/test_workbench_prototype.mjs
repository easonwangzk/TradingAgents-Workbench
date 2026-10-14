import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { createRequire } from 'node:module';

const require = createRequire(new URL('../tradingagents/workbench/static/package.json', import.meta.url));
const { JSDOM } = require('jsdom');
const prototypeUrl = new URL('../docs/prototypes/research-studio.html', import.meta.url);

async function setup(context, { reducedMotion = false, login = false, view = 'report', storedPalette, storageUnavailable = false } = {}) {
  const html = await readFile(prototypeUrl, 'utf8');
  const timers = new Map();
  const downloads = [];
  let timerId = 0;
  const dom = new JSDOM(html, {
    url: `http://localhost/prototype${login ? '?view=login' : view ? `?view=${view}` : ''}`, runScripts: 'dangerously', pretendToBeVisual: true,
    beforeParse(window) {
      if (storedPalette !== undefined) window.localStorage.setItem('tradingagents.prototype.palette', storedPalette);
      if (storageUnavailable) Object.defineProperty(window, 'localStorage', { get() { throw new window.DOMException('Storage unavailable', 'SecurityError'); } });
      window.matchMedia = query => ({ matches: query.includes('prefers-reduced-motion') && reducedMotion, addEventListener() {} });
      window.setTimeout = (task, delay) => { timers.set(++timerId, { task, delay }); return timerId; };
      window.clearTimeout = id => timers.delete(id);
      window.HTMLElement.prototype.scrollIntoView = () => {};
      window.HTMLDialogElement.prototype.showModal = function () { this.setAttribute('open', ''); };
      window.HTMLDialogElement.prototype.close = function () { this.removeAttribute('open'); };
      window.fetch = () => { throw new Error('Prototype must never call a network API'); };
      window.Blob = globalThis.Blob;
      window.URL.createObjectURL = blob => { downloads.push(blob); return `blob:prototype-${downloads.length}`; };
      window.URL.revokeObjectURL = () => {};
      window.HTMLAnchorElement.prototype.click = () => {};
    },
  });
  context.after(() => dom.window.close());
  const { document } = dom.window;
  const tick = () => {
    const scheduled = [...timers].find(([, timer]) => timer.delay === 1800);
    assert.ok(scheduled, 'A demo phase must be scheduled');
    timers.delete(scheduled[0]);
    scheduled[1].task();
  };
  return { window: dom.window, document, timers, tick, downloads };
}

test('palette choices update appearance without replacing the selected report and survive reload', async context => {
  const { window, document } = await setup(context);
  document.querySelector('[data-run="msft"]').click();
  document.querySelector('[data-report="risk"]').click();
  const report = document.querySelector('#report-body').textContent;
  document.querySelector('#appearance-button').click();
  const choices = [...document.querySelectorAll('[name="appearance-palette"]')];
  assert.equal(choices.length, 5, 'Appearance exposes all five palette swatches');
  const colors = new Set();
  for (const choice of choices) {
    choice.click();
    assert.equal(document.documentElement.dataset.palette, choice.value);
    assert.equal(document.querySelectorAll('[name="appearance-palette"]:checked').length, 1);
    colors.add(window.getComputedStyle(document.documentElement).getPropertyValue('--cp-accent').trim());
  }
  assert.equal(colors.size, 5, 'Every palette has a distinct accent');
  document.querySelector('[name="appearance-palette"][value="pine"]').click();
  const lightAccent = window.getComputedStyle(document.documentElement).getPropertyValue('--cp-accent');
  document.querySelector('[name="appearance-theme"][value="dark"]').click();
  assert.notEqual(window.getComputedStyle(document.documentElement).getPropertyValue('--cp-accent'), lightAccent);
  assert.equal(document.documentElement.dataset.palette, 'pine');
  assert.equal(document.querySelector('#report-body').textContent, report);
  assert.equal(document.querySelector('#ticker-title').textContent, 'MSFT');
  assert.equal(window.localStorage.length, 1);
  assert.equal(window.localStorage.getItem('tradingagents.prototype.palette'), 'pine');
  const reloaded = await setup(context, { login: true, storedPalette: window.localStorage.getItem('tradingagents.prototype.palette') });
  assert.equal(reloaded.document.documentElement.dataset.palette, 'pine');
  assert.equal(reloaded.document.querySelector('[name="appearance-palette"][value="pine"]').checked, true);
});

test('invalid or unavailable saved palettes fall back safely and live selection still works', async context => {
  const invalid = await setup(context, { storedPalette: 'unknown-color' });
  assert.equal(invalid.document.documentElement.dataset.palette, 'mono');
  assert.equal(invalid.document.querySelector('[name="appearance-palette"][value="mono"]').checked, true);
  const { document } = await setup(context, { login: true, storageUnavailable: true });
  assert.equal(document.documentElement.dataset.palette, 'mono');
  document.querySelector('#login-email').value = 'demo@tradingagents.test';
  document.querySelector('[name="appearance-palette"][value="coral"]').click();
  assert.equal(document.documentElement.dataset.palette, 'coral');
  assert.equal(document.querySelector('#login-email').value, 'demo@tradingagents.test');
  document.querySelector('#open-registration').click();
  assert.equal(document.documentElement.dataset.palette, 'coral');
  assert.equal(document.querySelector('#registration-panel').hidden, false);
});

test('registration deep link survives initialization and focuses the mismatched confirmation', async context => {
  const { window, document } = await setup(context, { view: 'register' });
  assert.equal(new URL(window.location.href).searchParams.get('view'), 'register');
  assert.equal(document.querySelector('#registration-panel').hidden, false);
  assert.equal(document.querySelector('.skip').getAttribute('href'), '#register-email');
  document.querySelector('#fill-demo-registration').click();
  document.querySelector('#register-confirm').value = 'Mismatch-2026';
  document.querySelector('#registration-form').dispatchEvent(new window.Event('submit', { bubbles: true, cancelable: true }));
  assert.equal(document.querySelector('#registration-panel').hidden, false);
  assert.equal(document.activeElement.id, 'register-confirm');
  assert.equal(document.activeElement.getAttribute('aria-invalid'), 'true');
  window.dispatchEvent(new window.Event('pagehide'));
  for (const id of ['register-code', 'register-password', 'register-confirm']) assert.equal(document.getElementById(id).value, '');
});

test('opening the standalone artifact starts at login without displaying sample reports', async context => {
  const { document } = await setup(context, { view: '' });
  assert.equal(document.querySelector('#login-screen').hidden, false);
  assert.equal(document.querySelector('.app').hidden, true);
});

test('registration validates demo code and password confirmation and clears secrets on exit', async context => {
  const { window, document } = await setup(context, { login: true });
  const launcher = document.querySelector('#open-registration');
  assert.ok(launcher, 'Login must offer registration');
  launcher.click();
  assert.equal(document.querySelector('#registration-panel').hidden, false);
  const submit = () => document.querySelector('#registration-form').dispatchEvent(new window.Event('submit', { bubbles: true, cancelable: true }));
  document.querySelector('#register-email').value = 'demo@tradingagents.test';
  document.querySelector('#register-send-code').click();
  assert.equal(document.querySelector('#register-send-code').disabled, true);
  document.querySelector('#register-code').value = '000000';
  document.querySelector('#register-password').value = 'Demo-Only-2026';
  document.querySelector('#register-confirm').value = 'Different-2026';
  submit();
  assert.equal(document.querySelector('#registration-error').hidden, false);
  assert.equal(document.querySelector('#registration-panel').hidden, false);
  document.querySelector('#register-code').value = '246810';
  document.querySelector('#register-password').value = 'Demo-Only-2026';
  document.querySelector('#register-confirm').value = 'Demo-Only-2026';
  submit();
  assert.equal(document.querySelector('#registration-panel').hidden, true);
  for (const id of ['register-code', 'register-password', 'register-confirm']) assert.equal(document.getElementById(id).value, '');
  assert.equal(window.localStorage.length, 0);
  assert.equal(window.sessionStorage.length, 0);
});

test('workspace home pauses playback and selecting history restores the complete report', async context => {
  const { document, timers } = await setup(context);
  const home = document.querySelector('#workspace-home-button');
  assert.ok(home, 'Workspace must expose its empty-state view');
  document.querySelector('#play-demo').click();
  home.click();
  assert.equal(document.querySelector('#workspace-home').hidden, false);
  assert.equal([...timers.values()].filter(timer => timer.delay === 1800).length, 0);
  document.querySelector('[data-run="msft"]').click();
  assert.equal(document.querySelector('#workspace-home').hidden, true);
  assert.equal(document.querySelector('#ticker-title').textContent, 'MSFT');
  assert.equal(document.querySelector('#directory-count').textContent, '16');
});

test('appearance controls synchronize across login and workspace without changing research', async context => {
  const { window, document } = await setup(context, { login: true });
  const launchers = document.querySelectorAll('[data-open-appearance]');
  assert.equal(launchers.length, 2, 'Both screens expose appearance settings');
  launchers[0].click();
  assert.equal(document.querySelector('#appearance-dialog').open, true);
  document.querySelector('[name="appearance-theme"][value="dark"]').click();
  assert.equal(document.documentElement.dataset.theme, 'dark');
  assert.match(document.querySelector('#theme-toggle').getAttribute('aria-label'), /浅色/);
  assert.match(document.querySelector('#login-theme-toggle').getAttribute('aria-label'), /浅色/);
  document.querySelector('#appearance-motion').click();
  assert.equal(document.documentElement.dataset.motion, 'reduced');
  assert.equal(document.querySelector('#login-motion-toggle').getAttribute('aria-pressed'), 'true');
  document.querySelector('#appearance-grid').click();
  assert.equal(document.documentElement.dataset.grid, 'hidden');
  document.querySelector('[name="appearance-density"][value="compact"]').click();
  assert.equal(document.documentElement.dataset.density, 'compact');
  document.querySelector('[data-close="appearance-dialog"]').click();
  document.querySelector('#login-theme-toggle').click();
  assert.equal(document.querySelector('[name="appearance-theme"][value="light"]').checked, true);
  document.querySelector('#login-motion-toggle').click();
  assert.equal(document.querySelector('#appearance-motion').checked, false);
  assert.equal(document.querySelector('#ticker-title').textContent, 'QQQM');
  assert.equal(document.querySelectorAll('#history-list [data-run]').length, 4);
  assert.equal(window.localStorage.length, 0);
});

test('interactive grid clears on reduced motion and ignores touch or hidden backgrounds', async context => {
  const { window, document } = await setup(context, { login: true });
  const cell = document.querySelector('#login-grid-hover');
  assert.ok(cell, 'Login provides an interactive grid cell');
  const screen = document.querySelector('#login-screen');
  const point = pointerType => {
    const event = new window.MouseEvent('pointermove', { clientX: 130, clientY: 195, bubbles: true });
    Object.defineProperty(event, 'pointerType', { value: pointerType }); screen.dispatchEvent(event);
  };
  point('mouse');
  assert.equal(cell.hidden, false);
  assert.equal(cell.style.left, '129px');
  assert.equal(cell.style.top, '193px');
  document.querySelector('#login-motion-toggle').click();
  assert.equal(cell.hidden, true);
  point('mouse');
  assert.equal(cell.hidden, true);
  document.querySelector('#login-motion-toggle').click();
  point('touch');
  assert.equal(cell.hidden, true);
  point('mouse');
  assert.equal(cell.hidden, false);
  screen.dispatchEvent(new window.Event('pointerleave'));
  assert.equal(cell.hidden, true);
  document.querySelector('#appearance-grid').click();
  point('mouse');
  assert.equal(cell.hidden, true);
});

test('global search navigates actual report chapters with keyboard and preserves history', async context => {
  const { window, document, timers } = await setup(context);
  const launcher = document.querySelector('#command-button');
  assert.ok(launcher, 'Workspace provides a global search launcher');
  document.querySelector('#play-demo').click();
  launcher.click();
  const input = document.querySelector('#command-query');
  assert.equal(document.querySelector('#command-dialog').open, true);
  assert.equal(document.activeElement, input);
  input.value = 'msft risk_debate_state.judge_decision';
  input.dispatchEvent(new window.Event('input', { bubbles: true }));
  const results = document.querySelectorAll('#command-results [role="option"]');
  assert.equal(results.length, 1);
  assert.match(results[0].textContent, /风险裁决/);
  assert.equal(input.getAttribute('aria-activedescendant'), results[0].id);
  input.dispatchEvent(new window.KeyboardEvent('keydown', { key: 'Enter', bubbles: true, cancelable: true }));
  assert.equal(document.querySelector('#command-dialog').open, false);
  assert.equal(document.querySelector('#ticker-title').textContent, 'MSFT');
  assert.match(document.querySelector('#report-heading').textContent, /风险裁决/);
  assert.equal(document.activeElement, document.querySelector('#report-article'));
  assert.equal(document.querySelector('#directory-count').textContent, '16');
  assert.equal(document.querySelectorAll('#history-list [data-run]').length, 4);
  assert.equal([...timers.values()].filter(timer => timer.delay === 1800).length, 0);
  document.dispatchEvent(new window.KeyboardEvent('keydown', { key: 'k', ctrlKey: true, bubbles: true, cancelable: true }));
  assert.equal(document.querySelector('#command-dialog').open, true);
  const first = input.getAttribute('aria-activedescendant');
  input.dispatchEvent(new window.KeyboardEvent('keydown', { key: 'ArrowDown', bubbles: true, cancelable: true }));
  assert.notEqual(input.getAttribute('aria-activedescendant'), first);
  input.value = '<no results>';
  input.dispatchEvent(new window.Event('input', { bubbles: true }));
  assert.equal(document.querySelector('#command-empty').hidden, false);
  assert.equal(input.hasAttribute('aria-activedescendant'), false);
  input.dispatchEvent(new window.KeyboardEvent('keydown', { key: 'Enter', bubbles: true, cancelable: true }));
  assert.equal(document.querySelector('#ticker-title').textContent, 'MSFT');
  document.querySelector('[data-close="command-dialog"]').click();
  document.querySelector('#preview-login').click();
  document.dispatchEvent(new window.KeyboardEvent('keydown', { key: 'k', ctrlKey: true, bubbles: true, cancelable: true }));
  assert.equal(document.querySelector('#command-dialog').open, false);
});

test('global search exposes new chapters only when the demo actually produces them', async context => {
  const { window, document, tick } = await setup(context);
  const launcher = document.querySelector('#command-button');
  assert.ok(launcher, 'Workspace provides a global search launcher');
  document.querySelector('#play-demo').click();
  launcher.click();
  const input = document.querySelector('#command-query');
  input.value = 'qqqm final_trade_decision';
  input.dispatchEvent(new window.Event('input', { bubbles: true }));
  assert.equal(document.querySelectorAll('#command-results [role="option"]').length, 0);
  for (let step = 0; step < 3; step += 1) tick();
  assert.equal(document.querySelectorAll('#command-results [role="option"]').length, 1);
  assert.equal(input.value, 'qqqm final_trade_decision');
  assert.equal(document.activeElement, input);
  document.querySelector('#command-results [role="option"]').click();
  assert.equal(document.querySelector('#command-dialog').open, false);
  assert.match(document.querySelector('#report-heading').textContent, /最终/);
});

test('prototype selects history and navigates reports without leaving the page', async context => {
  const { document } = await setup(context);
  assert.equal(document.querySelector('#ticker-title').textContent, 'QQQM');
  assert.match(document.querySelector('#demo-label').textContent, /演示/);
  document.querySelector('[data-run="msft"]').click();
  assert.equal(document.querySelector('#ticker-title').textContent, 'MSFT');
  document.querySelector('[data-report="risk"]').click();
  assert.equal(document.querySelector('[data-report="risk"]').getAttribute('aria-selected'), 'true');
  assert.match(document.querySelector('#report-body').textContent, /风险/);
});

test('prototype validates a new request and preserves the existing history', async context => {
  const { window, document } = await setup(context);
  const before = document.querySelectorAll('#history-list [data-run]').length;
  document.querySelector('#new-research').click();
  assert.equal(document.querySelector('#research-drawer').open, true);
  document.querySelector('#ticker-input').value = '<invalid>';
  document.querySelector('#research-form').dispatchEvent(new window.Event('submit', { bubbles: true, cancelable: true }));
  assert.equal(document.querySelector('#research-drawer').open, true);
  assert.equal(document.querySelector('#form-error').hidden, false);
  document.querySelector('#ticker-input').value = ' spy ';
  document.querySelector('#research-form').dispatchEvent(new window.Event('submit', { bubbles: true, cancelable: true }));
  assert.equal(document.querySelector('#research-drawer').open, false);
  assert.equal(document.querySelector('#ticker-title').textContent, 'SPY');
  assert.equal(document.querySelectorAll('#history-list [data-run]').length, before + 1);
});

test('demo advances to completion and stop cancels pending phase changes', async context => {
  const { document, timers, tick } = await setup(context);
  document.querySelector('#play-demo').click();
  for (let step = 0; step < 3; step += 1) tick();
  assert.equal(document.querySelector('#run-status').dataset.status, 'succeeded');
  assert.equal([...timers.values()].filter(timer => timer.delay === 1800).length, 0);
  document.querySelector('[data-run="nvda"]').click();
  document.querySelector('#retry-run').click();
  document.querySelector('#stop-run').click();
  assert.equal(document.querySelector('#run-status').dataset.status, 'stopped');
  assert.equal([...timers.values()].filter(timer => timer.delay === 1800).length, 0);
});

test('search, theme, reduced motion and mobile history have functional controls', async context => {
  const { window, document } = await setup(context);
  document.querySelector('#history-search').value = 'VOO';
  document.querySelector('#history-search').dispatchEvent(new window.Event('input', { bubbles: true }));
  assert.equal(document.querySelectorAll('#history-list [data-run]').length, 1);
  document.querySelector('#theme-toggle').click();
  assert.equal(document.documentElement.dataset.theme, 'dark');
  document.querySelector('#motion-toggle').click();
  assert.equal(document.documentElement.dataset.motion, 'reduced');
  document.querySelector('#mobile-history').click();
  assert.equal(document.querySelector('#history-drawer').open, true);
  document.querySelector('#history-drawer [data-run="voo"]').click();
  assert.equal(document.querySelector('#ticker-title').textContent, 'VOO');
  assert.equal(document.querySelector('#history-drawer').open, false);
});

test('English report selection produces English sample content after demo analysis', async context => {
  const { window, document, tick } = await setup(context);
  document.querySelector('#new-research').click();
  document.querySelector('[name="language"]').value = 'English';
  document.querySelector('#research-form').dispatchEvent(new window.Event('submit', { bubbles: true, cancelable: true }));
  assert.match(document.querySelector('#report-heading').textContent, /Waiting for research evidence/);
  tick();
  tick();
  assert.match(document.querySelector('#report-body').textContent, /Illustrative content/);
  assert.equal(document.querySelector('[data-report="risk"]').hidden, true);
  tick();
  tick();
  document.querySelector('[data-report="risk"]').click();
  assert.match(document.querySelector('#report-heading').textContent, /risk/i);
});

test('pause and page visibility suspend demo phase updates until explicitly resumed', async context => {
  const { window, document, timers } = await setup(context);
  const phaseTimers = () => [...timers.values()].filter(timer => timer.delay === 1800).length;
  document.querySelector('#play-demo').click();
  assert.equal(phaseTimers(), 1);
  document.querySelector('#play-demo').click();
  assert.equal(phaseTimers(), 0);
  document.querySelector('#play-demo').click();
  Object.defineProperty(document, 'hidden', { configurable: true, value: true });
  document.dispatchEvent(new window.Event('visibilitychange'));
  assert.equal(phaseTimers(), 0);
  Object.defineProperty(document, 'hidden', { configurable: true, value: false });
  document.dispatchEvent(new window.Event('visibilitychange'));
  assert.equal(phaseTimers(), 1);
});

test('selecting another task pauses the previous demo without advancing it', async context => {
  const { document, timers } = await setup(context);
  document.querySelector('#play-demo').click();
  assert.equal([...timers.values()].filter(timer => timer.delay === 1800).length, 1);
  document.querySelector('[data-run="msft"]').click();
  assert.equal([...timers.values()].filter(timer => timer.delay === 1800).length, 0);
  document.querySelector('[data-run="qqqm"]').click();
  assert.equal(document.querySelector('#stage-description').textContent, '观点交叉验证中');
  assert.match(document.querySelector('#play-demo').textContent, /播放演示/);
});

test('interrupted history filter includes failed and stopped tasks', async context => {
  const { document } = await setup(context);
  document.querySelector('#stop-run').click();
  const filter = document.querySelector('[data-filter="interrupted"]');
  assert.ok(filter, 'Interrupted tasks need a history filter');
  filter.click();
  assert.deepEqual([...document.querySelectorAll('#history-list [data-run]')].map(button => button.dataset.run), ['qqqm', 'nvda']);
  document.querySelector('[data-filter="all"]').click();
  assert.equal(document.querySelectorAll('#history-list [data-run]').length, 4);
});

test('task parameters retain the selected snapshot and load a draft without starting a task', async context => {
  const { document } = await setup(context);
  document.querySelector('[data-run="voo"]').click();
  const parameters = document.querySelector('#request-details');
  assert.ok(parameters, 'Selected task parameters must remain accessible');
  const snapshot = JSON.parse(document.querySelector('#request-json').textContent);
  assert.equal(snapshot.ticker, 'VOO');
  assert.equal(snapshot.asset_type, 'etf');
  assert.equal(snapshot.language, 'Chinese');
  assert.equal(snapshot.model, 'gpt-5-mini');
  assert.equal(snapshot.debate_rounds, 1);
  assert.equal(snapshot.risk_rounds, 1);
  assert.deepEqual(Object.keys(snapshot.data_vendors).sort(), [
    'core_stock_apis', 'fundamental_data', 'macro_data', 'news_data', 'prediction_markets', 'technical_indicators',
  ]);
  assert.match(parameters.textContent, /gpt-5-mini/);
  const count = document.querySelectorAll('#history-list [data-run]').length;
  document.querySelector('#load-request').click();
  assert.equal(document.querySelector('#research-drawer').open, true);
  assert.equal(document.querySelector('#ticker-input').value, 'VOO');
  assert.equal(document.querySelector('[name="model"]').value, 'gpt-5-mini');
  assert.equal(document.querySelectorAll('[name="analysts"]:checked').length, 2);
  document.querySelector('#ticker-input').value = 'SPY';
  assert.equal(JSON.parse(document.querySelector('#request-json').textContent).ticker, 'VOO');
  assert.equal(document.querySelectorAll('#history-list [data-run]').length, count);
});

test('report directory retains all available sections and exact counts as phases progress', async context => {
  const { document, tick } = await setup(context);
  const sections = () => [...document.querySelectorAll('#report-directory [data-section]')];
  assert.equal(sections().length, 4);
  assert.equal(document.querySelector('#report-count').textContent, '4');
  assert.equal(document.querySelector('[data-section="final_trade_decision"]'), null);
  document.querySelector('#play-demo').click();
  tick(); tick(); tick();
  assert.equal(sections().length, 16);
  assert.equal(document.querySelector('#report-count').textContent, '16');
  const bodies = new Set();
  for (const button of sections()) {
    button.click();
    assert.equal(button.getAttribute('aria-current'), 'true');
    const body = document.querySelector('#report-body').textContent;
    assert.match(body, /示例/);
    bodies.add(body);
  }
  assert.equal(bodies.size, 16, 'Each section must open its own report, not the overview');
  assert.match(document.querySelector('#report-heading').textContent, /最终决策/);
  document.querySelector('[data-run="voo"]').click();
  assert.equal(sections().length, 14);
  assert.equal(document.querySelector('[data-section="news_report"]'), null);
  assert.equal(document.querySelector('[data-section="sentiment_report"]'), null);
  document.querySelector('[data-run="nvda"]').click();
  assert.equal(sections().length, 0);
  assert.equal(document.querySelector('#report-count').textContent, '0');
  assert.match(document.querySelector('#report-directory').textContent, /尚无/);
});

test('run events retain timestamps, typed payloads, filters and expanded details during updates', async context => {
  const { document, window, tick } = await setup(context);
  document.querySelector('[data-view="events"]').click();
  const rows = () => [...document.querySelectorAll('#event-list > li')];
  assert.ok(rows().every(row => row.dataset.kind), 'Events must retain their structured kind');
  const timestamps = rows().map(row => row.querySelector('time').dateTime);
  assert.equal(new Set(timestamps).size, rows().length);
  assert.ok(timestamps.every(timestamp => Number.isFinite(Date.parse(timestamp))));
  assert.deepEqual(timestamps, [...timestamps].sort().reverse());
  const report = document.querySelector('#event-list [data-kind="report"]');
  assert.match(report.textContent, /字符/);
  const reportPayload = JSON.parse(report.querySelector('pre').textContent);
  assert.equal(typeof reportPayload.content, 'string');
  assert.ok(reportPayload.content.length > 50);
  report.querySelector('details').open = true;
  const originalTimestamp = report.querySelector('time').dateTime;
  const before = rows().length;
  document.querySelector('#play-demo').click();
  tick();
  assert.ok(rows().length > before);
  assert.ok(rows().find(row => row.querySelector('time').dateTime === originalTimestamp).querySelector('details').open);
  const kind = document.querySelector('#event-kind');
  kind.value = 'usage'; kind.dispatchEvent(new window.Event('change', { bubbles: true }));
  assert.ok(rows().length > 0);
  assert.ok(rows().every(row => row.dataset.kind === 'usage'));
  assert.match(rows()[0].textContent, /输入.*tokens/);
  const payload = JSON.parse(rows()[0].querySelector('pre').textContent);
  assert.ok(payload.llm_calls > 0 && payload.tool_calls > 0 && payload.input_tokens > 0 && payload.output_tokens > 0);
  const search = document.querySelector('#event-query');
  search.value = 'no-such-event'; search.dispatchEvent(new window.Event('input', { bubbles: true }));
  assert.equal(rows().length, 0);
  assert.match(document.querySelector('#event-empty').textContent, /没有匹配/);
  document.querySelector('[data-run="nvda"]').click();
  assert.equal(kind.value, 'all');
  assert.equal(search.value, '');
  assert.match(document.querySelector('#event-list [data-kind="error"]').textContent, /超时/);
  document.querySelector('[data-run="msft"]').click();
  assert.deepEqual([...new Set(rows().map(row => row.dataset.kind))].sort(), ['connectivity', 'phase', 'report', 'result', 'usage']);
  assert.match(document.querySelector('#event-list [data-kind="result"]').textContent, /succeeded/);
});

test('exports retain full task parameters, all chapters and structured events', async context => {
  const { document, downloads } = await setup(context);
  document.querySelector('[data-run="msft"]').click();
  document.querySelector('[data-export="json"]').click();
  const data = JSON.parse(await downloads[0].text());
  assert.equal(data.request?.ticker, 'MSFT');
  assert.equal(data.request.model, 'gpt-5.6-sol');
  assert.equal(data.events[0].payload.request.model, 'gpt-5.6-sol');
  assert.equal(Object.keys(data.request.data_vendors).length, 6);
  assert.equal(data.reports.length, 16);
  assert.ok(data.events.every(event => event.kind && event.timestamp && event.payload));
  document.querySelector('[data-report="market"]').click();
  document.querySelector('[data-export="markdown"]').click();
  const markdown = await downloads[1].text();
  for (const report of data.reports) assert.ok(markdown.includes(report.content), `Missing ${report.section}`);
  assert.match(markdown, /data_vendors/);
  assert.match(markdown, /演示数据/);
});

test('state-linked motion preserves exact metrics, full events and keyboard focus', async context => {
  const { document, window, tick } = await setup(context);
  const metric = document.querySelector('#metric-calls');
  assert.equal(metric.querySelector('.sr-only')?.textContent, '18');
  assert.equal(metric.querySelector('.number-window')?.getAttribute('aria-hidden'), 'true');
  document.querySelector('[data-view="events"]').click();
  document.querySelector('#play-demo').click();
  assert.equal(document.querySelectorAll('.flow-beam').length, 1);
  const details = document.querySelector('#event-list details');
  details.open = true; details.querySelector('summary').focus();
  const index = details.dataset.eventIndex;
  const before = document.querySelectorAll('#event-list > li').length;
  tick();
  assert.equal(metric.querySelector('.sr-only').textContent, '24');
  assert.equal(metric.querySelector('.number-reel').dataset.from, '18');
  assert.equal(document.querySelectorAll('#report-directory [data-section]').length, 10);
  const added = document.querySelectorAll('#event-list > li').length - before;
  assert.equal(document.querySelectorAll('#event-list > .event-arrival').length, added);
  assert.equal(document.activeElement.closest('details')?.dataset.eventIndex, index);
  assert.equal(document.activeElement.closest('details')?.open, true);
  const reel = metric.querySelector('.number-reel');
  document.querySelector('#play-demo').click();
  assert.equal(document.querySelectorAll('.flow-beam').length, 0);
  assert.equal(metric.querySelector('.number-reel'), reel, 'Unchanged metrics must not restart their animation');
  document.querySelector('#event-kind').value = 'report';
  document.querySelector('#event-kind').dispatchEvent(new window.Event('change', { bubbles: true }));
  assert.equal(document.querySelectorAll('#event-list > .event-arrival').length, 0);
});

test('reduced motion keeps reports, numbers and new events immediate', async context => {
  const { document, tick } = await setup(context, { reducedMotion: true });
  document.querySelector('#play-demo').click();
  tick();
  assert.equal(document.querySelector('#metric-calls .sr-only')?.textContent, '24');
  assert.equal(document.querySelectorAll('#report-directory [data-section]').length, 10);
  assert.equal(document.querySelectorAll('.event-arrival, .updated, .arrive').length, 0);
  assert.ok(document.querySelectorAll('#event-list > li').length > 11);
});

test('login preview validates locally and only accepts the explicit demo account', async context => {
  const { document, window, timers } = await setup(context, { login: true });
  const login = document.querySelector('#login-screen');
  assert.ok(login, 'The prototype must include a dedicated login screen');
  assert.equal(login.hidden, false);
  assert.equal(document.querySelector('.app').hidden, true);
  const form = document.querySelector('#login-form');
  const submit = () => form.dispatchEvent(new window.Event('submit', { bubbles: true, cancelable: true }));
  submit();
  assert.match(document.querySelector('#login-error').textContent, /邮箱/);
  assert.equal(document.activeElement.id, 'login-email');
  document.querySelector('#login-email').value = 'someone@example.test';
  document.querySelector('#login-password').value = 'not-a-real-password';
  submit();
  assert.match(document.querySelector('#login-error').textContent, /演示账户/);
  assert.equal(document.querySelector('#login-password').value, '');
  assert.equal(login.hidden, false);
  document.querySelector('#fill-demo-login').click();
  const password = document.querySelector('#login-password');
  assert.ok(password.value);
  assert.equal(password.type, 'password');
  document.querySelector('#login-password-toggle').click();
  assert.equal(password.type, 'text');
  assert.equal(document.querySelector('#login-password-toggle').getAttribute('aria-pressed'), 'true');
  submit();
  assert.equal(form.getAttribute('aria-busy'), 'true');
  assert.equal(document.querySelector('#login-submit').disabled, true);
  assert.equal(password.value, '', 'Never retain credentials during or after the simulated transition');
  const pending = [...timers].find(([, timer]) => timer.delay === 650);
  assert.ok(pending);
  timers.delete(pending[0]); pending[1].task();
  assert.equal(login.hidden, true);
  assert.equal(document.querySelector('.app').hidden, false);
  assert.equal(document.activeElement.id, 'workspace');
  assert.equal(window.localStorage.length, 0);
  assert.equal(window.sessionStorage.length, 0);
  assert.equal(document.cookie, '');
  assert.equal(password.type, 'password');
  assert.equal(document.querySelector('#report-count').textContent, '4');
});

test('returning to login pauses research and preserves history and motion controls', async context => {
  const { document, window, timers } = await setup(context);
  document.querySelector('#play-demo').click();
  document.querySelector('#account-button').click();
  const returnButton = document.querySelector('#preview-login');
  assert.ok(returnButton);
  returnButton.click();
  assert.equal(document.querySelector('#account-dialog').open, false);
  assert.equal(document.querySelector('#login-screen').hidden, false);
  assert.equal(document.querySelector('.app').hidden, true);
  assert.equal([...timers.values()].filter(timer => timer.delay === 1800).length, 0);
  assert.equal(document.querySelectorAll('#history-list [data-run]').length, 4);
  assert.equal(new URL(window.location.href).searchParams.get('view'), 'login');
  document.querySelector('#login-theme-toggle').click();
  assert.equal(document.documentElement.dataset.theme, 'dark');
  document.querySelector('#login-motion-toggle').click();
  assert.equal(document.documentElement.dataset.motion, 'reduced');
  assert.equal(document.querySelector('#motion-toggle').getAttribute('aria-pressed'), 'true');
  assert.equal(document.querySelector('#login-motion-toggle').getAttribute('aria-pressed'), 'true');
});