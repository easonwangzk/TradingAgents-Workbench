import { bindLocale, onLocaleChange, translate } from './locale.mjs';

function node(doc, tag, text, className) {
  const element = doc.createElement(tag);
  if (text !== undefined) element.textContent = text;
  if (className) element.className = className;
  return element;
}

function selectedModel(doc, selectId, customId) {
  return doc.getElementById(selectId).value === 'custom'
    ? doc.getElementById(customId).value.trim()
    : doc.getElementById(selectId).value;
}

export function mountModelSettings(doc, { request, readDraft, today, onSaved }) {
  const root = node(doc, 'details', undefined, 'model-settings');
  root.open = false;
  root.innerHTML = `
    <summary>
      <span class="model-settings-summary"><strong>模型与凭据</strong><span id="settings-summary-status" class="muted small">未载入</span></span>
      <span id="settings-revision" class="muted small">未载入</span>
    </summary>
    <div class="model-settings-body">
      <p id="settings-message" class="inline-note" role="status"></p>
      <form id="model-settings-form" class="resource-form" novalidate>
        <label>运行凭据模式<select id="settings-mode"><option value="api_key">BYOK API Key</option><option value="managed_identity">托管身份</option></select></label>
        <label>模型提供商<select id="settings-provider"></select></label>
        <div class="field-pair"><label>快速模型<select id="settings-quick-model"></select><input id="settings-quick-custom" class="settings-custom" placeholder="自定义快速模型" hidden></label><label>深度模型<select id="settings-deep-model"></select><input id="settings-deep-custom" class="settings-custom" placeholder="自定义深度模型" hidden></label></div>
        <label id="settings-base-url-wrap">接口 Endpoint<input id="settings-base-url" autocomplete="url" spellcheck="false"></label>
        <label id="settings-api-version-wrap">Azure API 版本<input id="settings-api-version" autocomplete="off" spellcheck="false"></label>
        <label>模型 API Key <span id="settings-key-state" class="muted small"></span><input id="settings-api-key" type="password" autocomplete="new-password"></label>
        <div class="resource-actions"><button id="settings-clear-key" type="button">删除模型密钥</button></div>
        <div class="field-pair"><label>Alpha Vantage <span id="settings-alpha-state" class="muted small"></span><input id="settings-alpha" type="password" autocomplete="new-password"></label><label>FRED <span id="settings-fred-state" class="muted small"></span><input id="settings-fred" type="password" autocomplete="new-password"></label></div>
        <div class="resource-actions"><button id="settings-clear-alpha" type="button">删除 Alpha Key</button><button id="settings-clear-fred" type="button">删除 FRED Key</button><button id="settings-save" type="submit" class="primary-button">保存设置</button></div>
      </form>
    </div>`;
  doc.getElementById('research-advanced').prepend(root);
  root.dataset.localeRoot = '';
  bindLocale(doc, '.model-settings');

  const byId = id => doc.getElementById(id);
  const state = {
    catalog: null,
    settings: null,
    budget: null,
    csrfToken: '',
    pending: false,
    byokProvider: null,
    removeKeyProvider: null,
    removeData: new Set(),
  };
  const setMessage = value => { byId('settings-message').dataset.sourceMessage = value || ''; byId('settings-message').textContent = translate(value) || ''; };
  const offLocale = onLocaleChange(() => {
    byId('settings-message').textContent = translate(byId('settings-message').dataset.sourceMessage || '');
    byId('settings-revision').textContent = state.settings?.version || translate('未保存');
    for (const id of ['settings-key-state', 'settings-alpha-state', 'settings-fred-state']) {
      const target = byId(id);
      target.textContent = translate(target.dataset.sourceState || '');
    }
    renderSummary();
  });

  function renderSummary() {
    byId('settings-summary-status').textContent = state.settings?.mode === 'managed_identity'
      ? `${translate('托管身份')} · ${buildConfig().model}`
      : `${state.catalog?.providers?.[byId('settings-provider').value]?.label || 'BYOK'} · ${translate(state.settings?.model_key_configured ? '已配置密钥' : '待配置密钥')}`;
  }

  function clearSecrets() {
    byId('settings-api-key').value = '';
    byId('settings-alpha').value = '';
    byId('settings-fred').value = '';
  }

  function resetTransientState() {
    state.removeKeyProvider = null;
    state.removeData.clear();
    clearSecrets();
  }

  function currentProvider() {
    return byId('settings-provider').value || state.settings?.provider || Object.keys(state.catalog?.providers || {})[0] || 'openai';
  }

  function managedIdentityConfigurations() {
    const managed = state.catalog?.modes?.managed_identity;
    const explicit = Array.isArray(managed?.configurations) ? managed.configurations : [];
    const fallback = managed?.configuration ? [managed.configuration] : [];
    return explicit.length ? explicit : fallback.filter(item => item?.deployment || item?.model_family || item?.endpoint);
  }

  function managedIdentityAllowed() {
    const managed = state.catalog?.modes?.managed_identity;
    return Boolean(state.settings?.user?.is_admin && managed?.available && (managedIdentityConfigurations().length || managed?.configuration?.deployment));
  }

  function currentMode() {
    const mode = byId('settings-mode').value;
    return mode === 'managed_identity' && !managedIdentityAllowed() ? 'api_key' : mode;
  }

  function defaultManagedModel(kind) {
    const managed = state.catalog?.modes?.managed_identity;
    const configurations = managedIdentityConfigurations();
    const validSelection = configurations.some(config => config?.deployment === state.settings?.managed_models?.[`${kind}_model`])
      ? state.settings.managed_models[`${kind}_model`]
      : null;
    if (validSelection) return validSelection;
    const legacySelection = state.settings?.[`${kind}_model`];
    if (legacySelection && configurations.some(config => config?.deployment === legacySelection)) return legacySelection;
    const configuration = managed?.configuration || configurations[0] || {};
    if (configuration?.deployment) return configuration.deployment;
    const fallback = configurations.find(config => config?.deployment);
    return fallback?.deployment || '';
  }

  function defaultModel(provider, mode) {
    const models = state.catalog?.providers?.[provider]?.models?.[mode] || [];
    const first = models.find(option => option?.value && option.value !== 'custom');
    return first?.value || '';
  }

  function providerProfile(provider) {
    return state.settings?.providers?.[provider] || {};
  }

  function setPending(value) {
    state.pending = value;
    const mode = currentMode();
    for (const element of root.querySelectorAll('input, select, button')) {
      element.disabled = value;
    }
    if (!value) {
      byId('settings-mode').disabled = false;
      byId('settings-provider').disabled = mode === 'managed_identity';
      byId('settings-api-key').disabled = mode === 'managed_identity';
      byId('settings-clear-key').disabled = mode === 'managed_identity';
      refreshProviderView();
    }
  }

  function managedIdentityModel(kind) {
    const configurations = managedIdentityConfigurations();
    const values = configurations.map(config => config.deployment).filter(Boolean);
    const managedValue = state.settings?.managed_models?.[`${kind}_model`];
    if (managedValue && values.includes(managedValue)) return managedValue;
    const legacyValue = state.settings?.[`${kind}_model`];
    if (legacyValue && values.includes(legacyValue)) return legacyValue;
    return defaultManagedModel(kind);
  }

  function buildConfig() {
    const settings = state.settings || {};
    const selected = providerProfile(settings.provider);
    const deploymentReady = settings.mode === 'managed_identity'
      ? Boolean(settings.revision && managedIdentityAllowed())
      : Boolean(settings.provider && settings.quick_model && settings.deep_model && (selected.key_configured ?? settings.model_key_configured));
    const defaults = {
      mode: 'analysis',
      ticker: 'MSFT',
      analysis_date: today,
      asset_type: settings.asset_type || 'stock',
      analysts: ['market', 'social', 'news', 'fundamentals'],
      debate_rounds: 1,
      risk_rounds: 1,
      language: 'Chinese',
      data_vendors: structuredClone(settings.data_vendors || {}),
    };
    const managedDeployment = settings.mode === 'managed_identity' ? (managedIdentityModel('deep') || defaultManagedModel('deep') || '--') : (selected.deep_model || settings.deep_model || '--');
    return {
      defaults,
      analyst_choices: defaults.analysts.map(value => ({ value, label: value })),
      vendor_choices: structuredClone(state.catalog?.data_vendors || {}),
      model: managedDeployment,
      deployment_ready: deploymentReady,
      csrf_token: state.csrfToken,
      email_mode: true,
      settings_aligned: true,
    };
  }

  function fillModels(selectId, customId, models, value, fixed = false, allowCustom = true) {
    const select = byId(selectId);
    const options = [...(models || [])];
    const currentValue = select.value;
    if (allowCustom) options.push({ label: '自定义', value: 'custom' });
    select.replaceChildren(...options.map(option => {
      const element = node(doc, 'option', option.label);
      element.value = option.value;
      return element;
    }));

    const hasSavedValue = typeof value === 'string' && value.trim() !== '';
    const matched = (models || []).some(option => option.value === value);
    const customValue = hasSavedValue && !matched ? value : '';
    const fallbackValue = (hasSavedValue && options.some(option => option.value === value)) ? value
      : (hasSavedValue && allowCustom ? 'custom' : (options.some(option => option.value === currentValue) ? currentValue : (options[0]?.value || '')));
    const preferredValue = hasSavedValue ? (matched ? value : fallbackValue) : (options.some(option => option.value === currentValue) ? currentValue : (allowCustom ? 'custom' : (options[0]?.value || '')));
    select.value = preferredValue;
    select.disabled = state.pending || fixed;
    byId(customId).hidden = fixed || !allowCustom || (!hasSavedValue && select.value !== 'custom') || matched;
    byId(customId).disabled = state.pending || fixed || !allowCustom;
    byId(customId).value = customValue;
  }

  function managedIdentityValue(kind) {
    const values = managedIdentityConfigurations().map(config => config.deployment).filter(Boolean);
    const managedValue = state.settings?.managed_models?.[`${kind}_model`];
    if (managedValue && values.includes(managedValue)) return managedValue;
    const legacyValue = state.settings?.[`${kind}_model`];
    if (legacyValue && values.includes(legacyValue)) return legacyValue;
    const current = byId(`settings-${kind}-model`)?.value;
    if (current && values.includes(current)) return current;
    return defaultManagedModel(kind) || values[0] || '';
  }

  function fillProviders() {
    const managedIdentity = currentMode() === 'managed_identity';
    const providers = managedIdentity
      ? [['azure', { label: 'Azure Foundry (托管身份)' }]]
      : Object.entries(state.catalog?.providers || {});
    byId('settings-provider').replaceChildren(...providers.map(([value, meta]) => {
      const option = node(doc, 'option', meta.label || value);
      option.value = value;
      return option;
    }));
    byId('settings-provider').value = managedIdentity ? 'azure' : state.byokProvider;
  }

  function refreshProviderView() {
    const managedIdentity = currentMode() === 'managed_identity';
    const configuration = state.catalog?.modes?.managed_identity?.configuration;
    const managedOptions = managedIdentityConfigurations().map(config => ({ label: config.deployment || config.model_family || 'default', value: config.deployment || config.model_family || '', endpoint: config.endpoint || '' })).filter(option => option.value);
    const fixed = managedIdentity && managedOptions.length <= 1;
    const provider = currentProvider();
    const metadata = state.catalog?.providers?.[provider] || {};
    const profile = providerProfile(provider);
    const quickManagedValue = managedIdentityValue('quick');
    const deepManagedValue = managedIdentityValue('deep');
    fillModels('settings-quick-model', 'settings-quick-custom', managedIdentity ? managedOptions : metadata.models?.quick, managedIdentity ? quickManagedValue : (profile.quick_model || defaultModel(provider, 'quick')), fixed, !managedIdentity);
    fillModels('settings-deep-model', 'settings-deep-custom', managedIdentity ? managedOptions : metadata.models?.deep, managedIdentity ? deepManagedValue : (profile.deep_model || defaultModel(provider, 'deep')), fixed, !managedIdentity);
    byId('settings-base-url').value = managedIdentity ? (configuration?.endpoint || managedOptions[0]?.endpoint || '') : (profile.base_url || '');
    byId('settings-base-url').readOnly = managedIdentity;
    byId('settings-api-version').value = managedIdentity ? '' : (profile.api_version || '');
    byId('settings-base-url-wrap').hidden = !managedIdentity && !['azure', 'approved_origin'].includes(metadata.endpoint_kind);
    byId('settings-api-version-wrap').hidden = managedIdentity || metadata.endpoint_kind !== 'azure';
    byId('settings-base-url').disabled = state.pending || (!managedIdentity && !['azure', 'approved_origin'].includes(metadata.endpoint_kind));
    byId('settings-api-version').disabled = state.pending || metadata.endpoint_kind !== 'azure' || managedIdentity;
    byId('settings-key-state').dataset.sourceState = managedIdentity ? '无需 API Key' : (profile.key_configured ? '已配置' : '未配置');
    byId('settings-key-state').textContent = translate(byId('settings-key-state').dataset.sourceState);
    byId('settings-api-key').hidden = managedIdentity;
    byId('settings-clear-key').hidden = managedIdentity;
  }

  function render() {
    const providers = Object.entries(state.catalog?.providers || {});
    byId('settings-revision').textContent = state.settings?.version || translate('未保存');
    state.byokProvider = state.settings?.provider || providers[0]?.[0] || 'openai';
    byId('settings-mode').value = state.settings?.mode || 'api_key';
    fillProviders();
    const managedOption = byId('settings-mode').querySelector('option[value="managed_identity"]');
    managedOption.disabled = !managedIdentityAllowed();
    byId('settings-provider').disabled = state.pending || currentMode() === 'managed_identity';
    byId('settings-api-key').disabled = state.pending || currentMode() === 'managed_identity';
    byId('settings-clear-key').disabled = state.pending || currentMode() === 'managed_identity';
    byId('settings-alpha-state').dataset.sourceState = state.settings?.data_key_configured?.alpha_vantage ? '已配置' : '未配置';
    byId('settings-fred-state').dataset.sourceState = state.settings?.data_key_configured?.fred ? '已配置' : '未配置';
    byId('settings-alpha-state').textContent = translate(byId('settings-alpha-state').dataset.sourceState);
    byId('settings-fred-state').textContent = translate(byId('settings-fred-state').dataset.sourceState);
    renderSummary();
    refreshProviderView();
  }

  function buildPatch() {
    const draft = readDraft();
    const mode = currentMode();
    const provider = currentProvider();
    const metadata = state.catalog.providers[provider] || {};
    const patch = { mode, asset_type: draft.asset_type, data_vendors: draft.data_vendors };
    if (mode === 'managed_identity') {
      const selectable = managedIdentityConfigurations().length > 1 || Boolean(state.settings?.managed_models?.quick_model);
      if (selectable) {
        patch.managed_models = {
          quick_model: byId('settings-quick-model').value,
          deep_model: byId('settings-deep-model').value,
        };
      }
    } else {
      patch.provider = provider;
      patch.quick_model = selectedModel(doc, 'settings-quick-model', 'settings-quick-custom');
      patch.deep_model = selectedModel(doc, 'settings-deep-model', 'settings-deep-custom');
      if (['azure', 'approved_origin'].includes(metadata.endpoint_kind)) patch.base_url = byId('settings-base-url').value.trim();
      if (metadata.endpoint_kind === 'azure') patch.api_version = byId('settings-api-version').value.trim();
      if (byId('settings-api-key').value) patch.api_key = byId('settings-api-key').value;
      if (state.removeKeyProvider === provider && !byId('settings-api-key').value) patch.api_key = null;
    }
    const data_keys = {};
    if (byId('settings-alpha').value) data_keys.alpha_vantage = byId('settings-alpha').value;
    if (byId('settings-fred').value) data_keys.fred = byId('settings-fred').value;
    if (state.removeData.has('alpha_vantage')) data_keys.alpha_vantage = null;
    if (state.removeData.has('fred')) data_keys.fred = null;
    if (Object.keys(data_keys).length) patch.data_keys = data_keys;
    return patch;
  }

  byId('settings-provider').addEventListener('change', () => {
    state.byokProvider = currentProvider();
    state.removeKeyProvider = null;
    clearSecrets();
    refreshProviderView();
  });
  byId('settings-mode').addEventListener('change', () => {
    if (byId('settings-mode').value === 'managed_identity' && !managedIdentityAllowed()) byId('settings-mode').value = 'api_key';
    state.removeKeyProvider = null;
    clearSecrets();
    fillProviders();
    byId('settings-provider').disabled = currentMode() === 'managed_identity';
    byId('settings-api-key').disabled = currentMode() === 'managed_identity';
    byId('settings-clear-key').disabled = currentMode() === 'managed_identity';
    refreshProviderView();
  });
  byId('settings-quick-model').addEventListener('change', () => { byId('settings-quick-custom').hidden = byId('settings-quick-model').value !== 'custom'; });
  byId('settings-deep-model').addEventListener('change', () => { byId('settings-deep-custom').hidden = byId('settings-deep-model').value !== 'custom'; });
  byId('settings-api-key').addEventListener('input', () => { if (byId('settings-api-key').value) state.removeKeyProvider = null; });
  byId('settings-alpha').addEventListener('input', () => { if (byId('settings-alpha').value) state.removeData.delete('alpha_vantage'); });
  byId('settings-fred').addEventListener('input', () => { if (byId('settings-fred').value) state.removeData.delete('fred'); });
  byId('settings-clear-key').addEventListener('click', () => { state.removeKeyProvider = currentProvider(); byId('settings-api-key').value = ''; setMessage('模型密钥将于下次保存时删除。'); });
  byId('settings-clear-alpha').addEventListener('click', () => { state.removeData.add('alpha_vantage'); byId('settings-alpha').value = ''; setMessage('Alpha Vantage 密钥将于下次保存时删除。'); });
  byId('settings-clear-fred').addEventListener('click', () => { state.removeData.add('fred'); byId('settings-fred').value = ''; setMessage('FRED 密钥将于下次保存时删除。'); });
  byId('model-settings-form').addEventListener('submit', async event => {
    event.preventDefault();
    if (state.pending) return;
    setPending(true);
    setMessage('正在保存设置…');
    try {
      const result = await request('/api/settings', {
        method: 'PUT',
        revision: state.settings.version ?? undefined,
        body: buildPatch(),
      });
      state.settings = { ...result, user: state.settings.user };
      resetTransientState();
      render();
      setMessage('设置已保存。');
      onSaved?.({ catalog: state.catalog, settings: state.settings, budget: state.budget, config: buildConfig() });
    } catch (error) {
      resetTransientState();
      render();
      setMessage(error?.message || '设置保存失败，请稍后重试。');
    } finally {
      setPending(false);
    }
  });

  return {
    async refresh({ csrfToken, user }) {
      state.csrfToken = csrfToken;
      const [catalog, settings, budget] = await Promise.all([
        request('/api/settings/catalog'),
        request('/api/settings'),
        user?.is_admin ? request('/api/budget') : Promise.resolve(null),
      ]);
      state.catalog = catalog;
      state.settings = { ...settings, user };
      state.budget = budget;
      state.pending = false;
      state.removeKeyProvider = null;
      state.removeData.clear();
      render();
      const defaults = buildConfig().defaults;
      const selected = settings.providers?.[settings.provider] || {};
      return {
        catalog,
        config: buildConfig(),
        settings,
        budget,
      };
    },
    get budget() {
      return state.budget;
    },
    get aligned() {
      if (!state.settings) return true;
      const draft = readDraft();
      return JSON.stringify({ asset_type: draft.asset_type, data_vendors: draft.data_vendors })
        === JSON.stringify({ asset_type: state.settings.asset_type, data_vendors: state.settings.data_vendors });
    },
    syncMessage() {
      if (!this.aligned) setMessage('研究表单中的资产类型或数据源已变更，请先保存设置。');
      else if (byId('settings-message').dataset.sourceMessage?.includes('资产类型或数据源')) setMessage('');
    },
    clearTransientState() {
      resetTransientState();
      render();
    },
    dispose() {
      offLocale();
      resetTransientState();
      root.remove();
    },
  };
}