import test from 'node:test';
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { readFile } from 'node:fs/promises';
import * as admin from '../tradingagents/workbench/static/admin-workspace.mjs';
import { mountWorkbench } from '../tradingagents/workbench/static/app.js';
import { mountLocale } from '../tradingagents/workbench/static/locale.mjs';

const staticUrl = new URL('../tradingagents/workbench/static/', import.meta.url);
const require = createRequire(new URL('package.json', staticUrl));
const { JSDOM } = require('jsdom');
const OWNER = '11111111-1111-4111-8111-111111111111', MEMBER = '22222222-2222-4222-8222-222222222222';
const permissions = { research: true, assistant: true, market: true };
const accounts = [{ object_id: OWNER, email: 'owner@example.test', name: 'Owner', is_admin: true, disabled: false, permissions },
  { object_id: MEMBER, email: 'member@example.test', name: '<img src=x> Member', is_admin: false, disabled: false, permissions }];
const coverage = { since: null, truncated: true, notes: ['SYNTHETIC coverage'] };
const overview = { accounts: { total: 2, enabled: 2, disabled: 0 }, runs: { total: 3, succeeded: 2, failed: 1, active: 0 }, budget: null,
  recent_errors: [{ account_email: 'member@example.test', run_id: MEMBER, code: 'synthetic_failure', occurred_at: null }], coverage };
const flush = async () => { await new Promise(setImmediate); await new Promise(setImmediate); };

async function fixture(context, { gate, failure, owner = true } = {}) {
  assert.equal(typeof admin.mountAdminWorkspace, 'function', 'Management mount is missing');
  const dom = new JSDOM(await readFile(new URL('index.html', staticUrl), 'utf8'), { url: 'http://127.0.0.1/', pretendToBeVisual: true });
  const document = dom.window.document, calls = [];
  const view = admin.mountAdminWorkspace(document, { request: async (path, options = {}) => {
    calls.push({ path, options });
    if (options.method === 'PATCH') {
      if (gate) await gate.promise;
      if (failure) throw Object.assign(new Error('raw private failure'), { status: failure });
      return { account: { ...accounts[1], ...options.body }, version: 'v2' };
    }
    if (path.endsWith('/accounts')) return { accounts, version: 'v1' };
    if (path.includes('/usage?')) return { days: 7, coverage, rows: [{ account_id: MEMBER, email: accounts[1].email, research_runs: 1, research_failed: 0, assistant_calls: 2, input_tokens: null, output_tokens: null, platform_cost_usd: null, external_cost_usd: null, last_activity: null }] };
    if (path.endsWith('/audit')) return { events: [{ id: 'event-1', actor_email: accounts[0].email, target_email: accounts[1].email, action: 'permissions', result: 'success', before: { permissions, secret: 'NEVER-RENDER' }, after: { permissions }, occurred_at: null }], truncated: true };
    if (options.method === 'POST') return { email: accounts[1].email, fragment: '/auth/recover#token=SYNTHETIC', token_id: 'not-a-table-value' };
    return overview;
  } });
  context.after(() => { view.dispose(); dom.window.close(); });
  view.setSession({ user: accounts[owner ? 0 : 1] });
  return { view, document, window: dom.window, calls };
}

test('four tabs mount as owner-only sibling with null metrics and explicit coverage', async context => {
  const { view, document } = await fixture(context);
  assert.ok(document.querySelector('#workspace-navigation #management-entry'));
  assert.equal(document.querySelector('#management-screen').hidden, true);
  await view.open();
  assert.equal(document.querySelector('#management-screen').hidden, false);
  assert.equal(document.querySelectorAll('[data-admin-tab]').length, 4);
  assert.equal(document.querySelector('[data-admin-metric="budget.available_microusd"]').textContent, '\u2014');
  assert.match(document.querySelector('#admin-overview-coverage').textContent, /截断|truncated/i);
  assert.match(document.querySelector('#admin-overview-coverage').textContent, /起始|since/i);
  document.querySelector('[data-admin-tab="usage"]').click(); await flush();
  assert.equal(document.querySelectorAll('#admin-usage-account option').length, 3);
  assert.equal(document.querySelector('[data-admin-field="input_tokens"]').textContent, '\u2014');
  assert.equal(document.querySelector('[data-admin-field="external_cost_usd"]').textContent, '\u2014');
  document.querySelector('[data-admin-tab="audit"]').click(); await flush();
  assert.match(document.querySelector('#admin-audit-rows').textContent, /permissions/);
  assert.equal(document.querySelector('#admin-audit-rows').textContent.includes('NEVER-RENDER'), false);
  assert.equal(JSON.stringify(view.state.audit).includes('NEVER-RENDER'), false);
});

