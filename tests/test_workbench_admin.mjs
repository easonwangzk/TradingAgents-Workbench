import test from 'node:test';
import assert from 'node:assert/strict';
import { createAdminService } from '../tradingagents/workbench/static/admin-workspace.mjs';
import { createApi } from '../tradingagents/workbench/static/ui-core.mjs';

export const OWNER = '11111111-1111-4111-8111-111111111111';
export const MEMBER = '22222222-2222-4222-8222-222222222222';
const permissions = { research: true, assistant: true, market: true };
export const accounts = [
  { object_id: OWNER, email: 'owner@example.test', name: 'Owner', is_admin: true, disabled: false, permissions },
  { object_id: MEMBER, email: 'member@example.test', name: 'Member', is_admin: false, disabled: false, permissions },
];
export const coverage = { since: null, notes: ['SYNTHETIC'], truncated: true };
export const overview = { accounts: { total: 2, enabled: 2, disabled: 0 }, runs: { total: 3, succeeded: 2, failed: 1, active: 0 }, budget: null, recent_errors: [], coverage };
const owner = { user: accounts[0] };
const edit = { disabled: true, permissions: { research: false, assistant: true, market: true } };

function fixture(handler = async (path, options) => options.method === 'PATCH'
  ? { account: { ...accounts[1], ...options.body }, version: 'v2' }
  : path.endsWith('/accounts') ? { accounts, version: 'v1' } : overview) {
  const calls = [];
  const service = createAdminService({ request: async (path, options = {}) => { calls.push({ path, options }); return handler(path, options); } });
  return { service, calls };
}

test('member and logged-out state never issue admin requests', async () => {
  const { service, calls } = fixture();
  await service.open();
  service.setSession({ user: accounts[1] });
  await service.open(); await service.refresh(); await service.save(MEMBER, edit);
  assert.equal(calls.length, 0);
  service.setSession(owner); await service.open();
  assert.equal(calls[0].path, '/api/admin/overview');
  service.setSession(null); await service.refresh();
  assert.equal(service.state.overview, null);
  assert.equal(calls.length, 1);
});

test('all tabs use exact read contracts and bounded usage parameters', async () => {
  const { service, calls } = fixture(async path => path.includes('/usage?') ? { days: 30, rows: [], coverage }
    : path.endsWith('/audit') ? { events: [], truncated: false }
      : path.endsWith('/accounts') ? { accounts, version: 'v1' } : overview);
  service.setSession(owner); await service.open();
  await service.selectTab('accounts');
  await service.selectTab('usage');
  await service.setUsage({ days: 30, accountId: MEMBER });
  await service.selectTab('audit');
  assert.deepEqual(calls.map(call => call.path), ['/api/admin/overview', '/api/admin/accounts', '/api/admin/usage?days=7', `/api/admin/usage?days=30&account_id=${MEMBER}`, '/api/admin/audit']);
  await assert.rejects(service.setUsage({ days: 14 }), /invalid/i);
});

test('overview normalizes usage failure indicators with empty run ids and event times', async () => {
  const recent_errors = [{ account_email: 'member@example.test', run_id: '', code: 'assistant_failed', occurred_at: '2026-10-12T10:00:00+00:00' },
    { account_email: 'member@example.test', run_id: '', code: 'provider_failed', occurred_at: '2026-10-12T10:00:01+00:00' }];
  const { service } = fixture(async () => ({ ...overview, recent_errors }));
  service.setSession(owner); await service.open();
  assert.deepEqual(service.state.overview.recent_errors, recent_errors);
  assert.equal(service.state.error, '');
});

test('explicit save sends complete state with loaded revision and protects owner', async () => {
  const { service, calls } = fixture();
  service.setSession(owner); await service.open(); await service.selectTab('accounts');
  await service.save(OWNER, edit);
  assert.equal(calls.filter(call => call.options.method === 'PATCH').length, 0);
  await service.save(MEMBER, edit);
  const write = calls.at(-1);
  assert.equal(write.path, `/api/admin/accounts/${MEMBER}`);
  assert.equal(write.options.revision, 'v1');
  assert.deepEqual(write.options.body, edit);
  assert.equal(service.state.version, 'v2');
  assert.equal(service.state.accounts[1].disabled, true);
});

for (const outcome of [409, 412, 'unknown', 'malformed', 'mismatch']) {
  test(`${outcome} save requires a fresh list with no automatic retry`, async () => {
    let fail = true;
    const { service, calls } = fixture(async (path, options) => {
      if (options.method !== 'PATCH') return path.endsWith('/accounts') ? { accounts, version: 'v1' } : overview;
      if (!fail) return { account: { ...accounts[1], ...edit }, version: 'v2' };
      if (outcome === 'malformed') return { ok: true };
      if (outcome === 'mismatch') return { account: accounts[0], version: 'v2' };
      throw Object.assign(new Error('private raw error must not render'), typeof outcome === 'number' ? { status: outcome } : {});
    });
    service.setSession(owner); await service.open(); await service.selectTab('accounts');
    await service.save(MEMBER, edit); await service.save(MEMBER, edit);
    assert.equal(calls.filter(call => call.options.method === 'PATCH').length, 1);
    assert.equal(service.state.needsRefresh, true);
    assert.equal(service.state.error.includes('private raw'), false);
    fail = false; await service.refresh(); await service.save(MEMBER, edit);
    assert.equal(calls.filter(call => call.options.method === 'PATCH').length, 2);
  });
}

