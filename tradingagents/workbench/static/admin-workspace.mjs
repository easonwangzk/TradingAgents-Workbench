import { bindLocale, locale, onLocaleChange, translate } from './locale.mjs';

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
const FEATURES = ['research', 'assistant', 'market'];
const validVersion = value => typeof value === 'string' && value.length > 0 && value.length <= 256;
const object = value => value !== null && typeof value === 'object' && !Array.isArray(value);
const validPermissions = value => object(value) && FEATURES.every(key => typeof value[key] === 'boolean');
const validAccount = value => object(value) && UUID.test(value.object_id) && typeof value.email === 'string'
  && typeof value.name === 'string' && typeof value.is_admin === 'boolean' && typeof value.disabled === 'boolean' && validPermissions(value.permissions);
const validCoverage = value => object(value) && (value.since === null || typeof value.since === 'string')
  && Array.isArray(value.notes) && value.notes.every(note => typeof note === 'string') && typeof value.truncated === 'boolean';
const safeAccount = value => ({ object_id: value.object_id, email: value.email, name: value.name, is_admin: value.is_admin, disabled: value.disabled,
  permissions: Object.fromEntries(FEATURES.map(key => [key, value.permissions[key]])) });
const safeAuditState = value => !object(value) ? null : {
  ...(typeof value.disabled === 'boolean' ? { disabled: value.disabled } : {}),
  ...(object(value.permissions) ? { permissions: Object.fromEntries(FEATURES.filter(key => typeof value.permissions[key] === 'boolean').map(key => [key, value.permissions[key]])) } : {}),
};

