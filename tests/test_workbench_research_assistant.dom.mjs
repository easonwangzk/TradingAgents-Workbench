/* Modified for public distribution: installation identifiers replaced with examples. */
import test from 'node:test';
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { readFile } from 'node:fs/promises';

const require = createRequire(new URL('../tradingagents/workbench/static/package.json', import.meta.url));
const domModule = await import('../tradingagents/workbench/static/research-assistant.mjs');
const { JSDOM: Jsdom } = require('jsdom');
const UUID = '11111111-1111-4111-8111-111111111111';
const flush = async () => { await new Promise(setImmediate); await new Promise(setImmediate); };

test('production page groups tasks and assistant in one navigation with history inside tasks', async () => {
  const html = await readFile(new URL('../tradingagents/workbench/static/index.html', import.meta.url), 'utf8');
  const dom = new Jsdom(html);
  const document = dom.window.document;
  assert.ok(document.querySelector('#workspace-navigation #research-tasks-entry'));
  assert.ok(document.querySelector('#workspace-navigation #research-assistant-entry'));
  assert.ok(document.querySelector('#workspace-tasks #new-research'));
  assert.ok(document.querySelector('#workspace-tasks #history-list'));
  assert.ok(document.querySelector('#workspace-home #platform-scene'));
  assert.equal(document.querySelector('#workspace-tasks').hidden, true);
  assert.equal(document.querySelector('#open-selected-report'), null);
  assert.equal(document.querySelectorAll('[data-assistant-filter]').length, 4);
  assert.ok(document.querySelector('#assistant-search'));
  assert.equal(document.querySelector('#assistant-chat').hidden, true);
  assert.ok(document.querySelector('#assistant-records .tasks-toolbar'));
  assert.equal(document.querySelector('#research-tasks-entry .count'), null);
  assert.equal(document.querySelector('#research-assistant-screen').hasAttribute('data-view'), false);
  assert.equal(document.querySelector('.vendor-details').open, false);
  assert.doesNotMatch(document.querySelector('.vendor-details summary').textContent, /6/);
  assert.equal(document.querySelector('.mode-group legend'), null);
  assert.ok(document.querySelector('#research-advanced .vendor-details'));
  assert.equal(document.querySelector('#sidebar #history-list'), null);
  assert.equal(document.querySelector('.breadcrumb button'), null);
  assert.ok(document.querySelector('#workspace-home #research-assistant-home-entry'));
  assert.equal(document.querySelector('[data-side-tab]'), null);
  for (const id of [
    'research-assistant-entry', 'research-assistant-screen',
    'assistant-session-list', 'assistant-messages', 'assistant-composer', 'assistant-input',
    'assistant-send', 'assistant-rename-dialog', 'assistant-rename-form',
  ]) assert.ok(dom.window.document.getElementById(id), `Missing ${id}`);
  dom.window.close();
});