test('overlapping saves and reads cannot abort an in-flight mutation', async () => {
  const gate = Promise.withResolvers();
  const { service, calls } = fixture(async (path, options) => options.method === 'PATCH' ? gate.promise : path.endsWith('/accounts') ? { accounts, version: 'v1' } : overview);
  service.setSession(owner); await service.open(); await service.selectTab('accounts');
  const pending = service.save(MEMBER, edit);
  await service.save(MEMBER, edit); await service.refresh(); await service.selectTab('usage');
  assert.equal(calls.length, 3);
  assert.equal(calls.at(-1).options.signal.aborted, false);
  gate.resolve({ account: { ...accounts[1], ...edit }, version: 'v2' }); await pending;
});

for (const action of ['logout', 'switch', 'close', 'tab']) {
  test(`late reads are fenced after ${action}`, async () => {
    const gate = Promise.withResolvers();
    const { service } = fixture(() => gate.promise);
    service.setSession(owner); const pending = service.open();
    if (action === 'logout') service.setSession(null);
    if (action === 'switch') service.setSession({ user: { ...accounts[0], object_id: MEMBER } });
    if (action === 'close') service.close();
    if (action === 'tab') void service.selectTab('accounts');
    gate.resolve(overview); await pending;
    assert.equal(service.state.overview, null);
  });
}

test('late mutation response and failure never repopulate a logged-out session', async () => {
  for (const rejected of [false, true]) {
    const gate = Promise.withResolvers();
    const { service } = fixture(async (path, options) => options.method === 'PATCH' ? gate.promise : path.endsWith('/accounts') ? { accounts, version: 'v1' } : overview);
    service.setSession(owner); await service.open(); await service.selectTab('accounts');
    const pending = service.save(MEMBER, edit); service.setSession(null);
    if (rejected) gate.reject(new Error('old error')); else gate.resolve({ account: { ...accounts[1], ...edit }, version: 'v2' });
    await pending;
    assert.deepEqual(service.state.accounts, []); assert.equal(service.state.error, '');
  }
});

test('request helper allows only exact admin paths/methods with CSRF and If-Match', async () => {
  const calls = [];
  const api = createApi({ emailMode: true, token: 'synthetic-csrf', fetchImpl: async (path, options) => { calls.push({ path, options }); return { ok: true, json: async () => ({}) }; } });
  await api('/api/admin/accounts'); await api(`/api/admin/accounts/${MEMBER}`, { method: 'PATCH', body: edit, revision: 'v1' });
  await api(`/api/admin/usage?days=30&account_id=${MEMBER}`);
  assert.equal(calls[1].options.headers['If-Match'], 'v1');
  assert.equal(calls[1].options.headers['X-Workbench-Token'], 'synthetic-csrf');
  for (const [path, method] of [['/api/admin/overview', 'POST'], ['/api/admin/accounts', 'PATCH'], ['/api/admin/usage?days=8', 'GET'], ['/api/admin/usage?days=7&secret=x', 'GET'], [`/api/admin/accounts/${MEMBER}/report`, 'GET']]) await assert.rejects(api(path, { method }));
  await assert.rejects(createApi({ emailMode: true })(`/api/admin/accounts/${MEMBER}`, { method: 'PATCH', body: edit, revision: 'v1' }));
});

test('safe invitation/recovery links reuse redeem/reset endpoints and reject foreign links', async () => {
  for (const [kind, value, fragment] of [['invite', 'new@example.test', '/auth/redeem#token=SYNTHETIC'], ['recovery', MEMBER, '/auth/reset#token=SYNTHETIC'], ['invite', 'new@example.test', 'https://foreign.test/#token=SYNTHETIC']]) {
    const { service, calls } = fixture(async (path, options) => options.method === 'POST' ? { fragment, email: 'new@example.test', token_id: 'not-stored' } : path.endsWith('/accounts') ? { accounts, version: 'v1' } : overview);
    service.setSession(owner); await service.open(); await service.selectTab('accounts');
    const success = await service.emailAction(kind, value);
    assert.equal(success, fragment.startsWith('/auth/'));
    assert.equal(service.state.actionResult?.fragment || null, fragment.startsWith('/auth/') ? fragment : null);
    assert.equal(JSON.stringify(service.state).includes('not-stored'), false);
    assert.deepEqual(calls.at(-1).options.body, kind === 'invite' ? { email: value } : { user_id: MEMBER });
  }
});