export function createAdminService({ request, onChange = () => {} }) {
  const state = { account: null, open: false, visible: true, tab: 'overview', accounts: [], version: null, overview: null, usage: null, audit: null,
    days: 7, accountId: '', loading: false, saving: false, needsRefresh: true, error: '', actionResult: null };
  let generation = 0, controller = null, disposed = false, mutationFlight = false;
  const emit = () => { if (!disposed) onChange(state); };
  const active = () => !disposed && state.account && state.open && state.visible;
  const cancel = () => {
    generation += 1; controller?.abort(); controller = null;
    if (state.saving) state.needsRefresh = true;
    state.loading = false; state.saving = false;
  };
  function setSession(identity) {
    const user = identity?.user;
    const account = user?.is_admin === true && !user.disabled ? user.object_id || user.id : null;
    if (state.account === account) return;
    cancel(); Object.assign(state, { account, open: false, tab: 'overview', accounts: [], version: null, overview: null, usage: null, audit: null,
      days: 7, accountId: '', needsRefresh: true, error: '', actionResult: null }); emit();
  }
  async function perform(path, options, apply, mutation = false) {
    const ticket = ++generation;
    controller?.abort(); controller = new AbortController();
    const current = () => active() && generation === ticket;
    state.loading = !mutation; state.saving = mutation; state.error = ''; emit();
    if (mutation) mutationFlight = true;
    try {
      const result = await request(path, { ...options, signal: controller.signal });
      if (!current()) return false;
      apply(result); return true;
    } catch (error) {
      if (!current()) return false;
      if ([401, 403].includes(error.status)) { setSession(null); return false; }
      if (mutation) { state.needsRefresh = true; state.version = null; }
      state.error = translate(mutation ? [409, 412].includes(error.status)
        ? '账户列表已变更，请刷新后重新检查。' : '操作结果未确认，请刷新账户列表后核对。'
        : '管理数据暂不可用，请刷新重试。');
      return false;
    } finally {
      if (mutation) mutationFlight = false;
      if (current()) { state.loading = false; state.saving = false; controller = null; emit(); }
    }
  }
  async function refresh() {
    if (!active() || mutationFlight || state.saving) return;
    const tab = state.tab;
    const applyAccounts = result => {
      if (!Array.isArray(result.accounts) || !result.accounts.every(validAccount) || !validVersion(result.version)
        || new Set(result.accounts.map(item => item.object_id)).size !== result.accounts.length) throw new Error('Invalid accounts');
      state.accounts = result.accounts.map(safeAccount); state.version = result.version; state.needsRefresh = false;
    };
    if (tab === 'usage' && !validVersion(state.version)) {
      const loaded = await perform('/api/admin/accounts', {}, applyAccounts);
      if (!loaded || !active() || state.tab !== tab) return;
    }
    const path = tab === 'usage' ? `/api/admin/usage?days=${state.days}${state.accountId ? `&account_id=${state.accountId}` : ''}` : `/api/admin/${tab}`;
    return perform(path, {}, result => {
      if (tab === 'accounts') {
        applyAccounts(result);
      } else if (tab === 'overview') {
        if (!object(result.accounts) || !object(result.runs) || !validCoverage(result.coverage) || !Array.isArray(result.recent_errors)) throw new Error('Invalid overview');
        state.overview = { accounts: result.accounts, runs: result.runs, budget: result.budget, coverage: result.coverage,
          recent_errors: result.recent_errors.map(item => ({ account_email: item.account_email, run_id: item.run_id, code: item.code, occurred_at: item.occurred_at })) };
      } else if (tab === 'usage') {
        if (![7, 30, 90].includes(result.days) || !Array.isArray(result.rows) || !validCoverage(result.coverage)) throw new Error('Invalid usage');
        state.usage = { days: result.days, coverage: result.coverage, rows: result.rows.map(item => Object.fromEntries(
          ['account_id', 'email', 'research_runs', 'research_failed', 'assistant_calls', 'input_tokens', 'output_tokens', 'platform_cost_usd', 'external_cost_usd', 'last_activity'].map(key => [key, item[key]]))) };
      } else {
        if (!Array.isArray(result.events) || typeof result.truncated !== 'boolean') throw new Error('Invalid audit');
        state.audit = { truncated: result.truncated, events: result.events.map(item => ({ id: item.id, occurred_at: item.occurred_at,
          actor_email: item.actor_email, target_email: item.target_email, action: item.action, before: safeAuditState(item.before), after: safeAuditState(item.after), result: item.result })) };
      }
    });
  }
  async function save(id, values) {
    const account = state.accounts.find(item => item.object_id === id);
    if (!active() || mutationFlight || state.loading || state.saving || state.needsRefresh || !validVersion(state.version) || !account || account.is_admin) return false;
    if (!object(values) || typeof values.disabled !== 'boolean' || !validPermissions(values.permissions)) throw new Error('Invalid account update');
    const body = { disabled: values.disabled, permissions: Object.fromEntries(FEATURES.map(key => [key, values.permissions[key]])) };
    return perform(`/api/admin/accounts/${id}`, { method: 'PATCH', body, revision: state.version }, result => {
      if (!validAccount(result.account) || result.account.object_id !== id || result.account.is_admin || !validVersion(result.version)
        || result.account.disabled !== body.disabled || FEATURES.some(key => result.account.permissions[key] !== body.permissions[key])) throw new Error('Invalid mutation result');
      state.accounts = state.accounts.map(item => item.object_id === id ? safeAccount(result.account) : item);
      state.version = result.version; state.needsRefresh = false;
    }, true);
  }
  async function emailAction(kind, value) {
    if (!active() || mutationFlight || state.loading || state.saving || state.needsRefresh) return false;
    if (!['invite', 'recovery'].includes(kind)) throw new Error('Invalid action');
    if (kind === 'recovery' && !state.accounts.some(item => item.object_id === value && !item.is_admin)) return false;
    if (kind === 'invite' && (typeof value !== 'string' || !/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(value) || value.length > 254)) throw new Error('Invalid email');
    state.actionResult = null;
    return perform(`/api/admin/${kind === 'invite' ? 'invitations' : 'recoveries'}`, { method: 'POST', body: kind === 'invite' ? { email: value.trim() } : { user_id: value } }, result => {
      if (typeof result.fragment !== 'string' || !/^\/auth\/(?:redeem|reset)#[^\s]+$/.test(result.fragment)) throw new Error('Invalid action result');
      state.actionResult = { fragment: result.fragment, email: typeof result.email === 'string' ? result.email : '' };
      state.needsRefresh = true; state.version = null;
    }, true);
  }
  return {
    state, setSession, refresh, save, emailAction,
    async open() { if (!state.account || disposed || mutationFlight) return; cancel(); state.open = true; emit(); return refresh(); },
    close() { cancel(); state.open = false; state.error = ''; state.actionResult = null; emit(); },
    async selectTab(tab) { if (!['overview', 'accounts', 'usage', 'audit'].includes(tab) || mutationFlight) return; cancel(); state.tab = tab; emit(); return refresh(); },
    async setUsage({ days = state.days, accountId = state.accountId }) {
      if (![7, 30, 90].includes(days) || (accountId && !UUID.test(accountId))) throw new Error('Invalid usage parameters');
      if (mutationFlight) return;
      cancel(); state.days = days; state.accountId = accountId; emit(); return refresh();
    },
    setVisible(visible) { if (state.visible === visible) return; cancel(); state.visible = visible; emit(); if (active() && !mutationFlight) void refresh(); },
    dispose() { cancel(); disposed = true; state.account = null; state.accounts = []; state.overview = null; state.usage = null; state.audit = null; },
  };
}

export function mountAdminWorkspace(doc, { request, paintIcons = () => {}, onOpen = () => {}, onClose = () => {} }) {
  const win = doc.defaultView, byId = id => doc.getElementById(id);
  const screen = byId('management-screen'), entry = byId('management-entry'), dialog = byId('admin-confirm-dialog');
  const listeners = [], drafts = new Map();
  let disposed = false, pendingConfirmation = null, localError = '', accountSignature = '', query = '', filter = 'all';
  const on = (target, name, callback) => { target.addEventListener(name, callback); listeners.push(() => target.removeEventListener(name, callback)); };
  const node = (tag, text, className) => { const item = doc.createElement(tag); if (text !== undefined) item.textContent = text; if (className) item.className = className; return item; };
  const metric = value => typeof value === 'number' && Number.isFinite(value) && value >= 0 ? value.toLocaleString(locale(), { maximumFractionDigits: 4 }) : '\u2014';
  const time = value => typeof value === 'string' && Number.isFinite(new Date(value).getTime()) ? new Date(value).toLocaleString(locale()) : '\u2014';
  const actionButton = (icon, label, attribute, value) => {
    const button = node('button', undefined, 'icon-button'); button.type = 'button'; button.title = translate(label); button.setAttribute('aria-label', button.title);
    button.setAttribute(attribute, value); const image = node('i'); image.dataset.lucide = icon; button.append(image); return button;
  };
  const closeConfirmation = () => {
    pendingConfirmation = null;
    if (dialog.open && typeof dialog.close === 'function') dialog.close(); else dialog.removeAttribute('open');
    byId('admin-confirm-description').textContent = '';
  };
  const confirm = (message, operation) => {
    if (!service.state.account || service.state.saving || service.state.loading || service.state.needsRefresh) return;
    pendingConfirmation = operation; byId('admin-confirm-description').textContent = message;
    if (typeof dialog.showModal === 'function') dialog.showModal(); else dialog.setAttribute('open', '');
    byId('admin-confirm-cancel').focus();
  };
  const coverageText = coverage => `${translate('覆盖起始')} ${time(coverage?.since)} · ${translate(coverage?.truncated ? '有界读取已截断' : '有界读取未截断')}${coverage?.notes?.length ? ` · ${coverage.notes.join(' · ')}` : ''}`;
  const auditState = value => {
    if (!object(value)) return '\u2014';
    const parts = [];
    if (typeof value.disabled === 'boolean') parts.push(`${translate('已禁用')}: ${translate(value.disabled ? '是' : '否')}`);
    for (const key of FEATURES) if (typeof value.permissions?.[key] === 'boolean') parts.push(`${translate({ research: '研究', assistant: '助手', market: '行情' }[key])}: ${translate(value.permissions[key] ? '是' : '否')}`);
    return parts.join(' · ') || '\u2014';
  };
  function renderAccounts(state) {
    const signature = JSON.stringify([state.accounts, state.version]);
    if (signature !== accountSignature) { drafts.clear(); accountSignature = signature; }
    const rows = byId('admin-account-rows'); rows.replaceChildren();
    const visible = state.accounts.filter(account => `${account.email} ${account.name}`.toLocaleLowerCase().includes(query.toLocaleLowerCase())
      && (filter === 'all' || (filter === 'disabled') === account.disabled));
    for (const account of visible) {
      const row = node('tr'); row.dataset.adminAccount = account.object_id;
      const identity = node('td'); identity.dataset.label = translate('账户'); identity.append(node('strong', account.name), node('small', account.email, 'muted'));
      if (account.is_admin) identity.append(node('small', translate('固定所有者'), 'muted'));
      row.append(identity);
      const draft = drafts.get(account.object_id) || { disabled: account.disabled, permissions: { ...account.permissions } };
      for (const key of ['disabled', ...FEATURES]) {
        const cell = node('td'), label = translate({ disabled: '已禁用', research: '研究', assistant: '助手', market: '行情' }[key]); cell.dataset.label = label;
        const checkbox = node('input'); checkbox.type = 'checkbox'; checkbox.dataset.adminPermission = key;
        checkbox.checked = key === 'disabled' ? draft.disabled : draft.permissions[key];
        checkbox.setAttribute('aria-label', `${account.email} ${label}`);
        checkbox.disabled = account.is_admin || state.loading || state.saving || state.needsRefresh;
        cell.append(checkbox); row.append(cell);
      }
      const actions = node('td'); actions.dataset.label = translate('操作');
      if (!account.is_admin) {
        const save = actionButton('file-text', '保存权限', 'data-admin-save', account.object_id);
        save.disabled = state.loading || state.saving || state.needsRefresh || !drafts.has(account.object_id);
        const recovery = actionButton('rotate-ccw', '生成重置链接', 'data-admin-recovery', account.object_id);
        recovery.disabled = state.loading || state.saving || state.needsRefresh || account.disabled;
        actions.append(save, recovery);
      } else actions.textContent = '\u2014';
      row.append(actions); rows.append(row);
    }
    byId('admin-account-empty').hidden = visible.length > 0;
    byId('admin-account-empty').textContent = translate(state.loading ? '正在读取管理数据…' : '暂无匹配账户');
    byId('admin-account-version').textContent = state.version ? `${translate('列表版本')} ${state.version}` : translate('需要刷新账户列表');
    const select = byId('admin-usage-account'), selected = state.accountId;
    select.replaceChildren(node('option', translate('全部账户'))); select.firstChild.value = '';
    for (const account of state.accounts) { const option = node('option', account.email); option.value = account.object_id; select.append(option); }
    if (selected && !state.accounts.some(account => account.object_id === selected)) { const option = node('option', selected); option.value = selected; select.append(option); }
    select.value = selected;
    paintIcons(rows);
  }
  function render(state) {
    if (disposed) return;
    entry.hidden = !state.account;
    if (!state.open || !state.account) {
      const wasOpen = !screen.hidden; screen.hidden = true; entry.removeAttribute('aria-current');
      closeConfirmation(); if (wasOpen) onClose();
    }
    for (const tab of screen.querySelectorAll('[data-admin-tab]')) {
      const active = tab.dataset.adminTab === state.tab;
      tab.setAttribute('aria-selected', String(active)); tab.tabIndex = active ? 0 : -1; tab.disabled = state.saving;
      byId(tab.getAttribute('aria-controls')).hidden = !active;
    }
    screen.setAttribute('aria-busy', String(state.loading || state.saving));
    byId('admin-status').textContent = localError || state.error || translate(state.saving ? '正在保存管理操作…' : state.loading ? '正在读取管理数据…' : '');
    byId('admin-status').dataset.error = String(Boolean(localError || state.error));
    byId('admin-refresh').disabled = !state.account || state.loading || state.saving;
    byId('admin-invite-submit').disabled = !state.account || state.loading || state.saving || state.needsRefresh;
    byId('admin-invite-email').disabled = !state.account || state.saving;
    for (const control of screen.querySelectorAll('[data-admin-days], #admin-usage-account')) control.disabled = state.loading || state.saving;
    for (const control of screen.querySelectorAll('[data-admin-days]')) control.setAttribute('aria-pressed', String(Number(control.dataset.adminDays) === state.days));
    for (const field of screen.querySelectorAll('[data-admin-metric]')) {
      const [section, key] = field.dataset.adminMetric.split('.'); const value = state.overview?.[section]?.[key];
      field.textContent = section === 'budget' && typeof value === 'number' && value >= 0 ? `$${(value / 1000000).toLocaleString(locale(), { minimumFractionDigits: 2, maximumFractionDigits: 2 })}` : metric(value);
    }
    byId('admin-budget-month').textContent = state.overview?.budget?.month || '\u2014';
    byId('admin-overview-coverage').textContent = coverageText(state.overview?.coverage);
    const errors = byId('admin-error-rows'); errors.replaceChildren();
    for (const item of state.overview?.recent_errors || []) {
      const row = node('tr'); for (const [label, value] of [['账户', item.account_email], ['运行', item.run_id], ['错误代码', item.code], ['时间', time(item.occurred_at)]]) { const cell = node('td', value || '\u2014'); cell.dataset.label = translate(label); row.append(cell); } errors.append(row);
    }
    byId('admin-errors-empty').hidden = Boolean(state.overview?.recent_errors.length);
    renderAccounts(state);
    const usage = byId('admin-usage-rows'); usage.replaceChildren();
    const usageLabels = ['账户', '研究次数', '研究失败', '助手调用', '输入 tokens', '输出 tokens', '平台费用 USD', '外部费用 USD', '最近活动'];
    const fields = ['email', 'research_runs', 'research_failed', 'assistant_calls', 'input_tokens', 'output_tokens', 'platform_cost_usd', 'external_cost_usd', 'last_activity'];
    for (const item of state.usage?.rows || []) {
      const row = node('tr'); fields.forEach((field, index) => {
        const value = field === 'email' ? item[field] : field === 'last_activity' ? time(item[field]) : field === 'external_cost_usd' ? '\u2014' : metric(item[field]);
        const cell = node('td', value || '\u2014'); cell.dataset.adminField = field; cell.dataset.label = translate(usageLabels[index]); row.append(cell);
      }); usage.append(row);
    }
    byId('admin-usage-empty').hidden = Boolean(state.usage?.rows.length);
    byId('admin-usage-coverage').textContent = coverageText(state.usage?.coverage);
    const audit = byId('admin-audit-rows'); audit.replaceChildren();
    for (const item of state.audit?.events || []) {
      const row = node('tr'); const values = [time(item.occurred_at), item.actor_email, item.target_email, item.action, auditState(item.before), auditState(item.after), item.result];
      values.forEach((value, index) => { const cell = node('td', typeof value === 'string' && value ? value : '\u2014'); cell.dataset.label = translate(['时间', '操作者', '目标账户', '操作', '之前', '之后', '结果'][index]); row.append(cell); }); audit.append(row);
    }
    byId('admin-audit-empty').hidden = Boolean(state.audit?.events.length);
    byId('admin-audit-coverage').textContent = translate(state.audit?.truncated ? '有界读取已截断' : '有界读取未截断');
    const result = byId('admin-action-result'); result.replaceChildren();
    if (state.actionResult) { const link = node('a', translate('打开安全链接')); link.href = state.actionResult.fragment; result.append(link); }
    result.hidden = !state.actionResult;
    if (!state.account) { drafts.clear(); query = ''; filter = 'all'; byId('admin-account-search').value = ''; byId('admin-account-filter').value = 'all'; byId('admin-invite-email').value = ''; }
  }
  const service = createAdminService({ request, onChange: render });
  service.setVisible(!doc.hidden);
  async function open() {
    if (!service.state.account || disposed || service.state.saving) return;
    localError = ''; onOpen(); screen.hidden = false; entry.setAttribute('aria-current', 'page');
    byId('research-tasks-entry').removeAttribute('aria-current');
    for (const id of ['workspace-home', 'workspace-tasks', 'workspace-run']) byId(id).hidden = true;
    doc.documentElement.dataset.screen = 'management'; byId('workbench').dataset.screen = 'management';
    byId('breadcrumb-studio').textContent = translate('管理');
    await service.open();
  }
  function close() { localError = ''; drafts.clear(); closeConfirmation(); byId('admin-invite-email').value = ''; service.state.actionResult = null; service.close(); }
  const run = async operation => { localError = ''; try { await operation(); } catch { localError = translate('管理操作无效，请检查输入。'); render(service.state); } };
  on(entry, 'click', () => { void open(); });
  on(byId('admin-refresh'), 'click', () => { drafts.clear(); closeConfirmation(); void run(service.refresh); });
  const tabs = [...screen.querySelectorAll('[data-admin-tab]')];
  for (const tab of tabs) {
    on(tab, 'click', () => { if (service.state.saving) return; closeConfirmation(); void run(() => service.selectTab(tab.dataset.adminTab)); });
    on(tab, 'keydown', event => { if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key) || service.state.saving) return;
      event.preventDefault(); const index = event.key === 'Home' ? 0 : event.key === 'End' ? tabs.length - 1 : (tabs.indexOf(tab) + (event.key === 'ArrowRight' ? 1 : -1) + tabs.length) % tabs.length;
      tabs[index].click(); tabs[index].focus(); });
  }
  on(byId('admin-account-search'), 'input', event => { query = event.target.value; renderAccounts(service.state); });
  on(byId('admin-account-filter'), 'change', event => { filter = event.target.value; renderAccounts(service.state); });
  on(byId('admin-account-rows'), 'change', event => {
    const checkbox = event.target.closest('[data-admin-permission]'), row = checkbox?.closest('[data-admin-account]');
    if (!row || service.state.saving || service.state.needsRefresh) return;
    const account = service.state.accounts.find(item => item.object_id === row.dataset.adminAccount); if (!account || account.is_admin) return;
    const draft = drafts.get(account.object_id) || { disabled: account.disabled, permissions: { ...account.permissions } };
    if (checkbox.dataset.adminPermission === 'disabled') draft.disabled = checkbox.checked; else draft.permissions[checkbox.dataset.adminPermission] = checkbox.checked;
    drafts.set(account.object_id, draft); row.querySelector('[data-admin-save]').disabled = false;
  });
  on(byId('admin-account-rows'), 'click', event => {
    const save = event.target.closest('[data-admin-save]'), recovery = event.target.closest('[data-admin-recovery]');
    const id = save?.dataset.adminSave || recovery?.dataset.adminRecovery;
    const account = service.state.accounts.find(item => item.object_id === id); if (!account || account.is_admin) return;
    if (save) {
      const draft = drafts.get(id); if (!draft) return;
      const operation = () => run(() => service.save(id, structuredClone(draft)));
      if (draft.disabled && !account.disabled) confirm(`${translate('确认禁用账户并撤销会话？')} ${account.email}`, operation); else void operation();
    } else if (recovery) confirm(`${translate('确认生成账户重置链接？')} ${account.email}`, () => run(() => service.emailAction('recovery', id)));
  });
  on(byId('admin-invite-form'), 'submit', event => {
    event.preventDefault(); const email = byId('admin-invite-email').value.trim();
    if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email)) { localError = translate('请输入有效邮箱。'); render(service.state); return; }
    confirm(`${translate('确认生成邀请链接？')} ${email}`, async () => { await run(() => service.emailAction('invite', email)); byId('admin-invite-email').value = ''; });
  });
  on(byId('admin-confirm-accept'), 'click', () => { const operation = pendingConfirmation; closeConfirmation(); if (service.state.account && !service.state.saving && !service.state.needsRefresh) void operation?.(); });
  on(byId('admin-confirm-cancel'), 'click', closeConfirmation);
  on(dialog, 'cancel', event => { event.preventDefault(); closeConfirmation(); });
  for (const button of screen.querySelectorAll('[data-admin-days]')) on(button, 'click', () => { void run(() => service.setUsage({ days: Number(button.dataset.adminDays) })); });
  on(byId('admin-usage-account'), 'change', event => { void run(() => service.setUsage({ accountId: event.target.value })); });
  on(doc, 'visibilitychange', () => { closeConfirmation(); service.setVisible(!doc.hidden); });
  on(win, 'pagehide', () => { closeConfirmation(); service.setVisible(false); });
  on(win, 'pageshow', () => service.setVisible(!doc.hidden));
  const offLocale = onLocaleChange(() => { render(service.state); if (service.state.open) byId('breadcrumb-studio').textContent = translate('管理'); });
  bindLocale(doc, '#management-screen, #management-entry, #admin-confirm-dialog'); render(service.state); paintIcons(screen);
  return { state: service.state, open, close,
    setSession(identity) { closeConfirmation(); localError = ''; service.setSession(identity); },
    dispose() { closeConfirmation(); close(); disposed = true; service.dispose(); offLocale(); listeners.forEach(remove => remove()); },
  };
}