test('draft search, status filter, save and disable confirmation use actual DOM and revision', async context => {
  const { view, document, window, calls } = await fixture(context);
  await view.open(); document.querySelector('[data-admin-tab="accounts"]').click(); await flush();
  assert.equal(document.querySelector('#admin-account-rows img'), null);
  assert.equal(document.querySelector(`[data-admin-account="${OWNER}"] input`).disabled, true);
  const search = document.querySelector('#admin-account-search'); search.value = 'member'; search.dispatchEvent(new window.Event('input'));
  assert.equal(document.querySelectorAll('#admin-account-rows tr').length, 1);
  const row = document.querySelector(`[data-admin-account="${MEMBER}"]`);
  const disabled = row.querySelector('[data-admin-permission="disabled"]'); disabled.checked = true; disabled.dispatchEvent(new window.Event('change', { bubbles: true }));
  row.querySelector('[data-admin-save]').click(); await flush();
  assert.equal(calls.filter(call => call.options.method).length, 0);
  assert.equal(document.querySelector('#admin-confirm-dialog').open, true);
  document.querySelector('#admin-confirm-accept').click(); await flush();
  assert.equal(calls.at(-1).options.method, 'PATCH'); assert.equal(calls.at(-1).options.revision, 'v1');
  assert.deepEqual(calls.at(-1).options.body, { disabled: true, permissions });
  const status = document.querySelector('#admin-account-filter'); status.value = 'enabled'; status.dispatchEvent(new window.Event('change'));
  assert.equal(document.querySelectorAll('#admin-account-rows tr').length, 0);
});

for (const failure of [409, 412]) test(`DOM conflict ${failure} disables writes until explicit refresh`, async context => {
  const { view, document, window, calls } = await fixture(context, { failure });
  await view.open(); document.querySelector('[data-admin-tab="accounts"]').click(); await flush();
  const row = document.querySelector(`[data-admin-account="${MEMBER}"]`), market = row.querySelector('[data-admin-permission="market"]');
  market.checked = false; market.dispatchEvent(new window.Event('change', { bubbles: true })); row.querySelector('[data-admin-save]').click(); await flush();
  assert.equal(calls.filter(call => call.options.method === 'PATCH').length, 1);
  assert.equal(document.querySelector('[data-admin-save]').disabled, true);
  assert.match(document.querySelector('#admin-status').textContent, /刷新|refresh/i);
  assert.equal(document.querySelector('#admin-status').textContent.includes('raw private'), false);
});

test('invite/recovery cancel and confirm never expose token table; logout closes pending confirmation', async context => {
  const { view, document, window, calls } = await fixture(context);
  await view.open(); document.querySelector('[data-admin-tab="accounts"]').click(); await flush();
  document.querySelector('[data-admin-recovery]').click();
  document.querySelector('#admin-confirm-cancel').click(); await flush();
  assert.equal(calls.filter(call => call.options.method).length, 0);
  document.querySelector('#admin-invite-email').value = 'new@example.test';
  document.querySelector('#admin-invite-form').dispatchEvent(new window.Event('submit', { cancelable: true }));
  assert.equal(document.querySelector('#admin-confirm-dialog').open, true);
  view.setSession(null);
  document.querySelector('#admin-confirm-accept').click(); await flush();
  assert.equal(calls.filter(call => call.options.method).length, 0);
  assert.equal(document.querySelector('#admin-confirm-dialog').open, false);
  assert.equal(document.querySelector('#management-entry').hidden, true);
  assert.equal(document.querySelector('#admin-account-rows').textContent, '');
});

