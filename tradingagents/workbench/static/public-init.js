import { bindLocale, onLocaleChange, translate } from './locale.mjs';

const FIELDS = ['subscription_id', 'resource_group', 'location', 'prefix', 'foundry_account_id', 'deployment_name'];
const STEPS = { group: '资源组', registry: '镜像仓库', workspace: '日志工作区', identity: '运行身份', environment: '容器环境', 'role-0': '镜像读取授权', 'role-1': 'Foundry 授权', build: '构建镜像', job: '研究 Job', profile: '保存配置' };

export function mountInitialization(doc, { container, request, perform, onProfile, signal, clock }) {
  const root = doc.createElement('section'); root.className = 'resource-section initialization-section';
  root.innerHTML = `
    <h3>按需初始化</h3>
    <form id="initialization-form" class="resource-form" novalidate>
      <label>订阅 ID<input id="init-subscription_id" autocomplete="off" required></label>
      <div class="field-pair"><label>资源组<input id="init-resource_group" required></label><label>区域<input id="init-location" value="eastus2" required></label></div>
      <label>资源前缀<input id="init-prefix" maxlength="20" required></label>
      <label>已有 Foundry 账户资源 ID<input id="init-foundry_account_id" list="foundry-options" required></label>
      <label>模型部署<input id="init-deployment_name" list="deployment-options" required></label>
      <button type="submit" data-resource-write><i data-lucide="search"></i>预览资源与授权</button>
    </form>
    <div class="resource-form"><label>初始化记录<select id="initialization-history"></select></label>
      <div id="initialization-plan" class="initialization-plan"></div>
      <p id="initialization-status" class="muted" role="status"></p>
      <label id="initialization-confirm-label" hidden>确认短语 <code id="initialization-required"></code><input id="initialization-confirmation" autocomplete="off"></label>
      <div class="resource-actions"><button id="initialization-confirm" type="button" data-resource-write><i data-lucide="check"></i>确认此预览</button><button id="initialization-resume" type="button" data-resource-write><i data-lucide="play"></i>继续初始化</button><button id="initialization-pause" type="button"><i data-lucide="square"></i>暂停</button><button id="initialization-refresh" type="button" title="刷新初始化记录" aria-label="刷新初始化记录"><i data-lucide="refresh-cw"></i></button></div>
    </div>`;
  container.append(root);
  root.dataset.localeRoot = '';
  bindLocale(doc, '.initialization-section');
  const byId = id => doc.getElementById(id);
  const listeners = [];
  const on = (id, event, action) => { const target = byId(id); target.addEventListener(event, action); listeners.push(() => target.removeEventListener(event, action)); };
  let records = [];
  let operation = null;
  let running = false;
  let timer = null;
  let disposed = false;
  let parentBusy = false;

  function controls(busy = parentBusy) {
    parentBusy = busy;
    byId('initialization-history').disabled = busy || running || !records.length;
    byId('initialization-confirm').disabled = busy || operation?.status !== 'preview' || byId('initialization-confirmation').value !== operation?.confirmation;
    byId('initialization-resume').disabled = busy || running || !operation?.confirmed_at || operation.status === 'succeeded';
    byId('initialization-pause').disabled = !running;
    byId('initialization-refresh').disabled = busy || running;
    byId('initialization-form').querySelector('button').disabled = busy || running;
  }

  function render(value, preserveConfirmation = false) {
    operation = value;
    if (!preserveConfirmation) byId('initialization-confirmation').value = '';
    byId('initialization-confirm-label').hidden = value?.status !== 'preview';
    byId('initialization-required').textContent = value?.confirmation || '';
    const plan = byId('initialization-plan'); plan.replaceChildren();
    const line = text => { const item = doc.createElement('p'); item.textContent = text; plan.append(item); };
    if (value) {
      line(translate(value.plan.group.create ? '新建资源组: ' : '使用资源组: ') + value.plan.group.id);
      for (const item of value.plan.resources) line((translate(STEPS[item.kind]) || item.kind) + ': ' + item.id);
      for (const item of value.plan.roles) line(item.role + ': ' + item.scope);
      for (const item of value.plan.charges) line(item);
      if (value.plan.source_sha256) line('SHA-256: ' + value.plan.source_sha256);
    }
    const complete = value?.completed_steps || [];
    byId('initialization-status').textContent = !value ? translate('暂无初始化记录') : value.status === 'succeeded' ? translate('初始化完成') : `${complete.length} / ${Object.keys(STEPS).length} · ${translate(STEPS[value.current_step]) || value.status}${value.error ? ' · ' + value.error : ''}`;
    controls();
  }

  function selectRecords(preferred) {
    const select = byId('initialization-history');
    select.replaceChildren(...records.map(record => {
      const option = doc.createElement('option'); option.value = record.id;
      option.textContent = `${record.plan.selection.resource_group}/${record.plan.selection.prefix} · ${record.status}`; return option;
    }));
    if (records.some(item => item.id === preferred)) select.value = preferred;
    render(records.find(item => item.id === select.value) || null);
  }

  function save(value) {
    records = [value, ...records.filter(item => item.id !== value.id)]; selectRecords(value.id);
  }

  async function refresh() {
    const data = await request('/api/initializations', { signal });
    if (disposed) return;
    if (!Array.isArray(data.initializations)) throw new Error('Invalid initialization history');
    records = data.initializations; selectRecords(operation?.id);
  }

  function pause() {
    running = false;
    if (timer !== null) clock.clearTimeout(timer);
    timer = null; if (!disposed) controls();
  }

  async function advance() {
    timer = null;
    if (disposed || !running) return;
    const id = operation.id;
    const succeeded = await perform(async () => {
      const value = await request(`/api/initializations/${id}/apply`, { method: 'POST', body: {}, signal });
      if (disposed) return;
      save(value);
      if (value.status === 'succeeded') { pause(); await onProfile(value.profile_id); }
      else if (value.status === 'blocked') pause();
    });
    if (!succeeded) pause();
    if (running && !disposed) timer = clock.setTimeout(advance, 5000);
  }

  on('initialization-form', 'submit', event => {
    event.preventDefault(); if (running) return;
    const body = Object.fromEntries(FIELDS.map(field => [field, byId('init-' + field).value.trim()]));
    perform(async () => { const value = await request('/api/initializations', { method: 'POST', body, signal }); if (!disposed) save(value); });
  });
  on('initialization-confirmation', 'input', () => controls());
  on('initialization-confirm', 'click', () => {
    if (byId('initialization-confirm').disabled) return;
    const id = operation.id;
    const body = { confirmation: byId('initialization-confirmation').value };
    perform(async () => { const value = await request(`/api/initializations/${id}/confirm`, { method: 'POST', body, signal }); if (!disposed) save(value); });
  });
  on('initialization-resume', 'click', () => { if (!byId('initialization-resume').disabled) { running = true; controls(); advance(); } });
  on('initialization-pause', 'click', pause);
  on('initialization-refresh', 'click', () => perform(refresh));
  on('initialization-history', 'change', () => { pause(); render(records.find(item => item.id === byId('initialization-history').value)); });
  render(null);
  const offLocale = onLocaleChange(() => render(operation, true));
  return { controls, refresh, pause, dispose() { offLocale(); pause(); disposed = true; listeners.forEach(remove => remove()); root.remove(); } };
}