function fixture(context, { failSend = false, beforeRequest = async () => {} } = {}) {
  const dom = new Jsdom(`<!doctype html><html><body><div id="workbench" data-screen="home"><main>
    <strong id="breadcrumb-studio">研究工作台</strong><button id="research-assistant-entry" hidden></button><button id="research-assistant-home-entry" hidden></button>
    <section id="research-assistant-screen" hidden><button id="assistant-close"></button>
      <div><button data-assistant-filter="all" aria-pressed="true"></button><button data-assistant-filter="active"></button><button data-assistant-filter="completed"></button><button data-assistant-filter="interrupted"></button></div><input id="assistant-search"><p id="assistant-list-empty" hidden></p>
      <button id="assistant-new"></button><div id="assistant-records"><div id="assistant-session-list"></div></div><section id="assistant-chat" hidden><button id="assistant-back" type="button"></button><div id="assistant-messages"></div>
      <p id="assistant-status"></p><form id="assistant-composer"><textarea id="assistant-input"></textarea><button id="assistant-send"></button></form>
    </section></section>
    <dialog id="assistant-rename-dialog"><form id="assistant-rename-form"><input id="assistant-rename-input"><p id="assistant-rename-status"></p><button type="submit"></button><button id="assistant-rename-cancel" type="button"></button></form></dialog>
    <div id="workspace-home"></div><div id="workspace-run"></div></main></div></body></html>`, { url: 'https://research.example.com/' });
  Object.defineProperty(dom.window.crypto, 'randomUUID', { value: () => UUID, configurable: true });
  context.after(() => dom.window.close());
  const sessions = [];
  const calls = [];
  const request = async (path, options = {}) => {
    calls.push({ path, options });
    await beforeRequest(path, options);
    if (path === '/api/assistant/sessions' && !options.method) return { sessions: sessions.map(({ messages, ...summary }) => structuredClone(summary)) };
    if (path === '/api/assistant/sessions' && options.method === 'POST') {
      const created = { id: UUID, title: '新对话', created_at: '2026-10-09T00:00:00Z', updated_at: '2026-10-09T00:00:00Z', messages: [] };
      sessions.unshift(created);
      return structuredClone(created);
    }
    if (path.endsWith('/messages')) {
      if (failSend) { const error = new Error('本月模型额度不足。'); error.status = 402; throw error; }
      const payload = JSON.parse(JSON.stringify(options.body));
      const current = sessions.find(item => item.id === UUID);
      current.title = payload.content.slice(0, 80);
      current.messages.push(
        { turn_id: payload.turn_id, role: 'user', content: payload.content, status: 'complete', created_at: '2026-10-09T00:00:01Z' },
        { turn_id: payload.turn_id, role: 'assistant', content: '<img src=x onerror=alert(1)>\nLuna answer', status: 'complete', created_at: '2026-10-09T00:00:02Z' },
      );
      return structuredClone({ ...current, answer: current.messages.at(-1).content });
    }
    if (path.endsWith('/messages') === false && options.method === 'PATCH') {
      const current = sessions.find(item => item.id === UUID);
      current.title = options.body.title;
      return structuredClone(current);
    }
    if (options.method === 'DELETE') {
      sessions.splice(0, sessions.length);
      return { deleted: true };
    }
    if (path === `/api/assistant/sessions/${UUID}`) return structuredClone(sessions.find(item => item.id === UUID));
    throw new Error(`Unexpected request ${options.method || 'GET'} ${path}`);
  };
  let opened = 0;
  let closed = 0;
  const assistant = domModule.mountResearchAssistant(dom.window.document, {
    request,
    onOpen: () => { opened += 1; },
    onClose: () => { closed += 1; },
  });
  return { assistant, calls, closed: () => closed, dom, opened: () => opened, sessions };
}

test('assistant entry stays hidden until enabled and creates a saved session', async context => {
  const app = fixture(context);
  const document = app.dom.window.document;
  assert.equal(document.querySelector('#research-assistant-entry').hidden, true);
  assert.equal(document.querySelector('#breadcrumb-studio').hidden, false);
  app.assistant.setAvailable(true);
  assert.equal(document.querySelector('#breadcrumb-studio').hidden, false);
  assert.equal(document.querySelector('#research-assistant-entry').hidden, false);
  document.querySelector('#research-assistant-entry').click();
  await flush();
  assert.equal(app.opened(), 1);
  assert.equal(document.querySelector('#research-assistant-entry').getAttribute('aria-current'), 'page');
  assert.equal(document.querySelector('#research-assistant-screen').hidden, false);
  document.querySelector('#assistant-new').click();
  await flush();
  assert.equal(app.sessions.length, 1);
  assert.equal(document.querySelector('#assistant-input').disabled, false);
  assert.equal(document.querySelector('#assistant-chat').hidden, false);
  assert.equal(document.querySelector('#assistant-records').hidden, true);
});

