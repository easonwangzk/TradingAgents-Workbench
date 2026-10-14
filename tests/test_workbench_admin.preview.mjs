import test from 'node:test';
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { readFile } from 'node:fs/promises';
import { once } from 'node:events';
import { fileURLToPath } from 'node:url';

test('ignored loopback review serves current app and only ephemeral synthetic administration writes', async context => {
  const script = new URL('../.local-run/admin-review/preview.mjs', import.meta.url);
  try { await readFile(script); } catch (error) {
    if (error.code !== 'ENOENT') throw error;
    context.skip('Optional ignored local review server is not present.'); return;
  }
  const process = spawn(globalThis.process.execPath, [fileURLToPath(script)], { env: { ...globalThis.process.env, ADMIN_REVIEW_PORT: '0' }, stdio: ['ignore', 'pipe', 'pipe'] });
  context.after(() => process.kill());
  const [output] = await once(process.stdout, 'data');
  const url = output.toString().match(/http:\/\/127\.0\.0\.1:\d+/)?.[0]; assert.ok(url);
  const html = await (await fetch(url)).text(); assert.match(html, /SYNTHETIC/); assert.match(html, /\/static\/boot.js/);
  assert.equal(await (await fetch(`${url}/static/app.js`)).text(), await readFile(new URL('../tradingagents/workbench/static/app.js', import.meta.url), 'utf8'));
  const read = await (await fetch(`${url}/api/admin/accounts`)).json();
  const account = read.accounts.find(item => !item.is_admin);
  const body = { disabled: true, permissions: { research: false, assistant: true, market: false } };
  const options = { method: 'PATCH', headers: { 'Content-Type': 'application/json', 'If-Match': read.version, 'X-Workbench-Token': 'SYNTHETIC-ONLY', Origin: url }, body: JSON.stringify(body) };
  const saved = await fetch(`${url}/api/admin/accounts/${account.object_id}`, options); assert.equal(saved.status, 200);
  assert.equal((await saved.json()).account.disabled, true);
  assert.equal((await fetch(`${url}/api/admin/accounts/${account.object_id}`, options)).status, 412);
  const guest = await fetch(`${url}/api/admin/accounts`, { headers: { 'X-Synthetic-Role': 'member' } }); assert.equal(guest.status, 403);
  for (const path of ['/api/runs', '/api/settings', '/api/market/snapshot', '/auth/login', '/api/provider/responses']) {
    assert.equal((await fetch(`${url}${path}`, { method: 'POST', headers: { Origin: url }, body: '{}' })).status, 403);
  }
  assert.equal((await fetch(`${url}/api/admin/accounts`, { headers: { Origin: 'https://cloud.example.test' } })).status, 403);
  assert.equal((await fetch(`${url}/api/runs/${account.object_id}/report`)).status, 403);
});