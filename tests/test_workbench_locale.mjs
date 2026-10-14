import test from 'node:test';
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { formatDate, formatNumber, mountLocale, translate } from '../tradingagents/workbench/static/locale.mjs';
import { mountEmailAccount } from '../tradingagents/workbench/static/account-ui.js';
import { mountModelSettings } from '../tradingagents/workbench/static/model-settings.js';

const require = createRequire(new URL('../tradingagents/workbench/static/package.json', import.meta.url));
const { JSDOM } = require('jsdom');

test('browser defaults, invalid saved choice and denied storage resolve safely', () => {
  for (const [browser, saved, expected] of [['zh-HK', 'bogus', 'zh-CN'], ['en-GB', 'bogus', 'en'], ['fr-FR', null, 'en']]) {
    const dom = new JSDOM('<title>TradingAgents</title><body><button>新建研究</button></body>', { url: 'https://example.test' });
    Object.defineProperty(dom.window.navigator, 'language', { value: browser });
    if (saved) dom.window.localStorage.setItem('tradingagents.studio.language', saved);
    if (!saved) Object.defineProperty(dom.window, 'localStorage', { get() { throw new Error('denied'); } });
    const locale = mountLocale(dom.window.document);
    assert.equal(dom.window.document.documentElement.lang, expected);
    assert.equal(locale.preference, 'auto');
    assert.equal(dom.window.document.querySelector('button').textContent, expected === 'en' ? 'New research' : '新建研究');
    locale.set('zh');
    assert.equal(dom.window.document.documentElement.lang, 'zh-CN');
    dom.window.close();
  }
});

test('explicit locale persists on fresh login and workspace independently of report data', () => {
  const dom = new JSDOM('<title>TradingAgents</title><body><h2>欢迎回来</h2><input value="报告语言"><pre>新建研究</pre></body>', { url: 'https://example.test' });
  Object.defineProperty(dom.window.navigator, 'language', { value: 'en-US' });
  const locale = mountLocale(dom.window.document);
  assert.equal(dom.window.document.querySelector('h2').textContent, 'Welcome back');
  locale.set('zh');
  assert.equal(dom.window.localStorage.getItem('tradingagents.studio.language'), 'zh');
  assert.equal(dom.window.document.querySelector('input').value, '报告语言');
  assert.equal(dom.window.document.querySelector('pre').textContent, '新建研究');
  assert.equal(translate('欢迎回来'), '欢迎回来');
  dom.window.document.querySelector('h2').textContent = '欢迎回来';
  const fresh = mountLocale(dom.window.document);
  assert.equal(fresh.preference, 'zh');
  assert.equal(dom.window.document.querySelector('h2').textContent, '欢迎回来');
  fresh.set('en');
  assert.equal(dom.window.localStorage.getItem('tradingagents.studio.language'), 'en');
  const newPage = new JSDOM('<body><button>新建研究</button></body>', { url: 'https://example.test' });
  Object.defineProperty(newPage.window.navigator, 'language', { value: 'zh-CN' });
  newPage.window.localStorage.setItem('tradingagents.studio.language', dom.window.localStorage.getItem('tradingagents.studio.language'));
  assert.equal(mountLocale(newPage.window.document).preference, 'en');
  assert.equal(newPage.window.document.querySelector('button').textContent, 'New research');
  assert.equal(formatNumber(12345), '12,345');
  assert.match(formatDate('2026-09-08T08:00:00Z', { timeZone: 'UTC', year: 'numeric' }), /2026/);
  newPage.window.close();
  dom.window.close();
});

test('account and model settings dialogs switch live without changing settings inputs', async () => {
  const dom = new JSDOM('<body><div class="topbar-end"><span class="local-label"></span></div><section id="side-config"><form id="config-form"></form><div id="research-advanced"></div></section></body>', { url: 'https://example.test' });
  Object.defineProperty(dom.window.navigator, 'language', { value: 'en-US' });
  const locale = mountLocale(dom.window.document);
  const account = mountEmailAccount(dom.window.document, { request: async () => ({}), onSession() {}, onLogout() {} });
  const settings = mountModelSettings(dom.window.document, {
    request: async path => ({
      '/api/settings/catalog': { providers: { openai: { label: 'OpenAI', models: { quick: [{ label: 'gpt-mini', value: 'gpt-mini' }], deep: [{ label: 'gpt-deep', value: 'gpt-deep' }] } } }, data_vendors: {} },
      '/api/settings': { provider: 'openai', mode: 'api_key', quick_model: 'gpt-mini', deep_model: 'gpt-deep', asset_type: 'stock', data_vendors: {}, model_key_configured: true, providers: { openai: { quick_model: 'gpt-mini', deep_model: 'gpt-deep', key_configured: true } }, data_key_configured: { alpha_vantage: true, fred: false } },
    })[path],
    readDraft: () => ({ asset_type: 'stock', data_vendors: {} }), today: '2026-09-30', onSaved() {},
  });
  await settings.refresh({ csrfToken: 'test-token', user: { is_admin: false } });
  const doc = dom.window.document;
  assert.equal(doc.querySelector('#account-heading').textContent, 'Account and security');
  assert.equal(doc.querySelector('.model-settings-summary strong').textContent, 'Models and credentials');
  const dataState = doc.querySelector('#settings-alpha-state');
  assert.equal(dataState.textContent, 'Configured');
  assert.equal(doc.querySelector('#settings-revision').textContent, 'Not saved');
  assert.match(doc.querySelector('#settings-summary-status').textContent, /Key configured/);
  assert.equal(doc.querySelector('#settings-quick-model').value, 'gpt-mini');
  account.setSession({ user: { email: 'person@example.test', is_admin: true } });
  account.setBudget({ month: '2026-09', limit_microusd: 1000000, reset_at: '2026-10-01T00:00:00Z' });
  assert.match(doc.querySelector('#account-role').textContent, /Administrator/);
  assert.match(doc.querySelector('#budget-reset').textContent, /reset/i);
  doc.querySelector('#settings-api-key').value = 'do-not-translate';
  locale.set('zh');
  assert.equal(doc.querySelector('#account-heading').textContent, '账户与安全');
  assert.equal(dataState.textContent, '已配置');
  assert.equal(doc.querySelector('#settings-quick-model').value, 'gpt-mini');
  assert.match(doc.querySelector('#account-role').textContent, /管理员/);
  assert.equal(doc.querySelector('#account-email').textContent, 'person@example.test');
  assert.equal(doc.querySelector('#settings-api-key').value, 'do-not-translate');
  assert.match(doc.querySelector('#settings-summary-status').textContent, /已配置密钥/);
  locale.set('en');
  assert.match(doc.querySelector('#settings-summary-status').textContent, /Key configured/);
  assert.equal(doc.querySelector('#settings-api-key').value, 'do-not-translate');
  account.dispose(); settings.dispose(); dom.window.close();
});

