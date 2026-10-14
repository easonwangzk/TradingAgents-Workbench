import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { createRequire } from 'node:module';

const staticUrl = new URL('../tradingagents/workbench/static/', import.meta.url);
const require = createRequire(new URL('package.json', staticUrl));
const { JSDOM } = require('jsdom');
const { mountLocale } = await import(new URL('locale.mjs', staticUrl));
const [client, styles, serverMarkup] = await Promise.all([
  readFile(new URL('auth-studio.js', staticUrl), 'utf8'),
  readFile(new URL('auth-studio.css', staticUrl), 'utf8'),
  readFile(new URL('../tradingagents/workbench/email_server.py', import.meta.url), 'utf8'),
]);

test('auth studio mounts the magnifier scene and disposes it with the page', () => {
  assert.match(client, /import\s*\{[^}]*mountScenes[^}]*\}\s*from\s*['"]\.\/vendor\.js['"]/);
  assert.match(client, /mountScenes\(doc\)/);
  assert.match(client, /pagehide/);
  assert.match(serverMarkup, /data-scene=\\?"magnifier\\?"/);
  assert.match(serverMarkup, /scene-fallback lens-fallback/);
});

test('auth studio keeps the 44/56 desktop split and stacks art on narrow screens', () => {
  assert.match(styles, /grid-template-columns:\s*minmax\(0,\s*44fr\)\s+minmax\(0,\s*56fr\)/);
  assert.match(styles, /@media\s*\(max-width:\s*899px\)/);
  assert.match(styles, /\.auth-layout\s*\{[^}]*flex-direction:\s*column/);
  assert.match(styles, /\.studio-auth\s+\.auth-layout\s*>\s*\.auth-art\s*\{[^}]*order:\s*2/);
});

test('rendered login and registration headings follow language and preserve form values', () => {
  for (const [markup, expected] of [
    ['<html data-auth="login"><body><h2>欢迎回来</h2><input value="用户值"></body></html>', 'Welcome back'],
    ['<html data-auth="register"><body><h2>创建账户</h2><input value="用户值"></body></html>', 'Create account'],
  ]) {
    const dom = new JSDOM(markup, { url: 'https://workbench.example/auth/login' });
    const subtitle = dom.window.document.createElement('p');
    subtitle.textContent = 'RESEARCH STUDIO / 研究工作台';
    dom.window.document.body.append(subtitle);
    Object.defineProperty(dom.window.navigator, 'language', { value: 'en-US' });
    const locale = mountLocale(dom.window.document);
    assert.equal(dom.window.document.querySelector('h2').textContent, expected);
    assert.equal(subtitle.textContent, 'RESEARCH STUDIO');
    assert.equal(dom.window.document.querySelector('input').value, '用户值');
    locale.set('zh');
    assert.equal(subtitle.textContent, 'RESEARCH STUDIO / 研究工作台');
    assert.equal(dom.window.document.querySelector('input').value, '用户值');
    dom.window.close();
  }
});

test('reset and redeem route titles and initial form copy translate without changing token inputs', () => {
  for (const [route, name, englishTitle] of [['reset', '重置密码', 'Reset password'], ['redeem', '兑换邀请', 'Redeem invitation']]) {
    const dom = new JSDOM(`<html data-auth="login" data-auth-route="${route}"><head><title>${name}</title></head><body><h1>${name}</h1><h2>${name}</h2><p>账户安全</p><p>请使用管理员提供的专属链接继续</p><p>正在验证链接…</p><label>新密码<input value="secret-value"></label><button>提交</button></body></html>`, { url: `https://workbench.example/auth/${route}` });
    Object.defineProperty(dom.window.navigator, 'language', { value: 'en-US' });
    const locale = mountLocale(dom.window.document);
    assert.equal(dom.window.document.title, `${englishTitle} · TradingAgents`);
    assert.equal(dom.window.document.querySelector('h1').textContent, englishTitle);
    assert.equal(dom.window.document.querySelector('input').value, 'secret-value');
    assert.match(dom.window.document.body.textContent, /Use the personal link provided by your administrator/);
    locale.set('zh');
    assert.equal(dom.window.document.title, `${name} · TradingAgents`);
    assert.equal(dom.window.document.querySelector('h1').textContent, name);
    dom.window.close();
  }
});

