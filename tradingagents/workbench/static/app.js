import {
  ANALYST_LABELS, PROVIDER_LABELS, SECTION_LABELS, STATUS_LABELS, VENDOR_LABELS,
  buildRequest, createApi, downloadUrl, filterRuns, localDate, pollDelays, projectRun, vendorChains,
} from './ui-core.mjs';
import { createDOMPurify, createIcons, icons, marked } from './vendor.js';
import { mountPublicAccount } from './public-ui.js';
import { mountEmailAccount } from './account-ui.js';
import { mountModelSettings } from './model-settings.js';
import { mountStudio } from './studio-ui.js';
import { mergeAgentProcessEvents, mountAgentProcess } from './agent-process.mjs';
import { mountResearchAssistant } from './research-assistant.mjs';
import { mountMarket } from './market-data.mjs';
import { mountAdminWorkspace } from './admin-workspace.mjs';
import { bindLocale, mountLocale, onLocaleChange, translate, locale } from './locale.mjs';

const FIXED_MODEL = 'gpt-5.6-sol';
const STAGE_LABELS = { starting: '正在启动云端任务', running: '正在运行', collecting: '等待日志到达与结果校验', connectivity: '检查提供商连通性', analysis: '执行研究分析', initializing: '初始化研究分析', market: '市场分析', social: '社交情绪分析', news: '新闻分析', fundamentals: '基本面分析', bull_researcher: '看涨研究', bear_researcher: '看跌研究', research_manager: '投资辩论', trader: '交易决策', aggressive_risk: '激进风险评估', conservative_risk: '保守风险评估', neutral_risk: '中性风险评估', portfolio_manager: '投资组合管理', ...Object.fromEntries(Object.entries(PROVIDER_LABELS).map(([provider, label]) => [`connectivity:${provider}`, `连通检查 · ${label}`])) };
const FLOW_STAGES = [
  { label: '数据准备', stages: ['initializing'] },
  { label: '多维分析', stages: ['market', 'social', 'news', 'fundamentals'] },
  { label: '投资辩论', stages: ['bull_researcher', 'bear_researcher', 'research_manager', 'trader'] },
  { label: '风险评估', stages: ['aggressive_risk', 'conservative_risk', 'neutral_risk', 'portfolio_manager'] },
  { label: '报告生成', stages: [] },
];
const PROVIDER_STATUS = {
  ok: ['已连接', 'success'], success: ['已连接', 'success'], succeeded: ['已连接', 'success'], connected: ['已连接', 'success'],
  unconfigured: ['未配置', 'warning'], not_configured: ['未配置', 'warning'], missing_key: ['缺少密钥', 'warning'],
  skipped: ['已跳过', 'warning'], error: ['连接失败', 'danger'], failed: ['连接失败', 'danger'], unavailable: ['不可用', 'danger'],
};