test('assistant renders output as text, saves rename, and deletes a selected conversation', async context => {
  const app = fixture(context);
  const document = app.dom.window.document;
  app.assistant.setAvailable(true);
  document.querySelector('#research-assistant-entry').click();
  await flush();
  document.querySelector('#assistant-new').click();
  await flush();
  const input = document.querySelector('#assistant-input');
  input.value = 'What happened in my MSFT history?';
  input.dispatchEvent(new app.dom.window.Event('input', { bubbles: true }));
  document.querySelector('#assistant-composer').dispatchEvent(new app.dom.window.Event('submit', { bubbles: true, cancelable: true }));
  await flush();
  assert.equal(document.querySelector('#assistant-messages img'), null);
  assert.match(document.querySelector('#assistant-messages').textContent, /onerror=alert/);
  assert.equal(app.sessions[0].messages.length, 2);
  assert.equal(app.calls.filter(call => call.path.endsWith('/messages')).length, 1);

  document.querySelector('.assistant-session-row button[aria-label^="重命名"]').click();
  const rename = document.querySelector('#assistant-rename-input');
  rename.value = '我的 MSFT 对话';
  document.querySelector('#assistant-rename-form').dispatchEvent(new app.dom.window.Event('submit', { bubbles: true, cancelable: true }));
  await flush();
  assert.equal(app.sessions[0].title, '我的 MSFT 对话');

  document.querySelector('.assistant-session-row button[aria-label^="删除"]').click();
  await flush();
  assert.equal(app.sessions.length, 0);
  assert.equal(document.querySelector('#assistant-chat').hidden, true);
  assert.equal(document.querySelector('#assistant-records').hidden, false);
  assert.match(document.querySelector('#assistant-messages').textContent, /新建一条/);
  document.querySelector('#assistant-close').click();
  assert.equal(app.closed(), 1);
  assert.equal(document.querySelector('#research-assistant-entry').hasAttribute('aria-current'), false);
});

test('assistant filters saved message states and searches title and content without writes', async context => {
  const app = fixture(context);
  const document = app.dom.window.document;
  app.sessions.push({ id: UUID, title: 'MSFT review', messages: [{ role: 'assistant', status: 'complete', content: 'portfolio evidence' }] });
  app.assistant.setAvailable(true);
  document.querySelector('#research-assistant-entry').click();
  await flush();
  document.querySelector('[data-assistant-filter="completed"]').click();
  assert.equal(document.querySelectorAll('.assistant-session-row').length, 1);
  document.querySelector('[data-assistant-filter="interrupted"]').click();
  assert.equal(document.querySelectorAll('.assistant-session-row').length, 0);
  document.querySelector('[data-assistant-filter="all"]').click();
  const search = document.querySelector('#assistant-search');
  search.value = 'portfolio';
  search.dispatchEvent(new app.dom.window.Event('input'));
  assert.equal(document.querySelectorAll('.assistant-session-row').length, 1);
  search.value = 'no-match';
  search.dispatchEvent(new app.dom.window.Event('input'));
  assert.equal(document.querySelectorAll('.assistant-session-row').length, 0);
  assert.equal(app.calls.some(call => call.options.method), false);
});

test('assistant opens the full record list before a conversation and returns without losing filters', async context => {
  const app = fixture(context);
  const document = app.dom.window.document;
  app.sessions.push({ id: UUID, title: 'MSFT review', updated_at: '2026-10-09T00:00:00Z', messages: [{ role: 'assistant', status: 'complete', content: 'Saved answer' }] });
  app.assistant.setAvailable(true);
  document.querySelector('#research-assistant-entry').click();
  await flush();
  assert.equal(document.querySelector('#assistant-chat').hidden, true);
  assert.equal(document.querySelector('#assistant-records').hidden, false);
  assert.equal(document.querySelector('.assistant-session-select').getAttribute('aria-current'), 'false');
  document.querySelector('[data-assistant-filter="completed"]').click();
  document.querySelector('.assistant-session-select').click();
  await flush();
  assert.equal(document.querySelector('#assistant-chat').hidden, false);
  assert.equal(document.querySelector('#assistant-records').hidden, true);
  assert.match(document.querySelector('#assistant-messages').textContent, /Saved answer/);
  document.querySelector('#assistant-back').click();
  assert.equal(document.querySelector('#assistant-chat').hidden, true);
  assert.equal(document.querySelector('#assistant-records').hidden, false);
  assert.equal(document.querySelector('[data-assistant-filter="completed"]').getAttribute('aria-pressed'), 'true');
  document.querySelector('#research-assistant-entry').click();
  await flush();
  assert.equal(document.querySelector('#assistant-chat').hidden, true);
});