async function appFixture(context, identity, before = async () => {}) {
  const dom = new JSDOM(await readFile(new URL('index.html', staticUrl), 'utf8'), { url: 'http://127.0.0.1/', pretendToBeVisual: true });
  const previous = globalThis.document; globalThis.document = dom.window.document;
  const calls = [], clock = { setTimeout: () => 1, clearTimeout() {} };
  const fetchImpl = async (path, options) => {
    calls.push({ path, options }); await before(path, options);
    const data = path === '/api/me' ? identity : path === '/api/runs' ? { runs: [] } : path === '/api/admin/overview' ? overview : path === '/auth/logout' ? { logged_out: true } : null;
    return { ok: data !== null, status: data === null ? 503 : 200, json: async () => data };
  };
  const app = mountWorkbench(dom.window.document, { fetchImpl, clock });
  context.after(() => { app.dispose(); dom.window.close(); globalThis.document = previous; });
  await app.ready;
  return { document: dom.window.document, calls, app };
}

test('real app owner can use Management despite model-unready configuration', async context => {
  const { document, calls } = await appFixture(context, { user: accounts[0], permissions, csrf_token: 'synthetic' });
  assert.equal(document.querySelector('#management-entry')?.hidden, false);
  assert.equal(document.querySelector('#start-run').disabled, true);
  document.querySelector('#management-entry').click(); await flush();
  assert.equal(document.querySelector('#management-screen').hidden, false);
  assert.equal(document.querySelector('#workspace-home').hidden, true);
  document.querySelector('#research-tasks-entry').click();
  assert.equal(document.querySelector('#management-screen').hidden, true);
  assert.equal(calls.some(call => call.options.method !== 'GET'), false);
});

test('real app guest permission denial hides entries and blocks programmatic feature opening', async context => {
  const { document, calls } = await appFixture(context, { user: accounts[1], permissions: { research: false, assistant: false, market: false }, csrf_token: 'synthetic' });
  assert.equal(document.querySelector('#management-entry')?.hidden, true);
  for (const id of ['research-tasks-entry', 'market-entry', 'research-assistant-entry']) assert.equal(document.getElementById(id).hidden, true);
  document.querySelector('#management-entry').click(); document.querySelector('#market-entry').click(); document.querySelector('#new-research').click();
  await flush();
  assert.equal(document.querySelector('#research-drawer').open, false);
  assert.equal(calls.some(call => call.path.startsWith('/api/admin') || call.path.startsWith('/api/market')), false);
});

test('real app legacy permissions default true and logout fences a late admin response', async context => {
  const gate = Promise.withResolvers();
  const { document, calls } = await appFixture(context, { user: accounts[0], csrf_token: 'synthetic' }, async path => { if (path === '/api/admin/overview') await gate.promise; });
  assert.equal(document.querySelector('#market-entry').hidden, false);
  assert.equal(document.querySelector('#research-tasks-entry').hidden, false);
  document.querySelector('#management-entry').click(); await flush();
  document.querySelector('#account-panel-toggle').click(); await flush();
  document.querySelector('#account-sign-out').click(); await flush();
  gate.resolve(); await flush();
  assert.equal(document.querySelector('#management-screen').hidden, true);
  assert.equal(document.querySelector('#management-entry').hidden, true);
  assert.equal(document.querySelector('[data-admin-metric="accounts.total"]').textContent, '\u2014');
  assert.equal(calls.filter(call => call.path === '/api/admin/overview').length, 1);
});

test('English Management labels and dynamic coverage switch without API writes', async context => {
  const { view, document, calls } = await fixture(context);
  const language = mountLocale(document); language.set('en');
  await view.open();
  assert.equal(document.querySelector('#management-title').textContent, 'Management');
  assert.match(document.querySelector('#admin-accounts-tab').textContent, /Accounts and permissions/);
  assert.match(document.querySelector('#admin-overview-coverage').textContent, /Coverage since.*truncated/);
  assert.ok([...document.querySelectorAll('#admin-overview-panel dt')].some(item => item.textContent === 'Success'));
  assert.equal(document.querySelector('#admin-overview-panel th:last-child').textContent, 'Time');
  assert.equal(calls.some(call => call.options.method), false);
  language.set('zh');
});