test('late admin rows and invitation actions follow live locale without network refresh', async () => {
  const dom = new JSDOM('<body><div class="topbar-end"><span class="local-label"></span></div></body>', { url: 'https://example.test' });
  Object.defineProperty(dom.window.navigator, 'language', { value: 'en-US' });
  const locale = mountLocale(dom.window.document);
  const calls = [];
  const account = mountEmailAccount(dom.window.document, {
    request: async (path, options) => {
      calls.push(path);
      if (path === '/api/admin/accounts') return { version: 'accounts-v1', accounts: [{ object_id: 'guest-1', email: 'guest@example.test', is_admin: false, disabled: false }] };
      if (path === '/api/admin/approved-origins') return { version: 'v1', approved_origins: [] };
      if (path === '/api/admin/recoveries') return { token_id: 'token-1', email: 'guest@example.test', fragment: '/auth/reset#token=private&email=guest%40example.test' };
      return { month: '2026-09' };
    }, onSession() {}, onLogout() {},
  });
  const doc = dom.window.document;
  account.setSession({ user: { email: 'owner@example.test', is_admin: true } });
  doc.querySelector('#account-panel-toggle').click();
  await new Promise(setImmediate);
  assert.match(doc.querySelector('#user-list').textContent, /Reset link.*Disable/);
  doc.querySelector('#user-list button').click();
  await new Promise(setImmediate);
  assert.match(doc.querySelector('#invite-result').textContent, /Copy link.*Revoke invitation/);
  doc.querySelector('#approved-origins').value = 'https://draft.example.test';
  const before = calls.length;
  locale.set('zh');
  assert.match(doc.querySelector('#user-list').textContent, /重置链接.*禁用/);
  assert.match(doc.querySelector('#invite-result').textContent, /复制链接.*撤销邀请/);
  locale.set('en');
  assert.match(doc.querySelector('#invite-result').textContent, /Copy link.*Revoke invitation/);
  assert.equal(doc.querySelectorAll('#invite-result a').length, 1);
  assert.equal(doc.querySelectorAll('#invite-result button').length, 2);
  assert.equal(doc.querySelector('#invite-result a').getAttribute('href'), 'https://example.test/auth/reset#token=private&email=guest%40example.test');
  assert.equal(doc.querySelector('#approved-origins').value, 'https://draft.example.test');
  assert.equal(calls.length, before);
  doc.querySelector('#account-close').click();
  locale.set('zh');
  assert.equal(doc.querySelector('#user-list').textContent, '');
  assert.equal(doc.querySelector('#invite-result').textContent, '');
  account.dispose(); dom.window.close();
});

test('admin error copy follows locale without another request', async () => {
  const dom = new JSDOM('<body><div class="topbar-end"><span class="local-label"></span></div></body>', { url: 'https://example.test' });
  Object.defineProperty(dom.window.navigator, 'language', { value: 'en-US' });
  const locale = mountLocale(dom.window.document);
  let calls = 0;
  const account = mountEmailAccount(dom.window.document, {
    request: async () => { calls += 1; throw new Error('账户信息加载失败。'); }, onSession() {}, onLogout() {},
  });
  const doc = dom.window.document;
  account.setSession({ user: { email: 'owner@example.test', is_admin: true } });
  doc.querySelector('#account-panel-toggle').click();
  await new Promise(setImmediate);
  assert.equal(doc.querySelector('#account-error').textContent, 'Could not load account information.');
  locale.set('zh');
  assert.equal(doc.querySelector('#account-error').textContent, '账户信息加载失败。');
  assert.equal(calls, 1);
  account.dispose(); dom.window.close();
});