test('late conversation creation does not replace the reopened record list', async context => {
  const pending = Promise.withResolvers();
  const app = fixture(context, { beforeRequest: async (path, options) => {
    if (path === '/api/assistant/sessions' && options.method === 'POST') await pending.promise;
  } });
  const document = app.dom.window.document;
  app.assistant.setAvailable(true);
  document.querySelector('#research-assistant-entry').click();
  await flush();
  document.querySelector('#assistant-new').click();
  await flush();
  document.querySelector('#assistant-close').click();
  document.querySelector('#research-assistant-entry').click();
  await flush();
  pending.resolve();
  await flush();
  assert.equal(document.querySelector('#assistant-chat').hidden, true);
  assert.equal(document.querySelector('#assistant-records').hidden, false);
  assert.equal(document.querySelectorAll('.assistant-session-row').length, 1);
});

test('late answer does not select a conversation after returning to the record list', async context => {
  const pending = Promise.withResolvers();
  const app = fixture(context, { beforeRequest: async path => {
    if (path.endsWith('/messages')) await pending.promise;
  } });
  const document = app.dom.window.document;
  app.assistant.setAvailable(true);
  document.querySelector('#research-assistant-entry').click();
  await flush();
  document.querySelector('#assistant-new').click();
  await flush();
  const input = document.querySelector('#assistant-input');
  input.value = 'Original question';
  document.querySelector('#assistant-composer').dispatchEvent(new app.dom.window.Event('submit', { cancelable: true }));
  await flush();
  document.querySelector('#assistant-back').click();
  document.querySelector('#assistant-close').click();
  document.querySelector('#research-assistant-entry').click();
  await flush();
  input.value = 'Retained draft';
  pending.resolve();
  await flush();
  assert.equal(document.querySelector('#assistant-chat').hidden, true);
  assert.equal(document.querySelector('.assistant-session-select').getAttribute('aria-current'), 'false');
  assert.equal(input.value, 'Retained draft');
});

for (const [filter, messages] of [
  ['active', []],
  ['active', [{ role: 'user', status: 'pending', content: 'Waiting' }]],
  ['completed', [{ role: 'assistant', status: 'complete', content: 'Answer' }]],
  ['interrupted', [{ role: 'user', status: 'error', content: 'Failed turn' }]],
]) test(`assistant ${filter} filter uses saved state with ${messages.at(-1)?.status || 'empty'} messages`, async context => {
  const app = fixture(context);
  app.sessions.push({ id: UUID, title: 'Saved conversation', messages });
  app.assistant.setAvailable(true);
  const document = app.dom.window.document;
  document.querySelector('#research-assistant-entry').click();
  await flush();
  document.querySelector(`[data-assistant-filter="${filter}"]`).click();
  assert.equal(document.querySelectorAll('.assistant-session-row').length, 1);
  const otherFilter = filter === 'completed' ? 'active' : 'completed';
  document.querySelector(`[data-assistant-filter="${otherFilter}"]`).click();
  assert.equal(document.querySelectorAll('.assistant-session-row').length, 0);
});

test('assistant reports Luna budget failure without dropping the draft message', async context => {
  const app = fixture(context, { failSend: true });
  const document = app.dom.window.document;
  app.assistant.setAvailable(true);
  document.querySelector('#research-assistant-entry').click();
  await flush();
  document.querySelector('#assistant-new').click();
  await flush();
  const input = document.querySelector('#assistant-input');
  input.value = 'Summarize MSFT';
  document.querySelector('#assistant-composer').dispatchEvent(new app.dom.window.Event('submit', { bubbles: true, cancelable: true }));
  await flush();
  assert.match(document.querySelector('#assistant-status').textContent, /额度不足/);
  assert.equal(app.calls.filter(call => call.path.endsWith('/messages')).length, 1);
});