test('login heading motion has reduced-motion and hidden-page gates', () => {
  assert.match(styles, /html\[data-auth="login"\] \.studio-auth \.auth-identity h1\s*\{\s*animation: auth-title-float/);
  assert.match(styles, /@keyframes auth-title-float\s*\{[^}]*transform: translateY/);
  assert.match(styles, /html\[data-page-hidden="true"\] \.studio-auth \.auth-identity h1\s*\{\s*animation: none/);
  assert.match(styles, /@media \(prefers-reduced-motion: reduce\)\s*\{\s*\.studio-auth \.auth-identity h1\s*\{\s*animation: none/);
});

test('password visibility accessible label follows selected interface language', async context => {
  const dom = new JSDOM('<html data-auth="login"><body><div data-studio-tools></div><main><input id="password" type="password"><button id="password-toggle" aria-label="显示密码" type="button"></button></main></body></html>', { url: 'https://workbench.example/auth/login', pretendToBeVisual: true });
  context.after(() => dom.window.close());
  Object.defineProperty(dom.window.navigator, 'language', { value: 'en-US' });
  const previous = globalThis.document;
  globalThis.document = dom.window.document;
  context.after(() => { globalThis.document = previous; });
  await import(new URL(`auth-studio.js?toggle-test=${Date.now()}`, staticUrl));
  dom.window.document.dispatchEvent(new dom.window.Event('DOMContentLoaded'));
  const toggle = dom.window.document.querySelector('#password-toggle');
  const language = dom.window.document.querySelector('#appearance-language');
  const switchTo = value => { language.value = value; language.dispatchEvent(new dom.window.Event('change', { bubbles: true })); };
  toggle.click();
  assert.equal(toggle.getAttribute('aria-label'), 'Hide password');
  switchTo('zh');
  assert.equal(toggle.getAttribute('aria-label'), '隐藏密码');
  assert.equal(toggle.title, '隐藏密码');
  assert.equal(dom.window.document.querySelector('#password').type, 'text');
  switchTo('en');
  assert.equal(toggle.getAttribute('aria-label'), 'Hide password');
  toggle.click();
  assert.equal(toggle.getAttribute('aria-label'), 'Show password');
  switchTo('zh');
  assert.equal(toggle.getAttribute('aria-label'), '显示密码');
  assert.equal(toggle.title, '显示密码');
  dom.window.dispatchEvent(new dom.window.Event('pagehide'));
});

test('auth visual adaptation leaves the live form IDs and endpoints intact', () => {
  for (const contract of [
    'id=\\"email-login-form\\"', 'action=\\"/auth/login\\"',
    'id=\\"email-register-code-form\\"', 'action=\\"/auth/register/code\\"',
    'id=\\"email-register-form\\"', 'action=\\"/auth/register\\"',
    'id=\\"password-confirm\\"', 'id=\\"auth-caps-lock\\"',
  ]) assert.ok(serverMarkup.includes(contract), `missing preserved auth markup: ${contract}`);
  assert.doesNotMatch(client, /layout\.append|row\.append/);
});

test('login initializes and submits JSON without resolving any visual module', async context => {
  const auth = await import(new URL('account-ui.js', staticUrl));
  const dom = new JSDOM('<form id="email-login-form"><input id="email"><input id="password" type="password"><button type="submit" disabled>Login</button><p id="auth-message" hidden></p></form>', { url: 'https://workbench.example/auth/login' });
  context.after(() => dom.window.close());
  const calls = [];
  dom.window.fetch = async (url, options) => { calls.push({ url, options }); return { ok: false, status: 401 }; };
  auth.mountAuthPage(dom.window.document);
  const form = dom.window.document.querySelector('form');
  const submit = form.querySelector('button');
  assert.equal(submit.disabled, false);
  dom.window.document.getElementById('email').value = 'fixture@example.test';
  dom.window.document.getElementById('password').value = 'Fixture-Password-Only';
  assert.equal(form.dispatchEvent(new dom.window.Event('submit', { cancelable: true })), false);
  assert.equal(submit.disabled, true);
  form.dispatchEvent(new dom.window.Event('submit', { cancelable: true }));
  await new Promise(setImmediate);
  assert.equal(calls.length, 1);
  assert.equal(calls[0].url, '/auth/login');
  assert.equal(calls[0].options.headers['Content-Type'], 'application/json');
  assert.equal(calls[0].options.credentials, 'same-origin');
  assert.deepEqual(JSON.parse(calls[0].options.body), { email: 'fixture@example.test', password: 'Fixture-Password-Only' });
  assert.equal(dom.window.document.getElementById('password').value, '');
  assert.equal(submit.disabled, false);
  dom.window.dispatchEvent(new dom.window.Event('pagehide'));
  assert.equal(submit.disabled, true);
});

test('login error uses selected UI language without changing submitted credentials', async context => {
  const { mountAuthPage } = await import(new URL('account-ui.js', staticUrl));
  const dom = new JSDOM('<html data-auth="login"><body><form id="email-login-form"><input id="email"><input id="password" type="password"><button type="submit" disabled>登录工作台</button></form><p id="auth-message" hidden></p></body></html>', { url: 'https://workbench.example/auth/login' });
  context.after(() => dom.window.close());
  Object.defineProperty(dom.window.navigator, 'language', { value: 'en-US' });
  dom.window.fetch = async () => ({ ok: false, status: 401 });
  const locale = mountLocale(dom.window.document);
  mountAuthPage(dom.window.document);
  dom.window.document.getElementById('email').value = 'owner@example.test';
  dom.window.document.getElementById('password').value = 'private-value';
  dom.window.document.querySelector('form').dispatchEvent(new dom.window.Event('submit', { cancelable: true }));
  await new Promise(setImmediate);
  assert.match(dom.window.document.getElementById('auth-message').textContent, /sign in again/i);
  assert.equal(dom.window.document.getElementById('email').value, 'owner@example.test');
  locale.set('zh');
  assert.match(dom.window.document.getElementById('auth-message').textContent, /重新登录/);
});

test('registration enables only available forms after dependency-free initialization', async context => {
  const auth = await import(new URL('account-ui.js', staticUrl));
  for (const available of [true, false]) {
    const dom = new JSDOM(`<section data-registration-available="${available}"><form id="email-register-code-form"><input id="email"><button id="register-send-code" type="submit" disabled><span>Send</span></button></form><form id="email-register-form"><input id="code"><input id="password" type="password"><input id="password-confirm" type="password"><button id="email-register-submit" type="submit" disabled>Register</button></form><p id="auth-message" hidden></p><p id="register-status"></p></section>`, { url: 'https://workbench.example/auth/register' });
    context.after(() => dom.window.close());
    auth.mountAuthPage(dom.window.document);
    assert.equal(dom.window.document.getElementById('register-send-code').disabled, !available);
    assert.equal(dom.window.document.getElementById('email-register-submit').disabled, !available);
  }
});

test('reset and redeem link previews translate on live switch without altering the email', async context => {
  const { mountAuthPage } = await import(new URL('account-ui.js', staticUrl));
  for (const route of ['reset', 'redeem']) {
    for (const valid of [true, false]) {
      const fragment = valid ? '#token=private&email=guest%2Bqa%40example.test' : '';
      const dom = new JSDOM(`<html data-auth="${route}"><body><form id="email-${route}-form"><input id="password"><button type="submit"></button></form><p id="auth-fragment-status"></p><p id="auth-message"></p></body></html>`, { url: `https://example.test/auth/${route}${fragment}` });
      context.after(() => dom.window.close());
      Object.defineProperty(dom.window.navigator, 'language', { value: 'en-US' });
      const locale = mountLocale(dom.window.document);
      mountAuthPage(dom.window.document);
      const status = dom.window.document.querySelector('#auth-fragment-status');
      assert.match(status.textContent, valid ? /link loaded: guest\+qa@example\.test/ : /invalid or expired/i);
      assert.equal(dom.window.location.hash, '');
      locale.set('zh');
      assert.match(status.textContent, valid ? /链接已载入：guest\+qa@example\.test/ : /链接无效或已失效/);
      locale.set('en');
      assert.match(status.textContent, valid ? /link loaded: guest\+qa@example\.test/ : /invalid or expired/i);
      dom.window.dispatchEvent(new dom.window.Event('pagehide'));
    }
  }
});

test('registration cooldown translates immediately and at next tick without resending', async context => {
  const { mountAuthPage } = await import(new URL('account-ui.js', staticUrl));
  const dom = new JSDOM('<html data-auth="register"><body><section data-registration-available="true"><form id="email-register-code-form"><input id="email"><button id="register-send-code" type="submit"><span>发送验证码</span></button></form><form id="email-register-form"><input id="code"><input id="password"><input id="password-confirm"><button id="email-register-submit" type="submit"></button></form><p id="auth-message"></p><p id="register-status"></p></section></body></html>', { url: 'https://example.test/auth/register' });
  context.after(() => dom.window.close());
  Object.defineProperty(dom.window.navigator, 'language', { value: 'en-US' });
  const scheduled = [];
  dom.window.setTimeout = task => { scheduled.push(task); return scheduled.length; };
  dom.window.clearTimeout = () => {};
  let sends = 0;
  dom.window.fetch = async () => { sends += 1; return { ok: true, json: async () => ({ retry_after: 3 }) }; };
  const locale = mountLocale(dom.window.document);
  mountAuthPage(dom.window.document);
  const doc = dom.window.document;
  doc.querySelector('#email').value = 'guest@example.test';
  doc.querySelector('#email-register-code-form').dispatchEvent(new dom.window.Event('submit', { cancelable: true }));
  await new Promise(setImmediate);
  const button = doc.querySelector('#register-send-code');
  assert.match(button.textContent, /3s until resend/);
  locale.set('zh');
  assert.match(button.textContent, /3s 后重发/);
  scheduled.shift()();
  assert.match(button.textContent, /2s 后重发/);
  locale.set('en');
  assert.match(button.textContent, /2s until resend/);
  assert.equal(sends, 1);
  dom.window.dispatchEvent(new dom.window.Event('pagehide'));
});