export function mountWorkbench(doc, { fetchImpl = doc.defaultView.fetch.bind(doc.defaultView), clock = doc.defaultView } = {}) {
  const win = doc.defaultView;
  doc.__interfaceLocale = mountLocale(doc);
  const byId = id => doc.getElementById(id);
  const all = selector => [...doc.querySelectorAll(selector)];
  const purify = createDOMPurify(win);
  const lifecycle = new AbortController();
  const listeners = [];
  const downloadTimers = new Set();
  const objectUrls = new Set();
  let sessionVersion = 0;
  const state = {
    config: null, configReady: false, runs: [], selected: null, query: '', taskFilter: 'all', section: 'all', disposed: false, suspended: false,
    busy: false, stopping: false, refreshing: false, downloading: false, reconciling: false, historyLoading: true,
    historyError: '', runError: '', actionError: '', runTimer: null, historyTimer: null,
    runFlight: null, historyFlight: null, selectionVersion: 0, selectionController: null,
    reportSignature: '', eventsSignature: '', providerSignature: '', requestSignature: '', submission: null, sessionGeneration: 0,
    streamedEvents: [], streamSource: '', streamConnection: 'idle', streamRetryCount: 0,
    streamController: null, streamReconnectTimer: null, streamGeneration: 0, processMode: 'live',
    streamEnded: false, assistantOpen: false, marketOpen: false, managementOpen: false, permissions: { research: true, assistant: true, market: true }, page: 'home',
  };
  let account = null;
  let emailAccount = null;
  let modelSettings = null;
  let studio = null;
  let agentProcess = null;
  let researchAssistant = null;
  let market = null;
  let management = null;
  let researchLauncher = byId('new-research');
  let drawerOpenedFromHome = false;
  let historyNeedsRetry = false;
  let api = createApi({ fetchImpl });
  const isPublicMode = () => Boolean(state.config?.public_mode);
  const isEmailMode = () => Boolean(state.config?.email_mode);
  const element = (tag, text, className) => {
    const node = doc.createElement(tag);
    if (text !== undefined) node.textContent = String(text);
    if (className) node.className = className;
    return node;
  };
  const on = (target, name, callback) => { target.addEventListener(name, callback); listeners.push(() => target.removeEventListener(name, callback)); };
  const text = (id, value) => { byId(id).textContent = value; };
  const message = (id, value) => { text(id, value || ''); byId(id).hidden = !value; };
  const clearStreamReconnect = () => {
    if (state.streamReconnectTimer !== null) clock.clearTimeout(state.streamReconnectTimer);
    state.streamReconnectTimer = null;
  };
  const stopEventStream = ({ keepEvents = false } = {}) => {
    state.streamController?.abort();
    state.streamController = null;
    clearStreamReconnect();
    state.streamGeneration += 1;
    state.streamConnection = 'idle';
    state.streamRetryCount = 0;
    if (!keepEvents) { state.streamedEvents = []; state.streamSource = ''; state.streamEnded = false; }
  };
  const mergedRun = run => !run ? run : { ...run, events: mergeAgentProcessEvents(run.events, state.streamedEvents) };
  const formatNumber = value => value === null || value === undefined ? '--' : value.toLocaleString(locale());
  const formatTime = value => {
    const date = new Date(value);
    return Number.isFinite(date.getTime()) ? date.toLocaleString(locale(), { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false }) : '--';
  };
  const errorText = error => {
    if (error.status === 401) byId('reauthorize').hidden = false;
    account?.handleError(error);
    emailAccount?.handleError(error);
    return error.status || error.field || /令牌/.test(error.message) ? error.message : state.config?.public_mode
      ? '请求结果尚未确认，请刷新历史核对任务状态。' : '本地服务请求未确认，请刷新历史核对任务状态。';
  };
  const isActive = run => pollDelays(run?.status).run !== null;
  const statusLabel = run => isEmailMode() && run?.status === 'reconciliation_required'
    ? translate(run.stop_pending ? '停止处理中' : run.error_code === 'automatic_expired' ? '执行异常' : '正在恢复')
    : isEmailMode() && run?.status === 'prepared' ? translate('正在启动') : translate(STATUS_LABELS[run?.status]) || run?.status || '';
  const paintIcons = root => createIcons({ icons, root, attrs: { 'aria-hidden': 'true', 'stroke-width': 1.6 } });
  const researchDrawer = byId('research-drawer');
  byId('config-drawer-content').append(byId('side-config'));
  byId('side-config').append(byId('research-advanced'));
  const sourceCoverage = element('section', undefined, 'source-coverage');
  const sourceList = element('div', undefined, 'studio-sources');
  sourceCoverage.append(element('h2', '数据来源', 'inspector-title'), sourceList);
  byId('connectivity-panel').prepend(sourceCoverage);

  function setHome(active, page = active ? 'home' : 'workspace') {
    if (state.assistantOpen || state.marketOpen || state.managementOpen) return;
    state.page = page;
    doc.documentElement.dataset.home = String(page === 'home');
    doc.documentElement.dataset.screen = page;
    byId('workbench').dataset.screen = page;
    byId('workspace-home').hidden = page !== 'home';
    byId('workspace-tasks').hidden = page !== 'tasks';
    byId('workspace-run').hidden = page !== 'workspace';
    if (page === 'home') byId('research-tasks-entry').removeAttribute('aria-current');
    else byId('research-tasks-entry').setAttribute('aria-current', 'page');
    text('breadcrumb-studio', translate(page === 'home' ? '研究工作台' : page === 'tasks' ? '研究任务' : '研究报告'));
  }

  function setAssistantOpen(open) {
    if (open && state.managementOpen) management?.close();
    if (open && state.marketOpen) market?.close();
    state.assistantOpen = open;
    byId('research-assistant-screen').hidden = !open;
    if (open) {
      byId('research-tasks-entry').removeAttribute('aria-current');
      text('breadcrumb-studio', translate('研究助手'));
      if (win.innerWidth <= 850) setSidebar(false);
      byId('workspace-home').hidden = true;
      byId('workspace-tasks').hidden = true;
      byId('workspace-run').hidden = true;
      doc.documentElement.dataset.screen = 'assistant';
      byId('workbench').dataset.screen = 'assistant';
    } else {
      setHome(false, 'tasks');
    }
  }

  function openResearchDrawer(source) {
    if (!state.permissions.research) return;
    if (state.managementOpen) management?.close();
    if (state.marketOpen) market?.close();
    if (state.assistantOpen) researchAssistant?.close();
    researchLauncher = source?.currentTarget || source || byId('new-research');
    drawerOpenedFromHome = doc.documentElement.dataset.home === 'true';
    for (const details of all('#research-advanced details')) details.open = false;
    if (researchDrawer.open) return;
    if (typeof researchDrawer.showModal === 'function') researchDrawer.showModal();
    else researchDrawer.setAttribute('open', '');
    byId('ticker').focus();
  }

  function closeResearchDrawer() {
    if (!researchDrawer.open) return;
    if (typeof researchDrawer.close === 'function') researchDrawer.close();
    else researchDrawer.removeAttribute('open');
    if (drawerOpenedFromHome) setHome(true);
    drawerOpenedFromHome = false;
    if (researchLauncher?.isConnected && !researchLauncher.closest('[hidden]')) researchLauncher.focus();
  }

  function switchTab(attribute, selected) {
    if (attribute === 'data-view' && selected !== 'agent-process') agentProcess?.stopPlayback();
    for (const button of all(`[${attribute}]`)) {
      const active = button.getAttribute(attribute) === selected;
      button.setAttribute('aria-selected', String(active));
      button.tabIndex = active ? 0 : -1;
      byId(button.getAttribute('aria-controls')).hidden = !active;
    }
  }

  function setSidebar(open) {
    const toggle = byId('sidebar-toggle');
    byId('workbench').dataset.sidebarOpen = String(open);
    toggle.setAttribute('aria-expanded', String(open));
    const label = open ? '收起侧栏' : '展开侧栏';
    toggle.setAttribute('aria-label', translate(label));
    toggle.title = translate(label);
    const icon = toggle.querySelector('[data-lucide]');
    if (icon) {
      icon.setAttribute('data-lucide', open ? 'panel-left-close' : 'panel-left-open');
      paintIcons(toggle);
    }
    byId('sidebar-backdrop').hidden = !open || win.innerWidth > 850;
    byId('workspace').inert = open && win.innerWidth <= 850;
  }

  function updateControls() {
    const configured = Boolean(state.config);
    const modelReady = isPublicMode() ? Boolean(account?.profile?.config?.model)
      : isEmailMode() ? state.config?.deployment_ready === true && state.config?.settings_aligned !== false
        : state.config?.model === FIXED_MODEL;
    const deploymentReady = isPublicMode() ? Boolean(account?.profile) : state.config?.deployment_ready === true;
    const ready = state.permissions.research && state.configReady && deploymentReady && modelReady && Boolean(state.config?.csrf_token);
    const awaitingPreparation = run => isEmailMode() && ['preparing', 'prepared', 'reconciliation_required'].includes(run?.status);
    const reserved = run => isActive(run) || awaitingPreparation(run) || run?.start_pending || run?.stop_pending || run?.start_uncertain;
    const blocksSelection = run => reserved(run) && (!isPublicMode() ||
      !run?.profile_snapshot?.job_id || run.profile_snapshot.job_id.toLowerCase() === account?.profile?.job_id?.toLowerCase());
    const occupied = state.runs.some(blocksSelection) || blocksSelection(state.selected);
    byId('config-fields').disabled = !configured;
    byId('reset-draft').disabled = !configured;
    byId('start-run').disabled = !ready || occupied || state.busy;
    byId('test-connectivity').disabled = !ready || occupied || state.busy;
    byId('rerun').disabled = !ready || occupied || state.busy || !state.selected ||
      (isPublicMode() && state.selected.profile_id !== account?.profile?.id);
    byId('reconcile-submit').disabled = isEmailMode() || !state.config?.csrf_token || state.reconciling || !state.selected?.start_uncertain;
    byId('resume-actions').hidden = true;
    byId('resume-run').hidden = true;
    byId('resume-run').disabled = true;
    byId('stop-run').disabled = !state.config?.csrf_token || !(isActive(state.selected) || awaitingPreparation(state.selected)) || state.stopping || state.busy;
    byId('download-json').disabled = !state.selected || state.downloading;
    const view = projectRun(state.selected);
    byId('download-markdown').disabled = !state.selected || state.downloading || !(view.reports.length || view.decision || view.connectivity.length);
    byId('refresh').disabled = state.refreshing;
    byId('load-request').disabled = !configured || !state.selected;
    const fundVendor = byId('vendor-fundamental_data');
    for (const select of all('#vendor-options select')) select.disabled = !configured;
    if (fundVendor) {
      const isEtf = byId('asset_type').value === 'etf';
      if (isEtf) fundVendor.value = 'yfinance';
      fundVendor.disabled = !configured || isEtf;
      fundVendor.title = isEtf ? translate('ETF 基金资料使用 Yahoo Finance 原生接口') : '';
    }
    const mode = doc.querySelector('input[name="mode"]:checked')?.value;
    text('start-label', translate(state.busy ? '正在提交…' : mode === 'connectivity' ? '开始测试' : '开始研究'));
    for (const button of all('[data-step]')) {
      const value = Number(byId(button.dataset.for).value);
      button.disabled = !configured || (Number(button.dataset.step) < 0 ? value <= 1 : value >= 3);
    }
    const blockedReason = occupied ? '已有任务运行，等待结束或停止后再提交'
      : isEmailMode() && state.config?.settings_aligned === false ? '研究表单中的资产或数据源与已保存设置不一致，请先保存设置。'
        : !ready ? '部署或会话尚未就绪' : '';
    byId('start-run').title = translate(blockedReason || '提交当前研究配置');
    byId('test-connectivity').title = translate(blockedReason || '以当前配置测试提供商连接');
  }

  function populateForm(request) {
    for (const field of ['ticker', 'analysis_date', 'asset_type', 'debate_rounds', 'risk_rounds', 'language']) byId(field).value = request[field];
    for (const radio of all('[name="mode"]')) radio.checked = radio.value === request.mode;
    for (const checkbox of all('[name="analysts"]')) checkbox.checked = request.analysts.includes(checkbox.value);
    for (const [category, chain] of Object.entries(request.data_vendors)) {
      const select = byId(`vendor-${category}`);
      if (select) select.value = chain;
    }
    message('form-error', '');
    all('[aria-invalid]').forEach(node => node.removeAttribute('aria-invalid'));
    updateControls();
  }

  function readDraft(mode) {
    const values = {};
    for (const field of ['ticker', 'analysis_date', 'asset_type', 'language', 'debate_rounds', 'risk_rounds']) values[field] = byId(field).value;
    values.mode = mode || doc.querySelector('[name="mode"]:checked')?.value;
    values.analysts = all('[name="analysts"]:checked').map(node => node.value);
    values.data_vendors = Object.fromEntries(Object.keys(VENDOR_LABELS).map(category => [category, byId(`vendor-${category}`).value]));
    return values;
  }

  function draftRequest(mode) {
    const values = readDraft(mode);
    return buildRequest({ ...values, debate_rounds: Number(values.debate_rounds), risk_rounds: Number(values.risk_rounds) }, state.config);
  }

  function renderConfigOptions(config) {
    const analysts = config.analyst_choices.filter(choice => Object.hasOwn(ANALYST_LABELS, choice.value)).map(choice => {
      const label = element('label');
      const checkbox = element('input');
      checkbox.type = 'checkbox'; checkbox.name = 'analysts'; checkbox.value = choice.value;
      label.append(checkbox, element('span', choice.label && choice.label !== choice.value ? choice.label : ANALYST_LABELS[choice.value]));
      return label;
    });
    byId('analyst-options').replaceChildren(...analysts);
    byId('vendor-options').replaceChildren(...Object.entries(VENDOR_LABELS).map(([category, labelText]) => {
      const label = element('label', labelText);
      const select = element('select'); select.id = `vendor-${category}`; select.name = category;
      label.htmlFor = select.id;
      for (const chain of vendorChains(config.vendor_choices[category] || [])) {
        const option = element('option', chain.split(',').map(provider => PROVIDER_LABELS[provider] || provider).join(' → '));
        option.value = chain; select.append(option);
      }
      label.append(select);
      return label;
    }));
    byId('analyst-options').dataset.localeRoot = '';
    byId('vendor-options').dataset.localeRoot = '';
    bindLocale(doc, '#analyst-options, #vendor-options');
  }

  function currentDraftValues() {
    return {
      asset_type: byId('asset_type').value || 'stock',
      data_vendors: Object.fromEntries(Object.keys(VENDOR_LABELS).map(category => [category, byId(`vendor-${category}`)?.value || ''])),
    };
  }

  function applyEmailConfig(identity, result) {
    const previous = state.config?.defaults;
    state.config = {
      ...result.config,
      defaults: {
        ...result.config.defaults,
        ...(previous ? {
          ticker: previous.ticker,
          analysis_date: previous.analysis_date,
          analysts: previous.analysts,
          debate_rounds: previous.debate_rounds,
          risk_rounds: previous.risk_rounds,
          language: previous.language,
        } : {}),
      },
      csrf_token: identity.csrf_token,
      settings_aligned: true,
    };
    state.configReady = true;
    if (result.catalog) {
      researchAssistant?.setAvailable(Boolean(identity.user)
        && state.permissions.assistant && result.catalog.assistant_available === true);
    }
    byId('reauthorize').textContent = '重新登录';
    byId('reauthorize').href = '/auth/login';
    byId('service-state').dataset.sourceLabel = state.config.deployment_ready ? '设置就绪' : '设置未完成';
    text('service-state', translate(byId('service-state').dataset.sourceLabel));
    byId('service-state').dataset.ready = String(Boolean(state.config.deployment_ready));
    text('model-name', state.config.model || '--');
    message('config-note', state.config.settings_aligned ? '' : '研究表单中的资产类型或数据源已变更，请先保存设置。');
  }

  function ensureEmailAccount(identity) {
    emailAccount ||= mountEmailAccount(doc, {
      request: (...args) => api(...args),
      onSession: session => {
        stopEventStream();
        sessionVersion += 1;
        state.sessionGeneration += 1;
        state.historyFlight = null;
        api = createApi({ token: session.csrf_token, fetchImpl, emailMode: true });
        researchAssistant?.setAvailable(false);
        market?.setSession(null);
        management?.setSession(null);
        emailAccount?.setSession(session);
        const generation = state.sessionGeneration;
        void loadConfig().then(() => {
          if (!state.disposed && generation === state.sessionGeneration && state.configReady) void loadHistory();
        });
      },
      onLogout: () => {
        stopEventStream();
        researchAssistant?.setAvailable(false);
        market?.setSession(null);
        management?.setSession(null);
        agentProcess?.stopPlayback();
        sessionVersion += 1;
        state.sessionGeneration += 1;
        state.config = null;
        state.configReady = false;
        state.runs = [];
        state.selected = null;
        state.submission = null;
        studio?.clear();
        state.historyError = '';
        state.runError = '';
        state.actionError = '';
        state.selectionController?.abort();
        state.historyFlight = null;
        state.runFlight = null;
        api = createApi({ fetchImpl, emailMode: true });
        emailAccount?.setSession(null);
        modelSettings?.clearTransientState?.();
        clearPolls();
        renderWorkspace();
        renderHistory();
      },
    });
    emailAccount.setSession(identity);
  }

  async function loadEmailConfig() {
    const revision = ++sessionVersion;
    const identityApi = createApi({ fetchImpl, emailMode: true });
    const identity = await identityApi('/api/me', { signal: lifecycle.signal });
    if (state.disposed || revision !== sessionVersion) return true;
    const emailApi = createApi({ token: identity.csrf_token, fetchImpl, emailMode: true });
    api = emailApi;
    ensureEmailAccount(identity);
    state.permissions = Object.fromEntries(['research', 'assistant', 'market'].map(key => [key, (identity.permissions || identity.user?.permissions)?.[key] !== false]));
    byId('research-tasks-entry').hidden = !state.permissions.research;
    for (const button of all('[data-open-research], #new-research')) button.hidden = !state.permissions.research;
    if (!state.permissions.research) closeResearchDrawer();
    if (!state.permissions.assistant) researchAssistant?.setAvailable(false);
    market?.setSession(state.permissions.market ? identity.user?.object_id || identity.user?.id || null : null);
    management?.setSession(identity);
    const settingsModule = modelSettings || mountModelSettings(doc, {
      request: (...args) => api(...args),
      readDraft: currentDraftValues,
      today: localDate(),
      onSaved: result => {
        applyEmailConfig({ user: state.config?.user || identity.user, csrf_token: result.config.csrf_token }, result);
        emailAccount?.setBudget(result.budget);
        renderWorkspace();
      },
    });
    let result;
    try {
      result = await settingsModule.refresh({ csrfToken: identity.csrf_token, user: identity.user });
      const validCatalog = result?.catalog && typeof result.catalog === 'object' && result.catalog.providers && typeof result.catalog.providers === 'object';
      const validSettings = result?.settings && typeof result.settings === 'object' && result.settings.providers && typeof result.settings.providers === 'object' && typeof result.settings.mode === 'string';
      if (!validCatalog || !validSettings) {
        const error = new Error('Not email mode');
        error.status = 404;
        throw error;
      }
    } catch (error) {
      if (!modelSettings) settingsModule.dispose();
      throw error;
    }
    if (state.disposed || revision !== sessionVersion) return true;
    api = emailApi;
    modelSettings ||= settingsModule;
    emailAccount.setSession(identity);
    emailAccount.setBudget(result.budget);
    state.config = { ...(state.config || {}), user: identity.user };
    applyEmailConfig(identity, result);
    return true;
  }

  async function loadConfig() {
    const generation = state.sessionGeneration;
    try {
      let config;
      try {
        const loaded = await loadEmailConfig();
        if (loaded && !state.disposed && generation === state.sessionGeneration) {
          const firstLoad = !byId('ticker').value;
          renderConfigOptions(state.config);
          byId('analysis_date').max = localDate();
          if (firstLoad) populateForm(state.config.defaults);
          state.config.settings_aligned = modelSettings?.aligned ?? true;
          message('config-note', state.config.settings_aligned ? '' : '研究表单中的资产类型或数据源已变更，请先保存设置。');
          updateControls();
          return;
        }
      } catch (emailError) {
        if (![401, 403, 404].includes(emailError?.status)) throw emailError;
      }
      config = await createApi({ fetchImpl })('/api/config', { signal: lifecycle.signal });
      if (state.disposed || generation !== state.sessionGeneration) return;
      if (!config?.defaults || !Array.isArray(config.analyst_choices) || !config.vendor_choices || typeof config.csrf_token !== 'string') throw new Error('Invalid configuration');
      buildRequest(config.defaults, config);
      const firstLoad = !state.config;
      const draft = firstLoad ? config.defaults : readDraft();
      state.config = config;
      state.configReady = true;
      api = createApi({ token: config.csrf_token, fetchImpl, publicMode: Boolean(config.public_mode) });
      if (config.public_mode) {
        account ||= mountPublicAccount(doc, { request: (...args) => api(...args), signal: lifecycle.signal, clock,
          onLogout: () => { market?.setSession(null); management?.setSession(null); state.configReady = false; state.suspended = true; state.runs = []; state.selected = null; studio?.clear(); clearPolls(); renderWorkspace(); renderHistory(); },
          onSelect: profile => {
            state.config.deployment_ready = Boolean(profile);
            text('model-name', profile?.config?.model || '--');
            byId('service-state').dataset.sourceLabel = profile ? '部署就绪' : '未选择资源';
            text('service-state', translate(byId('service-state').dataset.sourceLabel));
            byId('service-state').dataset.ready = String(Boolean(profile));
            message('config-note', profile ? '' : '尚未选择 Azure 资源配置。');
            updateControls();
          } });
        await account.refresh();
      }
      renderConfigOptions(config);
      byId('analysis_date').max = localDate();
      populateForm(draft);
      const ready = config.deployment_ready === true && (config.public_mode ? Boolean(account?.profile) : config.model === FIXED_MODEL);
      byId('service-state').dataset.sourceLabel = ready ? '部署就绪' : '部署未就绪';
      text('service-state', translate(byId('service-state').dataset.sourceLabel));
      byId('service-state').dataset.ready = String(ready);
      message('config-note', !config.public_mode && config.model !== FIXED_MODEL ? '模型配置不匹配，当前禁止提交。' : !ready ? 'Azure 任务尚未部署就绪。' : '');
    } catch (error) {
      if (state.disposed || generation !== state.sessionGeneration) return;
      state.configReady = false;
      byId('service-state').dataset.sourceLabel = '连接异常';
      text('service-state', translate(byId('service-state').dataset.sourceLabel));
      byId('service-state').dataset.ready = 'false';
      message('config-note', errorText(error));
    }
    updateControls();
  }

  function renderHistory() {
    const filtered = filterRuns(state.runs, state.query).filter(run => state.taskFilter === 'all'
      || state.taskFilter === 'active' && isActive(run)
      || state.taskFilter === 'completed' && run.status === 'succeeded'
      || state.taskFilter === 'interrupted' && ['failed', 'stopped'].includes(run.status));
    message('history-error', state.historyError);
    text('history-state', translate(state.historyLoading && !state.runs.length ? '正在读取历史…' : state.historyError && !state.runs.length ? '历史暂不可用' : state.runs.length ? '没有匹配的运行记录' : '暂无运行记录'));
    byId('history-state').hidden = filtered.length > 0;
    byId('history-list').replaceChildren(...filtered.map(run => {
      const button = element('button', undefined, 'history-item'); button.type = 'button';
      button.setAttribute('aria-current', String(run.id === state.selected?.id));
      const status = element('span', statusLabel(run), 'status'); status.dataset.status = run.status;
      button.append(element('strong', run.request?.ticker || run.id), status,
        element('small', `${run.request?.analysis_date || '--'} · ${formatTime(run.created_at)}`),
        element('small', translate(run.request?.mode === 'connectivity' ? '连通测试' : '研究分析'), 'history-mode'));
      button.title = `${run.id}\n${run.execution_name || '尚无执行名称'}`;
      button.addEventListener('click', () => { selectRun(run); if (win.innerWidth <= 850) setSidebar(false); });
      return button;
    }));
    studio?.sync();
    updateControls();
  }

  function upsert(run) {
    state.runs = [run, ...state.runs.filter(item => item.id !== run.id)].sort((first, second) => String(second.created_at).localeCompare(String(first.created_at)));
  }

  function safeMarkdown(container, content) {
    const fragment = purify.sanitize(marked.parse(content, { async: false, gfm: true }), {
      RETURN_DOM_FRAGMENT: true, USE_PROFILES: { html: true },
      FORBID_TAGS: ['img', 'style', 'iframe', 'form', 'input', 'button', 'textarea', 'video', 'audio', 'source', 'object', 'embed'],
      FORBID_ATTR: ['style', 'id', 'name'],
    });
    for (const anchor of fragment.querySelectorAll('a')) {
      if (!/^https?:\/\//i.test(anchor.getAttribute('href') || '')) anchor.removeAttribute('href');
      else { anchor.target = '_blank'; anchor.rel = 'noopener noreferrer'; }
    }
    container.replaceChildren(fragment);
  }

  function filterSections() {
    for (const article of all('.report-article')) article.hidden = state.section !== 'all' && article.dataset.section !== state.section;
    for (const button of all('.section-button')) button.setAttribute('aria-pressed', String(button.dataset.section === state.section));
    byId('decision').hidden = !projectRun(state.selected).decision || state.section !== 'all';
  }

  function renderReports(view) {
    const signature = JSON.stringify([state.selected?.id, view.reports, view.decision]);
    if (signature !== state.reportSignature) {
      state.reportSignature = signature;
      if (state.section !== 'all' && !view.reports.some(report => report.section === state.section)) state.section = 'all';
      const sections = [{ section: 'all', label: translate('全部章节') }, ...view.reports.map(report => ({ section: report.section, label: translate(SECTION_LABELS[report.section]) || report.section.replaceAll('_', ' ') }))];
      byId('section-list').replaceChildren(...sections.map(section => {
        const button = element('button', section.label, 'section-button'); button.type = 'button'; button.dataset.section = section.section;
        button.addEventListener('click', () => { state.section = section.section; filterSections(); byId('report-reader').scrollTop = 0; });
        return button;
      }));
      byId('report-articles').replaceChildren(...view.reports.map(report => {
        const article = element('article', undefined, 'report-article'); article.dataset.section = report.section;
        const heading = element('h2', translate(SECTION_LABELS[report.section]) || report.section.replaceAll('_', ' '));
        const body = element('div', undefined, 'report-body');
        safeMarkdown(body, report.content); article.append(heading, body); return article;
      }));
      text('decision-text', view.decision);
      filterSections();
    }
    text('report-count', String(view.reports.length));
    byId('report-empty').hidden = Boolean(view.reports.length || view.decision);
    text('report-empty-detail', translate(state.selected?.status === 'collecting' ? '容器已结束，等待日志到达并校验完整结果。' : view.active ? '任务进行中，尚未收到报告。' : state.selected?.status === 'failed' ? '任务失败，尚无可用报告。' : state.selected?.status === 'stopped' ? '任务已停止，尚未生成报告。' : '尚无已验证的任务结果'));
  }

  function renderProviders(view) {
    const signature = JSON.stringify(view.connectivity);
    if (signature === state.providerSignature) return;
    state.providerSignature = signature;
    byId('provider-empty').hidden = view.connectivity.length > 0;
    byId('provider-table').hidden = !view.connectivity.length;
    text('provider-count', view.connectivity.length ? `${formatNumber(view.connectivity.length)} ${translate('个提供商')}` : translate('未测试'));
    byId('provider-rows').replaceChildren(...view.connectivity.map(provider => {
      const row = element('tr');
      const [label, tone] = PROVIDER_STATUS[provider.status] || [provider.status || '未知', 'warning'];
      const status = element('span', translate(label), 'status'); status.dataset.tone = tone;
      const cell = element('td'); cell.append(status);
      const elapsed = typeof provider.elapsed_seconds === 'number' && Number.isFinite(provider.elapsed_seconds) && provider.elapsed_seconds >= 0 ? `${provider.elapsed_seconds.toFixed(2)} s` : '--';
      row.append(element('td', PROVIDER_LABELS[provider.provider] || provider.provider || '--'), cell,
        element('td', elapsed, 'mono'), element('td', typeof provider.detail === 'string' ? provider.detail : '--'));
      return row;
    }));
  }

  function renderEvents(run) {
    const events = Array.isArray(run?.events) ? run.events.filter(event => event && typeof event === 'object') : [];
    const kind = byId('event-kind').value;
    const query = byId('event-query').value.trim().toLowerCase();
    const signature = JSON.stringify([run?.id, events, kind, query]);
    if (signature === state.eventsSignature) return;
    state.eventsSignature = signature;
    const opened = new Set(all('#event-list details[open]').map(node => node.dataset.eventKey));
    const focused = doc.activeElement?.closest('#event-list details')?.dataset.eventKey;
    const existing = new Set(all('#event-list li').map(node => node.dataset.eventKey));
    const filtered = events.map((event, index) => ({ event, index })).filter(({ event }) => (kind === 'all' || kind === event.kind) && (!query || JSON.stringify(event).toLowerCase().includes(query)));
    text('event-count', String(events.length));
    text('event-filter-count', `${filtered.length} / ${events.length} · UTC`);
    byId('events-empty').hidden = filtered.length > 0;
    text('events-empty', translate(events.length ? '没有匹配的运行事件' : '尚无运行事件'));
    byId('event-list').replaceChildren(...filtered.reverse().map(({ event, index }) => {
      const row = element('li'); row.dataset.kind = event.kind;
      const key = `${run.id}:${index}`; row.dataset.eventKey = key;
      if (existing.size && !existing.has(key) && kind === 'all' && !query) row.classList.add('studio-arrive');
      const payload = event.payload || {};
      const names = { phase: '阶段', report: '报告', usage: '用量', connectivity: '连通', result: '结果', error: '错误' };
      const detail = event.kind === 'phase' ? translate(STAGE_LABELS[payload.stage]) || payload.stage
        : event.kind === 'report' ? `${translate(SECTION_LABELS[payload.section]) || payload.section || translate('报告')} · ${typeof payload.content === 'string' ? payload.content.length : 0} ${translate('字符')}`
          : event.kind === 'usage' ? `${translate('模型')} ${formatNumber(payload.llm_calls)} · ${translate('工具')} ${formatNumber(payload.tool_calls)} · ${translate('输入')} ${formatNumber(payload.input_tokens)} · ${translate('输出')} ${formatNumber(payload.output_tokens)}`
            : event.kind === 'connectivity' ? `${translate('已检查')} ${Array.isArray(payload.connectivity) ? payload.connectivity.length : 0} ${translate('个提供商')}`
            : event.kind === 'result' ? translate('已接收结果事件') : event.kind === 'error' ? payload.message : event.kind;
      const time = element('time', event.timestamp || '--'); time.dateTime = event.timestamp || ''; time.title = event.timestamp || '';
      const content = element('div', undefined, 'event-content');
      const disclosure = element('details'); disclosure.dataset.eventKey = key; disclosure.open = opened.has(key);
      disclosure.append(element('summary', translate('完整事件 JSON')), element('pre', JSON.stringify(event, null, 2), 'raw-json'));
      content.append(element('p', typeof detail === 'string' ? detail : '--'), disclosure);
      row.append(time, element('span', translate(names[event.kind]) || event.kind), content);
      return row;
    }));
    if (focused) all('#event-list details').find(node => node.dataset.eventKey === focused)?.querySelector('summary')?.focus({ preventScroll: true });
  }

  function renderFlow(run, view) {
    const events = Array.isArray(run?.events) ? run.events : [];
    const observed = events.filter(event => event?.kind === 'phase' && typeof event.payload?.stage === 'string');
    const stages = observed.map(event => event.payload.stage);
    const failed = ['failed', 'stopped'].includes(run?.status);
    const connectivity = run?.request?.mode === 'connectivity';
    const latest = stages.at(-1) || '';
    const latestKnown = [...stages].reverse().find(value => FLOW_STAGES.some(stage => stage.stages.includes(value))) || '';
    const latestPhaseIndex = FLOW_STAGES.findIndex(stage => stage.stages.includes(latestKnown));
    const reportObserved = view.reports.some(report => report.section === 'final_trade_decision');
    const successfulAnalysis = run?.request?.mode === 'analysis' && run?.status === 'succeeded';
    const complete = !failed && successfulAnalysis;
    const reportIndex = FLOW_STAGES.length - 1;
    const latestFlowIndex = Math.max(latestPhaseIndex, reportObserved ? reportIndex : -1);
    const currentIndex = failed ? latestPhaseIndex
      : !complete && reportObserved ? reportIndex
        : run?.status === 'running' ? latestPhaseIndex : -1;
    const showObservedProgress = !['prepared', 'reconciliation_required'].includes(run?.status);
    byId('flow-section').hidden = connectivity;
    if (connectivity) {
      byId('flow-dot').dataset.status = 'idle';
      text('stage-description', statusLabel(run));
      byId('phase-list').replaceChildren();
      return;
    }
    byId('flow-dot').dataset.status = failed ? run.status : complete ? 'succeeded' : view.active ? 'running' : 'idle';
    const description = !run ? '尚未启动' : failed ? statusLabel(run) : complete ? '研究已完成' : reportObserved ? '报告生成 · 等待结果校验' : run.status === 'collecting' ? '报告收集中 · 等待日志到达与结果校验' : latest.startsWith('tool:') || latest === 'tool' ? '调用研究工具' : latest ? STAGE_LABELS[latest] || latest : statusLabel(run);
    text('stage-description', translate(description));
    byId('phase-list').replaceChildren(...FLOW_STAGES.map((stage, index) => {
      const item = element('li', undefined, 'flow-stage');
      const wasObserved = stage.stages.some(value => stages.includes(value));
      const reportStageObserved = index === reportIndex && reportObserved;
      if (complete || (showObservedProgress && latestFlowIndex >= 0 && index < latestFlowIndex)) item.classList.add('done');
      else if (failed && currentIndex === index) item.classList.add(run.status === 'stopped' ? 'interrupted' : 'failed');
      else if (!failed && !complete && currentIndex === index) item.classList.add('current');
      const marker = element('span', String(index + 1), 'flow-marker');
      const label = element('span', translate(stage.label), 'flow-label');
      const detail = element('small', wasObserved || reportStageObserved ? translate('已记录') : '--', 'flow-detail');
      item.append(marker, label, detail);
      item.setAttribute('aria-current', item.classList.contains('current') ? 'step' : 'false');
      return item;
    }));
  }

  function renderSources(run, view) {
    const request = run?.request;
    const observed = new Map(view.connectivity.map(provider => [provider.provider, provider]));
    const configured = new Set(Object.values(request?.data_vendors || {}).flatMap(value => typeof value === 'string' ? value.split(',').map(name => name.trim()).filter(Boolean) : []));
    const providers = [...new Set([...configured, ...observed.keys()])];
    byId('studio-sources').replaceChildren(...providers.map(provider => {
      const row = element('div', undefined, 'source-row');
      const identity = element('span', PROVIDER_LABELS[provider] || provider);
      const result = observed.get(provider);
      const status = result ? (PROVIDER_STATUS[result.status] || [result.status || '未知'])[0] : configured.has(provider) ? '已配置 · 尚未验证' : '未配置';
      const stateLabel = element('span', translate(status), `source-status${result ? ` tone-${PROVIDER_STATUS[result.status]?.[1] || 'warning'}` : ''}`);
      row.append(identity, stateLabel);
      return row;
    }));
    sourceList.replaceChildren(...Array.from(byId('studio-sources').children, row => row.cloneNode(true)));
    sourceCoverage.hidden = providers.length === 0;
  }

  function renderWorkspace() {
    const run = mergedRun(state.selected);
    const view = projectRun(run);
    text('run-title', run ? `${run.request?.ticker || '--'} / ${translate(run.request?.mode === 'connectivity' ? '连通测试' : '研究分析')}` : translate('尚未选择任务'));
    text('run-mode', translate(run?.request?.mode === 'connectivity' ? '连通测试' : '研究分析'));
    text('run-meta', run ? `${run.request?.analysis_date || '--'} · ${run.request?.language === 'English' ? 'English' : translate('中文')} · ${formatTime(run.created_at)}${run.profile_snapshot ? ' · ' + run.profile_snapshot.name + ' · ' + run.profile_snapshot.config.model : ''}` : translate('等待任务记录'));
    text('run-status', statusLabel(run));
    byId('run-status').hidden = !run; byId('run-status').dataset.status = run?.status || '';
    byId('activity-dot').dataset.active = String(view.active);
    text('stage', translate(!run ? '尚未启动' : run.status === 'collecting' ? '报告收集中 · 等待日志到达与结果校验' : ['prepared', 'reconciliation_required'].includes(run.status) || !view.active ? statusLabel(run) : STAGE_LABELS[view.stage] || view.stage || statusLabel(run)));
    const automaticError = isEmailMode() ? {
      reconciliation_required: '',
      automatic_expired: '执行恢复已超时，正在确认停止状态，请联系管理员。',
      authorization_changed: '账户授权已变更，任务已取消或正在停止。',
    }[run?.error_code] : undefined;
    message('workspace-error', state.actionError || state.runError || (automaticError ?? view.error));
    byId('reconcile-form').hidden = !isPublicMode() || !run?.start_uncertain;
    byId('reconcile-execution').required = isPublicMode();
    byId('reconcile-execution').disabled = isEmailMode();
    byId('reconcile-execution').parentElement.hidden = isEmailMode();
    byId('request-details').hidden = !run;
    text('execution-name', run?.execution_name || translate('尚无执行名称'));
    const requestSignature = JSON.stringify([run?.id, run?.request, run?.profile_snapshot, run?.settings]);
    if (requestSignature !== state.requestSignature) {
      state.requestSignature = requestSignature;
      const request = run?.request;
      const details = request ? [
        ['运行 ID', run.id], ['运行模式', request.mode], ['证券代码', request.ticker], ['分析日期', request.analysis_date], ['报告语言', request.language],
        ['模型快照', run.settings ? JSON.stringify(run.settings) : run.profile_snapshot ? JSON.stringify(run.profile_snapshot) : request.model || '未记录'],
        ['分析师', request.analysts?.map(value => ANALYST_LABELS[value] || value).join('、')],
        ['辩论 / 风险', `${request.debate_rounds} / ${request.risk_rounds}`], ['资产类型', request.asset_type],
        ...Object.entries(request.data_vendors || {}).map(([category, value]) => [VENDOR_LABELS[category] || category, value]),
      ] : [];
      byId('request-values').replaceChildren(...details.flatMap(([label, value]) => [element('dt', translate(label)), element('dd', value || '--')]));
      text('request-json', request ? JSON.stringify(request, null, 2) : '');
    }
    const historicalModels = run?.settings ? [run.settings.quick_model, run.settings.deep_model].filter(Boolean).join(' / ') || (run.settings.mode === 'managed_identity' ? 'Foundry 托管身份 · 未记录模型名称' : '未记录模型名称') : run?.profile_snapshot?.config?.model || run?.request?.model;
    text('studio-selected-model', historicalModels || (run ? '未记录模型名称' : '--'));
    byId('studio-team').replaceChildren(...(run?.request?.analysts || []).map(analyst => element('div', ANALYST_LABELS[analyst] || analyst)));
    const metricGrid = doc.querySelector('.metric-grid');
    metricGrid?.classList.toggle('has-values', Object.values(view.stats).some(value => value !== null));
    for (const [key, value] of Object.entries(view.stats)) {
      const target = byId(`stat-${key}`); const next = formatNumber(value);
      if (target.textContent !== next) { target.textContent = next; target.classList.remove('studio-number'); if (studio?.motion()) { void target.offsetWidth; target.classList.add('studio-number'); } }
    }
    const input = view.stats.input_tokens;
    const output = view.stats.output_tokens;
    const total = input !== null && output !== null ? input + output : 0;
    byId('token-chart').hidden = total <= 0;
    if (total > 0) {
      text('token-total', formatNumber(total));
      byId('input-bar').style.width = `${input / total * 100}%`;
      byId('output-bar').style.width = `${output / total * 100}%`;
      byId('token-bars').setAttribute('aria-label', `输入 ${input} tokens，输出 ${output} tokens`);
    }
    renderFlow(run, view);
    renderSources(run, view);
    renderReports(view); renderProviders(view); renderEvents(run); agentProcess?.render(run, { source: state.streamSource, connection: state.streamConnection, mode: state.processMode }); updateControls(); studio?.sync();
  }

  async function startEventStream(run) {
    if (!isEmailMode() || !run?.id || run.request?.mode !== 'analysis' || !isActive(run)
        || state.disposed || state.suspended || doc.hidden || state.streamEnded || state.streamController) return;
    clearStreamReconnect();
    const generation = ++state.streamGeneration;
    const sessionGeneration = state.sessionGeneration;
    const controller = new AbortController();
    state.streamController = controller;
    state.streamConnection = state.streamRetryCount > 0 ? 'reconnecting' : 'connecting';
    const validSelection = () => !controller.signal.aborted && generation === state.streamGeneration
      && sessionGeneration === state.sessionGeneration && state.selected?.id === run.id && !state.disposed;
    let reader;
    const cancelReader = () => { void reader?.cancel().catch(() => {}); };
    controller.signal.addEventListener('abort', cancelReader, { once: true });
    renderWorkspace();
    try {
      const response = await fetchImpl(`/api/runs/${run.id}/events`, {
        method: 'GET', headers: { Accept: 'text/event-stream' }, credentials: 'same-origin', cache: 'no-store', redirect: 'error', signal: controller.signal,
      });
      if (!validSelection()) { void response.body?.cancel?.().catch(() => {}); return; }
      if ([401, 403, 404].includes(response.status)) {
        state.streamEnded = true;
        state.streamConnection = 'unavailable';
        if (response.status === 401) errorText({ status: 401, message: '会话已失效，请重新登录。' });
        renderWorkspace();
        return;
      }
      if (!response.ok || !response.body?.getReader) throw new Error('stream unavailable');
      reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = '';
      while (!controller.signal.aborted) {
        const { value, done } = await reader.read();
        if (!validSelection()) return;
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        if (buffer.length > 16 * 1024 * 1024) throw new Error('stream limit');
        buffer = buffer.replace(/\r\n/g, '\n');
        let boundary = buffer.indexOf('\n\n');
        while (boundary >= 0) {
          const raw = buffer.slice(0, boundary);
          buffer = buffer.slice(boundary + 2);
          let eventName = '';
          let data = '';
          for (const line of raw.split(/\r?\n/)) {
            if (line.startsWith('event:')) eventName = line.slice(6).trim();
            else if (line.startsWith('data:')) data += line.slice(5).trim();
          }
          if (eventName === 'complete') { state.streamEnded = true; state.streamConnection = 'complete'; renderWorkspace(); return; }
          if (eventName === 'progress' && data) {
            let parsed = null;
            try { parsed = JSON.parse(data); } catch {}
            const valid = parsed && parsed.run_id === run.id && Array.isArray(parsed.events) && parsed.events.length <= 1000
              && ['direct', 'logs', 'mixed'].includes(parsed.source) && parsed.events.every(event => event
                && ['phase', 'report', 'usage', 'connectivity', 'result', 'error'].includes(event.kind)
                && typeof event.timestamp === 'string' && event.timestamp.length <= 64 && Number.isFinite(Date.parse(event.timestamp))
                && event.payload && typeof event.payload === 'object' && !Array.isArray(event.payload));
            if (valid && validSelection()) {
              const merged = mergeAgentProcessEvents(state.streamedEvents, parsed.events);
              if (merged.length > 1000 || JSON.stringify(merged).length > 16 * 1024 * 1024) throw new Error('stream limit');
              state.streamedEvents = merged;
              state.streamSource = parsed.source;
              state.streamConnection = 'live';
              state.streamRetryCount = 0;
              renderWorkspace();
            }
          }
          boundary = buffer.indexOf('\n\n');
        }
      }
      if (!controller.signal.aborted && generation === state.streamGeneration) throw new Error('stream ended');
    } catch {
      if (!validSelection() || state.suspended || doc.hidden || !isActive(state.selected) || state.streamEnded) return;
      state.streamConnection = 'reconnecting';
      state.streamRetryCount = Math.min(5, state.streamRetryCount + 1);
      clearStreamReconnect();
      state.streamReconnectTimer = clock.setTimeout(() => {
        state.streamReconnectTimer = null;
        if (validSelection()) void startEventStream(state.selected);
      }, Math.min(5000, 500 * 2 ** (state.streamRetryCount - 1)));
      renderWorkspace();
    } finally {
      controller.signal.removeEventListener('abort', cancelReader);
      if (reader) {
        try { await reader.cancel(); } catch {}
        reader.releaseLock();
      }
      if (generation === state.streamGeneration) state.streamController = null;
    }
  }

  function schedulePolls() {
    if (state.disposed || state.suspended || doc.hidden) { clearPolls(); return; }
    const delays = pollDelays(state.selected?.status);
    if (delays.run === null || state.stopping) {
      if (state.runTimer !== null) clock.clearTimeout(state.runTimer);
      state.runTimer = null;
    } else if (state.runTimer === null && !state.runFlight) {
      state.runTimer = clock.setTimeout(() => { state.runTimer = null; return loadSelected(); }, delays.run);
    }
    if (state.historyTimer === null && !state.historyFlight) {
      state.historyTimer = clock.setTimeout(() => { state.historyTimer = null; return loadHistory(); }, delays.history);
    }
    text('sync-state', translate(isPublicMode() ? 'Entra 会话 · 自动同步' : isEmailMode() ? '邮箱会话 · 自动同步' : '本地会话 · 自动同步'));
  }

  function clearPolls() {
    if (state.runTimer !== null) clock.clearTimeout(state.runTimer);
    if (state.historyTimer !== null) clock.clearTimeout(state.historyTimer);
    state.runTimer = null; state.historyTimer = null;
  }

  async function loadSelected() {
    if (!state.selected || state.disposed) return;
    if (state.runFlight) return state.runFlight;
    const id = state.selected.id;
    const version = state.selectionVersion;
    const generation = state.sessionGeneration;
    const controller = new AbortController(); state.selectionController = controller;
    const pending = (async () => {
      try {
        const run = await api(`/api/runs/${id}`, { signal: AbortSignal.any([lifecycle.signal, controller.signal]) });
        if (state.disposed || version !== state.selectionVersion || generation !== state.sessionGeneration) return;
        if (run.id !== id) throw new Error('Mismatched run');
        state.selected = run; state.runError = ''; upsert(run);
        if (isEmailMode() && emailAccount && modelSettings?.budget) emailAccount.setBudget(modelSettings.budget);
        text('run-updated', `${translate('更新')} ${formatTime(new Date())}`);
        if (run.request?.mode !== 'analysis' || !isActive(run)) stopEventStream({ keepEvents: true });
        else if (!state.streamController && !state.streamReconnectTimer) void startEventStream(run);
        renderWorkspace(); renderHistory();
      } catch (error) {
        if (state.disposed || version !== state.selectionVersion || generation !== state.sessionGeneration || controller.signal.aborted) return;
        state.runError = errorText(error); renderWorkspace();
      } finally {
        if (version === state.selectionVersion && generation === state.sessionGeneration) { state.runFlight = null; schedulePolls(); }
      }
    })();
    state.runFlight = pending;
    return pending;
  }

  function selectRun(run, refresh = true) {
    if (!state.permissions.research) return Promise.resolve();
    if (state.managementOpen) management?.close();
    if (state.marketOpen) market?.close();
    if (state.assistantOpen) researchAssistant?.close();
    const previousId = state.selected?.id;
    state.selectionController?.abort();
    state.selectionVersion += 1; state.runFlight = null;
    state.selected = run; state.section = 'all'; state.runError = ''; state.actionError = '';
    if (run?.id !== previousId) {
      stopEventStream();
      state.streamedEvents = [];
      state.processMode = 'live';
    }
    byId('event-kind').value = 'all'; byId('event-query').value = '';
    state.requestSignature = ''; state.reportSignature = '';
    drawerOpenedFromHome = false;
    setHome(false);
    text('run-updated', '--');
    renderWorkspace(); renderHistory();
    if (run.request?.mode === 'connectivity') switchTab('data-view', 'connectivity');
    else switchTab('data-view', 'reports');
    if (isEmailMode() && run.request?.mode === 'analysis') void startEventStream(run);
    schedulePolls();
    return refresh ? loadSelected() : Promise.resolve();
  }

  async function loadHistory() {
    if (state.historyFlight) return state.historyFlight;
    const generation = state.sessionGeneration;
    const configuredAtStart = state.configReady;
    const pending = (async () => {
      try {
        const data = await api('/api/runs', { signal: lifecycle.signal });
        if (state.disposed || generation !== state.sessionGeneration) return;
        if (!Array.isArray(data.runs)) throw new Error('Invalid history');
        state.runs = data.runs.filter(run => run?.id && run?.request).sort((first, second) => String(second.created_at).localeCompare(String(first.created_at)));
        state.historyError = '';
        text('history-updated', formatTime(new Date()));
        if (state.selected && !isActive(state.selected)) await loadSelected();
      } catch (error) {
        if (state.disposed || generation !== state.sessionGeneration) return;
        if (!configuredAtStart && error.status === 401) historyNeedsRetry = true;
        else state.historyError = errorText(error);
      } finally {
        if (generation !== state.sessionGeneration || state.historyFlight !== pending) return;
        state.historyLoading = false; state.historyFlight = null;
        if (!state.disposed) { renderHistory(); schedulePolls(); }
      }
    })();
    state.historyFlight = pending;
    return pending;
  }

  async function submit(mode, historicalRequest) {
    if (!state.permissions.research) return;
    if (state.busy || byId('start-run').disabled) return;
    let request;
    try {
      request = historicalRequest ? buildRequest(historicalRequest, state.config) : draftRequest(mode);
    } catch (error) {
      message('form-error', error.message);
      const field = byId(error.field) || byId(`vendor-${error.field}`) || doc.querySelector('[name="analysts"]');
      field?.setAttribute('aria-invalid', 'true'); field?.focus(); return;
    }
    state.busy = true; state.actionError = ''; message('form-error', ''); updateControls();
    try {
      let payload = request;
      if (isPublicMode()) {
        const signature = JSON.stringify([account.profile.id, request]);
        if (state.submission?.signature !== signature) state.submission = { signature, id: win.crypto.randomUUID() };
        payload = { profile_id: account.profile.id, submission_id: state.submission.id, request };
      } else if (isEmailMode()) {
        const signature = JSON.stringify(request);
        if (state.submission?.signature !== signature) state.submission = { signature, id: win.crypto.randomUUID() };
        payload = { submission_id: state.submission.id, request };
      }
      const run = await api('/api/runs', { method: 'POST', body: payload, signal: lifecycle.signal });
      if (state.disposed) return;
      if (!run?.id || !run.request) throw new Error('Invalid run response');
      state.submission = null;
      upsert(run); await selectRun(run, false);
      closeResearchDrawer();
      if (win.innerWidth <= 850) setSidebar(false);
    } catch (error) {
      if (state.disposed) return;
      state.actionError = errorText(error); message('form-error', state.actionError);
      await loadHistory();
    } finally {
      state.busy = false;
      if (!state.disposed) { renderWorkspace(); updateControls(); }
    }
  }

  async function resume() {
    if (byId('resume-run').disabled) return;
    const id = state.selected.id;
    const generation = state.sessionGeneration;
    state.busy = true; state.actionError = ''; updateControls();
    state.selectionController?.abort(); state.selectionVersion += 1; state.runFlight = null;
    try {
      const run = await api(`/api/runs/${id}/resume`, { method: 'POST', body: {}, signal: lifecycle.signal });
      if (state.disposed || generation !== state.sessionGeneration) return;
      if (run.id !== id) throw new Error('Mismatched run');
      upsert(run);
      if (state.selected?.id === id) state.selected = run;
    } catch (error) {
      if (!state.disposed && generation === state.sessionGeneration) state.actionError = errorText(error);
    } finally {
      if (generation === state.sessionGeneration) {
        state.busy = false;
        if (!state.disposed) { renderWorkspace(); renderHistory(); schedulePolls(); }
      }
    }
  }

  async function stop() {
    if (byId('stop-run').disabled) return;
    const id = state.selected.id;
    state.stopping = true; state.actionError = ''; updateControls();
    schedulePolls();
    state.selectionController?.abort(); state.selectionVersion += 1; state.runFlight = null;
    try {
      const run = await api(`/api/runs/${id}/stop`, { method: 'POST', body: {}, signal: lifecycle.signal });
      if (state.disposed) return;
      upsert(run);
      if (state.selected?.id === id) state.selected = run;
    } catch (error) {
      if (!state.disposed) state.actionError = errorText(error);
    } finally {
      state.stopping = false;
      if (!state.disposed) { renderWorkspace(); renderHistory(); schedulePolls(); }
    }
  }

  async function reconcile(event) {
    event.preventDefault();
    if (isEmailMode() || !state.config?.csrf_token || state.reconciling || !state.selected?.start_uncertain) return;
    const id = state.selected.id;
    state.reconciling = true; state.actionError = ''; updateControls();
    state.selectionController?.abort(); state.selectionVersion += 1; state.runFlight = null;
    try {
      let body = {};
      if (isPublicMode()) {
        const execution = byId('reconcile-execution').value.trim();
        if (!execution) { byId('reconcile-execution').focus(); state.reconciling = false; updateControls(); return; }
        body = { execution_name: execution };
      }
      const run = await api(`/api/runs/${id}/reconcile`, { method: 'POST', body, signal: lifecycle.signal });
      if (state.disposed) return;
      if (run.id !== id) throw new Error('Mismatched run');
      upsert(run);
      if (state.selected?.id === id) state.selected = run;
      byId('reconcile-execution').value = '';
    } catch (error) { if (!state.disposed) state.actionError = errorText(error); }
    finally {
      state.reconciling = false;
      if (!state.disposed) { renderWorkspace(); renderHistory(); schedulePolls(); }
    }
  }

  async function download(format) {
    if (!state.selected || state.downloading) return;
    const id = state.selected.id;
    state.downloading = true; state.actionError = ''; updateControls();
    try {
      const response = await fetchImpl(downloadUrl(id, format), {
        method: 'GET', credentials: 'same-origin', cache: 'no-store', redirect: 'error',
        signal: AbortSignal.any([lifecycle.signal, AbortSignal.timeout(30000)]),
      });
      if (!response.ok) { const error = new Error(`下载失败 (HTTP ${response.status})。`); error.status = response.status; throw error; }
      const blob = await response.blob();
      if (state.disposed) return;
      const url = win.URL.createObjectURL(blob); objectUrls.add(url);
      const anchor = element('a'); anchor.href = url; anchor.download = `${id}.${format === 'json' ? 'json' : 'md'}`;
      doc.body.append(anchor); anchor.click(); anchor.remove();
      const timer = clock.setTimeout(() => { win.URL.revokeObjectURL(url); objectUrls.delete(url); downloadTimers.delete(timer); }, 1000);
      downloadTimers.add(timer);
    } catch (error) {
      if (!state.disposed) state.actionError = errorText(error);
    } finally {
      state.downloading = false;
      if (!state.disposed) renderWorkspace();
    }
  }

  async function refresh() {
    if (state.refreshing || state.disposed) return;
    state.refreshing = true; updateControls();
    try { await Promise.all([loadConfig(), loadHistory(), loadSelected()]); }
    finally { state.refreshing = false; if (!state.disposed) { updateControls(); schedulePolls(); } }
  }

  for (const attribute of ['data-view']) {
    const buttons = all(`[${attribute}]`);
    for (const button of buttons) {
      on(button, 'click', () => {
        switchTab(attribute, button.getAttribute(attribute));
      });
      on(button, 'keydown', event => {
        if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
        event.preventDefault();
        const index = event.key === 'Home' ? 0 : event.key === 'End' ? buttons.length - 1 : (buttons.indexOf(button) + (event.key === 'ArrowRight' ? 1 : -1) + buttons.length) % buttons.length;
        buttons[index].click(); buttons[index].focus();
      });
    }
  }
  on(byId('config-form'), 'submit', event => { event.preventDefault(); submit(); });
  on(byId('new-research'), 'click', openResearchDrawer);
  function openTasks() {
    if (!state.permissions.research) return;
    if (state.managementOpen) management?.close();
    if (state.marketOpen) market?.close();
    if (state.assistantOpen) researchAssistant.close();
    setHome(false, 'tasks');
    if (win.innerWidth <= 850) setSidebar(false);
  }
  on(byId('workspace-home-button'), 'click', () => {
    if (state.managementOpen) management?.close();
    if (state.marketOpen) market?.close();
    if (state.assistantOpen) researchAssistant.close();
    setHome(true);
    if (win.innerWidth <= 850) setSidebar(false);
  });
  on(byId('research-tasks-entry'), 'click', openTasks);
  on(byId('return-tasks'), 'click', openTasks);
  for (const button of all('[data-task-filter]')) on(button, 'click', () => {
    state.taskFilter = button.dataset.taskFilter;
    for (const filter of all('[data-task-filter]')) filter.setAttribute('aria-pressed', String(filter === button));
    renderHistory();
  });
  for (const button of all('[data-open-research]')) on(button, 'click', openResearchDrawer);
  on(byId('close-research-drawer'), 'click', closeResearchDrawer);
  on(researchDrawer, 'cancel', event => { event.preventDefault(); closeResearchDrawer(); });
  on(researchDrawer, 'click', event => {
    if (event.target === researchDrawer) closeResearchDrawer();
  });
  on(byId('event-kind'), 'change', () => renderEvents(state.selected));
  on(byId('event-query'), 'input', () => renderEvents(state.selected));
  on(byId('config-form'), 'input', () => { modelSettings?.syncMessage(); state.config && (state.config.settings_aligned = modelSettings?.aligned ?? state.config.settings_aligned); updateControls(); });
  on(byId('config-form'), 'change', () => { modelSettings?.syncMessage(); state.config && (state.config.settings_aligned = modelSettings?.aligned ?? state.config.settings_aligned); updateControls(); });
  on(byId('vendor-options'), 'change', () => { modelSettings?.syncMessage(); state.config && (state.config.settings_aligned = modelSettings?.aligned ?? state.config.settings_aligned); updateControls(); });
  on(byId('test-connectivity'), 'click', () => submit('connectivity'));
  on(byId('stop-run'), 'click', stop);
  on(byId('resume-run'), 'click', resume);
  on(byId('reconcile-form'), 'submit', reconcile);
  on(byId('rerun'), 'click', () => submit(undefined, state.selected?.request));
  on(byId('refresh'), 'click', refresh);
  on(byId('reset-draft'), 'click', () => populateForm(state.config.defaults));
  on(byId('load-request'), 'click', event => { populateForm(state.selected.request); openResearchDrawer(event.currentTarget); });
  on(byId('history-search'), 'input', event => { state.query = event.target.value; renderHistory(); });
  on(byId('sidebar-toggle'), 'click', () => setSidebar(byId('workbench').dataset.sidebarOpen !== 'true'));
  on(byId('sidebar-backdrop'), 'click', () => setSidebar(false));
  on(doc, 'keydown', event => { if (event.key === 'Escape' && win.innerWidth <= 850 && !doc.querySelector('dialog[open]')) { setSidebar(false); byId('sidebar-toggle').focus(); } });
  on(win, 'resize', () => setSidebar(byId('workbench').dataset.sidebarOpen === 'true'));
  for (const button of all('[data-download]')) on(button, 'click', () => download(button.dataset.download));
  for (const button of all('[data-step]')) on(button, 'click', () => {
    const input = byId(button.dataset.for);
    input.value = String(Math.min(3, Math.max(1, Math.round(Number(input.value) || 1) + Number(button.dataset.step))));
    updateControls();
  });
  on(doc, 'visibilitychange', () => {
    if (doc.hidden) { clearPolls(); stopEventStream({ keepEvents: true }); agentProcess?.stopPlayback(); text('sync-state', translate('页面隐藏 · 同步暂停')); }
    else if (!state.disposed) { loadHistory(); if (isActive(state.selected)) loadSelected(); void startEventStream(state.selected); schedulePolls(); }
  });
  on(win, 'pagehide', () => { state.suspended = true; clearPolls(); stopEventStream({ keepEvents: true }); agentProcess?.stopPlayback(); });
  on(win, 'pageshow', () => { if (state.suspended && !state.disposed) { state.suspended = false; refresh(); } });

  function dispose() {
    if (state.disposed) return;
    state.disposed = true; clearPolls(); lifecycle.abort(); state.selectionController?.abort();
    stopEventStream();
    account?.dispose();
    emailAccount?.dispose();
    modelSettings?.dispose();
    market?.dispose();
    management?.dispose();
    researchAssistant?.dispose();
    studio?.dispose();
    agentProcess?.dispose();
    unsubscribeLocale();
    listeners.forEach(remove => remove());
    downloadTimers.forEach(timer => clock.clearTimeout(timer));
    objectUrls.forEach(url => win.URL.revokeObjectURL(url));
    downloadTimers.clear(); objectUrls.clear();
  }

  studio = mountStudio(doc, {
    getRuns: () => state.runs,
    getSelected: () => state.selected,
    selectReport: async (id, section) => {
      const run = state.runs.find(item => item.id === id);
      if (!run || state.disposed) return;
      const generation = state.sessionGeneration;
      await selectRun(run);
      if (state.disposed || state.sessionGeneration !== generation || state.selected?.id !== id) return;
      if (section === 'all' || projectRun(state.selected).reports.some(report => report.section === section)) state.section = section;
      filterSections(); switchTab('data-view', 'reports');
      if (win.innerWidth <= 850) setSidebar(false);
    },
  });
  agentProcess = mountAgentProcess(doc, {
    renderMarkdown: safeMarkdown,
    paintIcons,
    clock,
    onModeChange: mode => {
      state.processMode = mode === 'paused' ? 'replay' : mode;
    },
  });
  researchAssistant = mountResearchAssistant(doc, {
    request: (...args) => api(...args),
    paintIcons,
    onOpen: () => setAssistantOpen(true),
    onClose: () => setAssistantOpen(false),
  });
  market = mountMarket(doc, {
    request: (...args) => api(...args), paintIcons, clock,
    onOpen: () => {
      if (state.managementOpen) management?.close();
      if (state.assistantOpen) researchAssistant.close();
      state.marketOpen = true;
      agentProcess?.stopPlayback();
      if (win.innerWidth <= 850) setSidebar(false);
    },
    onClose: () => { state.marketOpen = false; if (!state.disposed) setHome(false, 'tasks'); },
    onPrefill: quote => {
      if (!state.permissions.research) return;
      byId('ticker').value = quote.symbol;
      if (quote.asset_type === 'etf' || quote.quote_type === 'ETF') byId('asset_type').value = 'etf';
      else if (!byId('asset_type').value) byId('asset_type').value = 'stock';
      modelSettings?.syncMessage();
      if (state.config) state.config.settings_aligned = modelSettings?.aligned ?? state.config.settings_aligned;
      updateControls();
      openResearchDrawer(byId('new-research'));
    },
  });
  management = mountAdminWorkspace(doc, {
    request: (...args) => api(...args), paintIcons,
    onOpen: () => {
      if (state.marketOpen) market?.close();
      if (state.assistantOpen) researchAssistant?.close();
      state.managementOpen = true; agentProcess?.stopPlayback();
      if (win.innerWidth <= 850) setSidebar(false);
    },
    onClose: () => { state.managementOpen = false; if (!state.disposed) setHome(true); },
  });
  const unsubscribeLocale = onLocaleChange(() => {
    if (state.disposed) return;
    if (byId('service-state').dataset.sourceLabel) text('service-state', translate(byId('service-state').dataset.sourceLabel));
    state.reportSignature = ''; state.eventsSignature = ''; state.providerSignature = ''; state.requestSignature = '';
    renderHistory(); renderWorkspace();
    text('breadcrumb-studio', translate(state.managementOpen ? '管理' : state.marketOpen ? '行情' : state.assistantOpen ? '研究助手' : state.page === 'home' ? '研究工作台' : state.page === 'tasks' ? '研究任务' : '研究报告'));
  });
  paintIcons(doc);
  setHome(true);
  setSidebar(win.innerWidth > 850);
  renderWorkspace();
  const initialHistory = loadHistory();
  const ready = loadConfig().then(() => {
    if (state.disposed) return;
    void initialHistory.then(() => {
      if (!state.disposed && (isEmailMode() || isPublicMode() || state.historyError || historyNeedsRetry)) {
        historyNeedsRetry = false;
        void loadHistory();
      }
    });
    schedulePolls();
  });
  return { ready, dispose };
}