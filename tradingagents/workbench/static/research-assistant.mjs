import { onLocaleChange, translate } from './locale.mjs';

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;

export function mountResearchAssistant(doc, { request, paintIcons = () => {}, onOpen = () => {}, onClose = () => {} }) {
  const win = doc.defaultView;
  const byId = id => doc.getElementById(id);
  const entry = byId('research-assistant-entry');
  const homeEntry = byId('research-assistant-home-entry');
  const screen = byId('research-assistant-screen');
  const sessionList = byId('assistant-session-list');
  const conversation = byId('assistant-messages');
  const status = byId('assistant-status');
  const composer = byId('assistant-composer');
  const input = byId('assistant-input');
  const sendButton = byId('assistant-send');
  const newButton = byId('assistant-new');
  const closeButton = byId('assistant-close');
  const renameDialog = byId('assistant-rename-dialog');
  const renameInput = byId('assistant-rename-input');
  const renameForm = byId('assistant-rename-form');
  const renameCancel = byId('assistant-rename-cancel');
  const renameStatus = byId('assistant-rename-status');
  const state = { available: false, open: false, loading: false, sending: false, sessions: [], selected: null, generation: 0, filter: 'all', query: '', sendingId: null, view: 'list' };
  const listeners = [];
  const on = (target, event, callback) => {
    target.addEventListener(event, callback);
    listeners.push(() => target.removeEventListener(event, callback));
  };
  const node = (tag, text, className) => {
    const item = doc.createElement(tag);
    if (text !== undefined) item.textContent = text;
    if (className) item.className = className;
    return item;
  };
  const setStatus = (value, error = false) => {
    status.textContent = value || '';
    status.dataset.error = String(error);
  };
  const api = async (path, options) => {
    try {
      return await request(path, options);
    } catch (error) {
      if (error.status === 401) setStatus(translate('登录已失效，请重新登录。'), true);
      else if (error.status === 429) setStatus(translate('对话数量或用量达到限制。'), true);
      else if (error.status === 402) setStatus(translate('本月模型额度不足。'), true);
      else setStatus(error.message || translate('研究助手暂时不可用，请稍后重试。'), true);
      throw error;
    }
  };
  const selectedSession = () => state.sessions.find(item => item.id === state.selected) || null;
  const setView = view => {
    state.view = view;
    screen.dataset.assistantView = view;
    if (byId('assistant-records')) byId('assistant-records').hidden = view !== 'list';
    if (byId('assistant-chat')) byId('assistant-chat').hidden = view !== 'chat';
  };
  const sessionStatus = session => {
    if (state.sendingId === session.id) return 'active';
    if (!Array.isArray(session.messages)) return 'unknown';
    const last = session.messages.at(-1);
    if (last?.status === 'error') return 'interrupted';
    return last?.role === 'assistant' && last.status === 'complete' ? 'completed' : 'active';
  };

  function renderSessions() {
    sessionList.replaceChildren();
    const query = state.query.trim().toLocaleLowerCase();
    const sessions = state.sessions.filter(session => (state.filter === 'all' || sessionStatus(session) === state.filter)
      && (!query || [session.title, ...(session.messages || []).map(message => message.content)].join(' ').toLocaleLowerCase().includes(query)));
    for (const session of sessions) {
      const row = node('div', undefined, 'assistant-session-row');
      const select = node('button', session.title, 'assistant-session-select');
      select.type = 'button';
      select.setAttribute('aria-current', String(session.id === state.selected));
      select.title = session.title;
      on(select, 'click', () => { void openSession(session.id); });
      const rename = node('button', undefined, 'icon-button');
      rename.type = 'button';
      rename.setAttribute('aria-label', `${translate('重命名对话')} ${session.title}`);
      rename.title = translate('重命名对话');
      rename.append(node('i'));
      rename.firstElementChild.setAttribute('data-lucide', 'pencil');
      on(rename, 'click', () => beginRename(session));
      const remove = node('button', undefined, 'icon-button');
      remove.type = 'button';
      remove.setAttribute('aria-label', `${translate('删除对话')} ${session.title}`);
      remove.title = translate('删除对话');
      remove.append(node('i'));
      remove.firstElementChild.setAttribute('data-lucide', 'trash-2');
      on(remove, 'click', () => { void deleteSession(session); });
      const statusLabel = { active: '进行中', completed: '已完成', interrupted: '已中断', unknown: '暂不可用' }[sessionStatus(session)];
      const badge = node('span', translate(statusLabel), 'assistant-session-status');
      badge.dataset.status = sessionStatus(session);
      const timestamp = session.updated_at || session.created_at;
      const date = timestamp ? new Date(timestamp) : null;
      const updated = node('time', date && !Number.isNaN(date.getTime()) ? date.toLocaleString() : '--', 'assistant-session-date');
      if (date && !Number.isNaN(date.getTime())) updated.dateTime = date.toISOString();
      row.append(select, updated, badge, rename, remove);
      sessionList.append(row);
    }
    if (!sessions.length) sessionList.append(node('p', translate('暂无匹配的对话'), 'empty-inline'));
    paintIcons(sessionList);
  }

  function renderConversation() {
    conversation.replaceChildren();
    const current = selectedSession();
    if (!current) {
      conversation.append(node('p', translate('选择已有对话，或新建一条开始。'), 'assistant-empty'));
      input.disabled = true;
      sendButton.disabled = true;
      return;
    }
    for (const message of current.messages || []) {
      const article = node('article', undefined, `assistant-message assistant-message-${message.role}`);
      article.append(
        node('span', translate(message.role === 'user' ? '你' : '研究助手'), 'assistant-message-label'),
        node('p', message.content, 'assistant-message-content'),
      );
      conversation.append(article);
    }
    if (state.sending) conversation.append(node('p', translate('研究助手正在整理回答…'), 'assistant-pending'));
    input.disabled = state.sending;
    sendButton.disabled = state.sending || !input.value.trim();
    conversation.scrollTop = conversation.scrollHeight;
  }

  async function loadSessions() {
    if (!state.available) return;
    const generation = ++state.generation;
    state.loading = true;
    setStatus(translate('正在读取已保存的对话…'));
    try {
      const result = await api('/api/assistant/sessions');
      if (!state.open || generation !== state.generation) return;
      const sessions = await Promise.all(result.sessions.slice(0, 12).map(async session => {
        try { return { ...session, ...await request(`/api/assistant/sessions/${session.id}`) }; }
        catch { return session; }
      }));
      if (!state.open || generation !== state.generation) return;
      state.sessions = sessions;
      renderSessions();
      renderConversation();
      setStatus('');
    } catch {
      if (state.open && generation === state.generation) {
        state.sessions = [];
        state.selected = null;
        renderSessions();
        renderConversation();
      }
    } finally {
      state.loading = false;
    }
  }

  async function openSession(id) {
    if (!UUID.test(id) || state.loading && !state.sessions.some(item => item.id === id)) return;
    const generation = ++state.generation;
    state.selected = id;
    setView('chat');
    renderSessions();
    renderConversation();
    setStatus(translate('正在读取对话…'));
    try {
      const result = await api(`/api/assistant/sessions/${id}`);
      if (!state.open || generation !== state.generation) return;
      state.sessions = state.sessions.map(item => item.id === id ? { ...item, ...result } : item);
      renderSessions();
      renderConversation();
      setStatus('');
      input.focus();
    } catch {}
  }

  async function createSession() {
    if (state.loading || state.sending) return;
    const generation = state.generation;
    state.loading = true;
    newButton.disabled = true;
    setStatus(translate('正在新建对话…'));
    try {
      const created = await api('/api/assistant/sessions', { method: 'POST', body: {} });
      state.sessions = [{ ...created, messages: created.messages || [] }, ...state.sessions.filter(session => session.id !== created.id)];
      if (!state.open || generation !== state.generation) { renderSessions(); return; }
      state.filter = 'all';
      state.query = '';
      if (byId('assistant-search')) byId('assistant-search').value = '';
      for (const filter of doc.querySelectorAll('[data-assistant-filter]')) filter.setAttribute('aria-pressed', String(filter.dataset.assistantFilter === 'all'));
      state.selected = created.id;
      setView('chat');
      renderSessions();
      renderConversation();
      setStatus('');
      input.focus();
    } catch {} finally {
      state.loading = false;
      newButton.disabled = false;
    }
  }

  async function deleteSession(session) {
    if (state.sending) return;
    try {
      await api(`/api/assistant/sessions/${session.id}`, { method: 'DELETE' });
      state.sessions = state.sessions.filter(item => item.id !== session.id);
      if (state.selected === session.id) { state.selected = null; setView('list'); }
      renderSessions();
      renderConversation();
      setStatus('');
    } catch {}
  }

  function beginRename(session) {
    renameInput.value = session.title;
    renameStatus.textContent = '';
    renameDialog.dataset.sessionId = session.id;
    if (typeof renameDialog.showModal === 'function') renameDialog.showModal();
    else renameDialog.setAttribute('open', '');
    renameInput.focus();
    renameInput.select();
  }

  async function renameSession(event) {
    event.preventDefault();
    const id = renameDialog.dataset.sessionId;
    const title = renameInput.value.trim();
    const button = renameForm.querySelector('[type="submit"]');
    if (!title || title.length > 80 || !UUID.test(id || '')) {
      renameStatus.textContent = translate('请输入 1 到 80 个字符的标题。');
      return;
    }
    button.disabled = true;
    try {
      const renamed = await api(`/api/assistant/sessions/${id}`, { method: 'PATCH', body: { title } });
      state.sessions = state.sessions.map(item => item.id === id ? { ...item, ...renamed } : item);
      renderSessions();
      if (typeof renameDialog.close === 'function') renameDialog.close();
      else renameDialog.removeAttribute('open');
      if (state.selected === id) setStatus('');
    } catch (error) { renameStatus.textContent = error.message; }
    finally { button.disabled = false; }
  }

  async function send(event) {
    event.preventDefault();
    const session = selectedSession();
    const content = input.value.trim();
    if (!session || !content || state.sending || content.length > 4000) return;
    const generation = state.generation;
    state.sending = true;
    state.sendingId = session.id;
    renderSessions();
    setStatus(translate('正在使用 GPT-5.6 Luna 回答…'));
    renderConversation();
    try {
      const turnId = win.crypto.randomUUID();
      const result = await api(`/api/assistant/sessions/${session.id}/messages`, { method: 'POST', body: { turn_id: turnId, content } });
      state.sessions = state.sessions.map(item => item.id === result.id ? { ...item, ...result } : item);
      if (state.open && generation === state.generation && state.view === 'chat' && state.selected === session.id) {
        state.selected = result.id;
        input.value = '';
      }
      renderSessions();
      renderConversation();
      setStatus(translate('回答已保存。'));
    } catch {
      try {
        const saved = await request(`/api/assistant/sessions/${session.id}`);
        state.sessions = state.sessions.map(item => item.id === saved.id ? { ...item, ...saved } : item);
      } catch {}
      renderConversation();
    } finally {
      state.sending = false;
      state.sendingId = null;
      renderSessions();
      renderConversation();
      sendButton.disabled = !input.value.trim() || !state.selected;
    }
  }

  async function open() {
    if (!state.available) return;
    state.open = true;
    state.selected = null;
    setView('list');
    entry.setAttribute('aria-current', 'page');
    screen.hidden = false;
    doc.documentElement.dataset.screen = 'assistant';
    byId('workbench').dataset.screen = 'assistant';
    byId('workspace-home').hidden = true;
    if (byId('workspace-tasks')) byId('workspace-tasks').hidden = true;
    byId('workspace-run').hidden = true;
    onOpen();
    await loadSessions();
  }

  function close() {
    state.open = false;
    state.generation += 1;
    screen.hidden = true;
    entry.removeAttribute('aria-current');
    onClose();
  }

  function setAvailable(available) {
    state.available = Boolean(available);
    entry.hidden = !state.available;
    if (homeEntry) homeEntry.hidden = !state.available;
    if (!state.available && state.open) close();
  }

  on(entry, 'click', () => { void open(); });
  if (homeEntry) on(homeEntry, 'click', () => { void open(); });
  for (const filter of doc.querySelectorAll('[data-assistant-filter]')) on(filter, 'click', () => {
    state.filter = filter.dataset.assistantFilter;
    for (const button of doc.querySelectorAll('[data-assistant-filter]')) button.setAttribute('aria-pressed', String(button === filter));
    renderSessions();
  });
  if (byId('assistant-search')) on(byId('assistant-search'), 'input', event => { state.query = event.target.value; renderSessions(); });
  on(closeButton, 'click', close);
  if (byId('assistant-back')) on(byId('assistant-back'), 'click', () => {
    state.generation += 1;
    setView('list');
    renderSessions();
    byId('assistant-search')?.focus();
  });
  on(newButton, 'click', () => { void createSession(); });
  on(composer, 'submit', event => { void send(event); });
  on(input, 'input', () => { sendButton.disabled = !input.value.trim() || state.sending || !state.selected; });
  on(renameForm, 'submit', event => { void renameSession(event); });
  on(renameCancel, 'click', () => {
    if (typeof renameDialog.close === 'function') renameDialog.close();
    else renameDialog.removeAttribute('open');
  });
  on(doc, 'keydown', event => {
    if (event.key === 'Escape' && state.open && !renameDialog.open) {
      close();
      entry.focus();
    }
  });
  const unsubscribeLocale = onLocaleChange(() => { if (state.open) { renderSessions(); renderConversation(); } });
  renderSessions();
  renderConversation();

  return {
    close,
    dispose() { state.open = false; listeners.forEach(remove => remove()); unsubscribeLocale(); },
    open,
    setAvailable,
  };
}
