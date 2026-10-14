import { createIcons, icons } from './vendor.js';
import { mountInitialization } from './public-init.js';
import { bindLocale, onLocaleChange, translate } from './locale.mjs';

const PROFILE_FIELDS = ['name', 'job_id', 'workspace_resource_id', 'foundry_account_id', 'deployment_name'];
const AUTH_SCOPES = new Set(['https://management.azure.com/.default', 'https://api.loganalytics.io/.default', 'https://cognitiveservices.azure.com/.default']);

export function mountPublicAccount(doc, { request, onSelect, onLogout, signal, clock = doc.defaultView }) {
  const node = (tag, text) => {
    const element = doc.createElement(tag);
    if (text !== undefined) element.textContent = text;
    return element;
  };
  const accountName = node('span'); accountName.id = 'account-name'; accountName.className = 'small';
  doc.querySelector('.topbar-end').prepend(accountName);
  doc.querySelector('.local-label').textContent = 'ENTRA ID';
  const root = node('div'); root.id = 'public-account'; root.className = 'account-select';
  const label = node('label', '\u8d44\u6e90\u914d\u7f6e'); label.htmlFor = 'profile-select';
  const select = node('select'); select.id = 'profile-select'; label.append(select); root.append(label);
  doc.querySelector('#side-config').prepend(root);
  doc.querySelector('.model-line span').textContent = '\u5f53\u524d\u6a21\u578b';
  const manage = node('button'); manage.id = 'manage-resources'; manage.type = 'button';
  manage.className = 'icon-button'; manage.title = '\u7ba1\u7406 Azure \u8d44\u6e90'; manage.setAttribute('aria-label', manage.title);
  manage.innerHTML = '<i data-lucide="settings-2"></i>'; root.append(manage);
  const logout = node('button'); logout.id = 'sign-out'; logout.type = 'button'; logout.className = 'icon-button';
  logout.title = '\u9000\u51fa\u767b\u5f55'; logout.setAttribute('aria-label', logout.title);
  logout.innerHTML = '<i data-lucide="arrow-right"></i>'; doc.querySelector('.topbar-end').append(logout);
  const dialog = node('dialog'); dialog.id = 'resource-dialog'; dialog.className = 'resource-dialog';
  dialog.setAttribute('aria-labelledby', 'resource-heading');
  dialog.innerHTML = `
    <header class="resource-heading"><h2 id="resource-heading">Azure 与 Foundry</h2><button id="resources-close" type="button" class="icon-button" title="关闭资源配置" aria-label="关闭资源配置"><i data-lucide="x"></i></button></header>
    <p id="resource-error" class="error-message" role="alert" hidden></p>
    <a id="resource-reauthorize" class="reauthorize" href="/auth/login" hidden>重新授权</a>
    <section id="resource-profiles" class="resource-section">
      <div class="panel-heading"><h3>资源配置</h3><button id="profile-new" type="button"><i data-lucide="plus"></i>新建配置</button></div>
      <div class="resource-discovery"><button id="browse-resources" type="button"><i data-lucide="search"></i>浏览 Azure</button><div id="resource-browser" class="field-pair" hidden><label>订阅<select id="resource-subscription"></select></label><label>资源组<select id="resource-group"></select></label></div></div>
      <form id="profile-form" novalidate class="resource-form">
        <label>配置名称<input id="profile-name" maxlength="80" required></label>
        <label>研究 Job 资源 ID<input id="profile-job_id" list="job-options" autocomplete="off" required></label>
        <label>日志工作区资源 ID<input id="profile-workspace_resource_id" list="workspace-options" autocomplete="off" required></label>
        <label>Foundry 账户资源 ID<input id="profile-foundry_account_id" list="foundry-options" autocomplete="off" required></label>
        <label>模型部署<input id="profile-deployment_name" list="deployment-options" autocomplete="off" required></label>
        <datalist id="job-options"></datalist><datalist id="workspace-options"></datalist><datalist id="foundry-options"></datalist><datalist id="deployment-options"></datalist>
        <div class="resource-actions"><button id="profile-save" type="submit" class="primary-button" data-resource-write><i data-lucide="check"></i>验证并保存</button><button id="profile-delete" type="button" class="danger-button" data-resource-write><i data-lucide="x"></i>删除配置</button><button id="profile-delete-confirm" type="button" class="danger-button" data-resource-write hidden>确认删除配置</button></div>
      </form>
      <form id="provider-form" class="resource-form provider-form" novalidate>
        <h3>提供商密钥</h3><p id="provider-key-state" class="muted" role="status"></p>
        <fieldset id="provider-fields"><div class="provider-key-row"><label>Alpha Vantage <span id="key-state-alpha_vantage" class="muted"></span><input id="key-alpha_vantage" type="password" autocomplete="new-password" maxlength="512"></label><button type="button" data-disable-provider="alpha_vantage" title="停用 Alpha Vantage 绑定">停用</button></div>
        <div class="provider-key-row"><label>FRED <span id="key-state-fred" class="muted"></span><input id="key-fred" type="password" autocomplete="new-password" maxlength="512"></label><button type="button" data-disable-provider="fred" title="停用 FRED 绑定">停用</button></div>
        <button id="keys-save" type="submit" data-resource-write><i data-lucide="check"></i>保存密钥</button></fieldset>
      </form>
    </section>`;
  doc.body.append(dialog);
  root.dataset.localeRoot = '';
  dialog.dataset.localeRoot = '';
  logout.dataset.localeRoot = '';
  bindLocale(doc, '#public-account, #resource-dialog, #sign-out');
  createIcons({ icons, root: doc, attrs: { 'aria-hidden': 'true', 'stroke-width': 1.6 } });
  const byId = id => doc.getElementById(id);
  const listeners = [];
  const on = (target, event, handler) => { target.addEventListener(event, handler); listeners.push(() => target.removeEventListener(event, handler)); };
  let profiles = [];
  let profile = null;
  let editing = null;
  let busy = false;
  let providerGeneration = 0;
  let discoveryGeneration = 0;
  let disposed = false;
  let initialization = null;

  function handleError(error) {
    if (error.status !== 401 && error.code !== 'authentication_required') return;
    const href = AUTH_SCOPES.has(error.scope) ? '/auth/login?scope=' + encodeURIComponent(error.scope) : '/auth/login';
    for (const id of ['reauthorize', 'resource-reauthorize']) {
      const link = byId(id);
      link.href = href; link.hidden = false;
    }
  }

  function controls() {
    select.disabled = busy || !profiles.length;
    for (const button of dialog.querySelectorAll('[data-resource-write], #profile-new, #browse-resources')) button.disabled = busy;
    byId('profile-delete').disabled = busy || !editing;
    byId('provider-fields').disabled = busy || !profile;
    byId('resource-subscription').disabled = busy;
    byId('resource-group').disabled = busy;
    logout.disabled = busy;
    initialization?.controls(busy);
  }
  const offLocale = onLocaleChange(() => {
    for (const provider of ['alpha_vantage', 'fred']) {
      const target = byId('key-state-' + provider);
      if (target.dataset.sourceLabel) target.textContent = translate(target.dataset.sourceLabel);
    }
    const pending = byId('provider-key-state');
    if (pending.dataset.sourceLabel) pending.textContent = translate(pending.dataset.sourceLabel);
  });

  async function perform(action) {
    if (busy || disposed) return;
    busy = true; controls(); byId('resource-error').hidden = true;
    try { await action(); return true; }
    catch (error) {
      if (!disposed) {
        handleError(error); byId('resource-error').textContent = error.status ? error.message : '请求结果尚未确认，请刷新配置核对。';
        byId('resource-error').hidden = false;
      }
      return false;
    } finally { busy = false; if (!disposed) controls(); }
  }

  function editProfile(value) {
    editing = value;
    for (const field of PROFILE_FIELDS) byId('profile-' + field).value = value?.[field] || '';
    byId('profile-delete-confirm').hidden = true;
    controls();
  }

  function clearProviderStatus() {
    providerGeneration += 1;
    byId('provider-key-state').textContent = '';
    for (const provider of ['alpha_vantage', 'fred']) {
      byId('key-' + provider).value = '';
      byId('key-state-' + provider).textContent = '--';
    }
  }

  async function providerStatus() {
    const selectedId = profile?.id;
    clearProviderStatus();
    const generation = providerGeneration;
    if (!selectedId) return;
    const result = await request(`/api/profiles/${selectedId}/providers`, { signal });
    if (disposed || generation !== providerGeneration || selectedId !== profile?.id) return;
    for (const provider of ['alpha_vantage', 'fred']) {
      const target = byId('key-state-' + provider);
      target.dataset.sourceLabel = result.configured?.[provider] ? '已配置' : '未配置';
      target.textContent = translate(target.dataset.sourceLabel);
    }
    byId('provider-key-state').dataset.sourceLabel = result.pending ? 'Azure 更新结果待核对' : '';
    byId('provider-key-state').textContent = translate(byId('provider-key-state').dataset.sourceLabel);
  }

  function choose(loadProviders = true) {
    profile = profiles.find(item => item.id === select.value) || null;
    clearProviderStatus();
    editProfile(profile);
    onSelect(profile);
    if (loadProviders && dialog.open) perform(providerStatus);
  }
  on(select, 'change', choose);

  async function refresh(preferred = profile?.id) {
    const [identity, data] = await Promise.all([request('/api/me', { signal }), request('/api/profiles', { signal })]);
    if (disposed) return;
    if (!identity?.user || !Array.isArray(data?.profiles)) throw new Error('Invalid account response');
    accountName.textContent = identity.user.name || identity.user.object_id;
    accountName.title = identity.user.tenant_id;
    profiles = data.profiles;
    select.replaceChildren(...profiles.map(item => {
      const option = node('option', item.name); option.value = item.id; return option;
    }));
    if (!profiles.length) select.append(node('option', '\u6682\u65e0\u8d44\u6e90\u914d\u7f6e'));
    select.disabled = !profiles.length;
    if (profiles.some(item => item.id === preferred)) select.value = preferred;
    choose(false);
    if (dialog.open) await providerStatus();
  }

  function fillOptions(id, values, selectedValue) {
    byId(id).replaceChildren(...values.map(item => {
      const option = node('option', item.name || item.id); option.value = item.id || item.name; return option;
    }));
    if (selectedValue) byId(id).value = selectedValue;
  }

  async function groups() {
    const generation = ++discoveryGeneration;
    const subscription = byId('resource-subscription').value;
    fillOptions('resource-group', []);
    if (!subscription) return;
    const result = await request('/api/resources/groups?' + new URLSearchParams({ subscription_id: subscription }), { signal });
    if (disposed || generation !== discoveryGeneration) return;
    fillOptions('resource-group', result.groups || []);
    await catalog();
  }

  async function catalog() {
    const generation = ++discoveryGeneration;
    const subscription = byId('resource-subscription').value;
    const group = byId('resource-group').value;
    for (const id of ['job-options', 'workspace-options', 'foundry-options', 'deployment-options']) fillOptions(id, []);
    if (!subscription || !group) return;
    const result = await request('/api/resources/catalog?' + new URLSearchParams({ subscription_id: subscription, group }), { signal });
    if (disposed || generation !== discoveryGeneration) return;
    for (const kind of ['job', 'workspace', 'foundry']) fillOptions(kind + '-options', result[kind] || []);
  }

  on(manage, 'click', () => {
    if (typeof dialog.showModal === 'function') dialog.showModal(); else dialog.setAttribute('open', '');
    editProfile(profile); perform(async () => { await providerStatus(); await initialization.refresh(); });
  });
  on(byId('resources-close'), 'click', () => {
    initialization.pause();
    if (typeof dialog.close === 'function') dialog.close(); else dialog.removeAttribute('open');
    for (const provider of ['alpha_vantage', 'fred']) byId('key-' + provider).value = '';
    manage.focus();
  });
  on(byId('profile-new'), 'click', () => editProfile(null));
  on(byId('profile-form'), 'submit', event => {
    event.preventDefault();
    const target = editing;
    const body = Object.fromEntries(PROFILE_FIELDS.map(field => [field, byId('profile-' + field).value.trim()]));
    perform(async () => {
      const saved = await request(target ? '/api/profiles/' + target.id : '/api/profiles', {
        method: target ? 'PUT' : 'POST', revision: target?.revision, body, signal,
      });
      await refresh(saved.id);
    });
  });
  on(byId('profile-delete'), 'click', () => { byId('profile-delete-confirm').hidden = false; });
  on(byId('profile-delete-confirm'), 'click', () => {
    const target = editing;
    if (target) perform(async () => { await request('/api/profiles/' + target.id, { method: 'DELETE', revision: target.revision, signal }); await refresh(); });
  });
  on(byId('provider-form'), 'submit', event => {
    event.preventDefault();
    const selectedId = profile?.id;
    const body = {};
    for (const provider of ['alpha_vantage', 'fred']) {
      const input = byId('key-' + provider);
      if (input.value) body[provider] = input.value;
      input.value = '';
    }
    if (selectedId && Object.keys(body).length) perform(async () => {
      await request(`/api/profiles/${selectedId}/providers`, { method: 'PUT', body, signal }); await providerStatus();
    });
  });
  for (const button of dialog.querySelectorAll('[data-disable-provider]')) on(button, 'click', () => {
    const selectedId = profile?.id;
    if (selectedId) perform(async () => {
      await request(`/api/profiles/${selectedId}/providers`, { method: 'PUT', body: { [button.dataset.disableProvider]: null }, signal });
      await providerStatus();
    });
  });
  on(byId('browse-resources'), 'click', () => perform(async () => {
    const result = await request('/api/resources/subscriptions', { signal });
    if (disposed) return;
    byId('resource-browser').hidden = false;
    fillOptions('resource-subscription', result.subscriptions || []); await groups();
  }));
  on(byId('resource-subscription'), 'change', () => perform(groups));
  on(byId('resource-group'), 'change', () => perform(catalog));
  on(byId('profile-foundry_account_id'), 'change', () => perform(async () => {
    const accountId = byId('profile-foundry_account_id').value.trim();
    fillOptions('deployment-options', []);
    if (!accountId) return;
    const result = await request('/api/resources/deployments?' + new URLSearchParams({ account_id: accountId }), { signal });
    if (!disposed && byId('profile-foundry_account_id').value.trim() === accountId) fillOptions('deployment-options', result.deployments || []);
  }));
  on(logout, 'click', () => perform(async () => {
    await request('/auth/logout', { method: 'POST', signal });
    onLogout?.(); doc.defaultView.location.assign('/signed-out');
  }));
  initialization = mountInitialization(doc, { container: dialog, request, perform, onProfile: refresh, signal, clock });
  on(dialog, 'cancel', () => initialization.pause());
  createIcons({ icons, root: dialog, attrs: { 'aria-hidden': 'true', 'stroke-width': 1.6 } });

  return {
    get profile() { return profile; },
    refresh, handleError,
    dispose() { disposed = true; offLocale(); initialization.dispose(); listeners.forEach(remove => remove()); root.remove(); dialog.remove(); accountName.remove(); logout.remove(); },
  };
}