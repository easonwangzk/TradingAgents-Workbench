import { bindLocale, locale, onLocaleChange, translate } from './locale.mjs';

export const MARKET_RANGES = ['1m', '3m', '6m', '1y', '5y'];
export const MARKET_SOURCES = ['yfinance', 'alpha_vantage'];

const validMarketVersion = version => typeof version === 'string' && version.length > 0 && version.length <= 256;

export function marketSymbols(values) {
  if (!Array.isArray(values) || values.length > 12) throw new Error(translate('自选列表最多 12 个证券。'));
  const symbols = values.map(value => typeof value === 'string' ? value.trim().toUpperCase() : '');
  if (symbols.some(value => value.length > 24 || !/^\^?[A-Z0-9]+(?:[.=-][A-Z0-9]+)*$/.test(value)) || new Set(symbols).size !== symbols.length) {
    throw new Error(translate('请输入有效且不重复的证券代码。'));
  }
  return symbols;
}

export function createMarketService({ request, onChange = () => {}, clock = globalThis }) {
  const state = { account: null, open: false, visible: true, paused: false, symbols: [], version: null, loaded: false, selected: null, range: '1m', source: 'yfinance', snapshot: null, loading: false, saving: false, error: '' };
  let generation = 0;
  let controller = null;
  let timer = null;
  let disposed = false;
  const emit = () => { if (!disposed) onChange(state); };
  const active = () => !disposed && state.account && state.open && state.visible;
  const cancel = () => {
    generation += 1;
    controller?.abort(); controller = null;
    if (timer !== null) clock.clearTimeout(timer);
    timer = null;
    if (state.saving) state.loaded = false;
    state.loading = false; state.saving = false;
  };
  const schedule = () => {
    if (timer !== null) clock.clearTimeout(timer);
    timer = null;
    if (active() && !state.paused && state.symbols.length && !state.loading && !state.saving) {
      timer = clock.setTimeout(async () => { timer = null; await refresh(); }, 60000);
    }
  };
  const failure = error => {
    if (error.status === 401) {
      cancel(); Object.assign(state, { account: null, open: false, symbols: [], version: null, loaded: false, selected: null, snapshot: null, error: translate('登录已失效，请重新登录。') }); emit();
      return;
    }
    state.error = error.status === 409 || error.status === 412 ? translate('自选列表已在其他窗口更新，请重新打开行情后再编辑。')
      : error.status === 401 ? translate('登录已失效，请重新登录。')
        : error.status === 429 ? translate('行情请求达到限制，自动刷新已暂停。')
          : error.status === 403 ? translate('当前会话无权读取行情，请重新登录。')
            : error.message || translate('行情暂时不可用。');
    if ([401, 403, 429].includes(error.status)) state.paused = true;
  };
  async function perform(path, options, apply) {
    const ticket = ++generation;
    controller?.abort();
    controller = new AbortController();
    const current = () => active() && ticket === generation;
    state.loading = true; state.error = ''; emit();
    try {
      const result = await request(path, { ...options, signal: controller.signal });
      if (!current()) return false;
      apply(result);
      return true;
    } catch (error) {
      if (current()) failure(error);
      return false;
    } finally {
      if (current()) { state.loading = false; state.saving = false; controller = null; emit(); schedule(); }
    }
  }
  async function refresh() {
    if (!active() || !state.symbols.length || state.loading || state.saving) return;
    await perform('/api/market/snapshot', { method: 'POST', body: { symbols: [...state.symbols], selected: state.selected, range: state.range, source: state.source } }, result => {
      if (result.selected !== state.selected || result.source !== state.source || result.interval !== '1d' || !Array.isArray(result.quotes) || !Array.isArray(result.candles) || !Array.isArray(result.errors)) throw new Error(translate('行情响应无效。'));
      state.snapshot = result;
    });
  }
  async function open() {
    if (!state.account || disposed) return;
    cancel(); state.open = true; state.loaded = false; state.snapshot = null;
    if (!state.visible) { emit(); return; }
    const loaded = await perform('/api/market/watchlist', {}, result => {
      const symbols = marketSymbols(result.symbols);
      if (result.version !== null && !validMarketVersion(result.version)) throw new Error(translate('行情响应无效。'));
      state.symbols = symbols; state.version = result.version; state.loaded = true;
      if (!state.symbols.includes(state.selected)) state.selected = state.symbols[0] || null;
    });
    if (loaded) await refresh();
  }
  async function save(values) {
    const symbols = marketSymbols(values);
    if (!active() || state.loading || state.saving || !state.loaded || (state.version !== null && !validMarketVersion(state.version))) return;
    state.saving = true;
    const saved = await perform('/api/market/watchlist', { method: 'PUT', body: { symbols }, ...(state.version === null ? {} : { revision: state.version }) }, result => {
      const savedSymbols = marketSymbols(result.symbols);
      if (!validMarketVersion(result.version)) throw new Error(translate('行情响应无效。'));
      state.symbols = savedSymbols; state.version = result.version; state.loaded = true;
      if (!state.symbols.includes(state.selected)) state.selected = state.symbols[0] || null;
      state.snapshot = null;
    });
    if (saved) await refresh();
  }
  async function select(symbol) {
    if (!active() || !state.symbols.includes(symbol) || state.saving) return;
    cancel(); state.selected = symbol; state.snapshot = null; emit(); await refresh();
  }
  async function configure({ range = state.range, source = state.source }) {
    if (!MARKET_RANGES.includes(range) || !MARKET_SOURCES.includes(source)) throw new Error(translate('行情参数无效。'));
    if (state.saving) return;
    cancel(); state.range = range; state.source = source; state.snapshot = null; emit(); await refresh();
  }
  return {
    state, open, save, refresh, select, configure,
    close() { cancel(); state.open = false; state.error = ''; emit(); },
    setSession(account) {
      if (state.account === account) return;
      cancel(); Object.assign(state, { account, open: false, paused: false, symbols: [], version: null, loaded: false, selected: null, snapshot: null, range: '1m', source: 'yfinance', error: '' }); emit();
    },
    setVisible(visible) {
      if (state.visible === visible) return;
      cancel(); state.visible = visible; emit();
      if (active()) {
        if (!state.loaded) void open();
        else if (!state.paused) {
          if (!state.snapshot) void refresh();
          else schedule();
        }
      }
    },
    setPaused(paused) { state.paused = paused; emit(); schedule(); },
    dispose() { cancel(); disposed = true; state.account = null; state.symbols = []; state.version = null; state.loaded = false; state.snapshot = null; },
  };
}

export function mountMarket(doc, { request, paintIcons = () => {}, onOpen = () => {}, onClose = () => {}, onPrefill = () => {}, clock = doc.defaultView, loadChart = () => import('./market-chart.mjs') }) {
  const win = doc.defaultView;
  const byId = id => doc.getElementById(id);
  const screen = byId('market-screen');
  const entry = byId('market-entry');
  const listeners = [];
  let chart = null, chartTicket = 0, actionTicket = 0, chartLoading = false, disposed = false;
  let mode = 'line', sort = 'symbol', direction = 1, query = '', localError = '';
  const on = (target, event, callback) => { target.addEventListener(event, callback); listeners.push(() => target.removeEventListener(event, callback)); };
  const node = (tag, text, className) => { const item = doc.createElement(tag); if (text !== undefined) item.textContent = text; if (className) item.className = className; return item; };
  const iconButton = (name, label) => {
    const button = node('button', undefined, 'icon-button'); button.type = 'button'; button.title = label; button.setAttribute('aria-label', label);
    const icon = node('i'); icon.dataset.lucide = name; button.append(icon); return button;
  };
  const number = value => typeof value === 'number' && Number.isFinite(value) ? value.toLocaleString(locale(), { maximumFractionDigits: 2 }) : '--';
  const time = value => value && Number.isFinite(new Date(value).getTime()) ? new Date(value).toLocaleString(locale()) : '--';
  const selectedQuote = () => service.state.snapshot?.quotes.find(item => item.symbol === service.state.selected) || { symbol: service.state.selected };
  const retireChart = () => { chartTicket += 1; chartLoading = false; chart?.dispose(); chart = null; };
  const canChart = () => !disposed && service.state.open && service.state.visible && !screen.hidden && Boolean(service.state.snapshot?.candles.length);
  async function renderChart() {
    if (!canChart()) { chart?.update(null, mode); return; }
    if (chart) { chart.update(service.state.snapshot, mode); return; }
    if (chartLoading) return;
    const ticket = ++chartTicket;
    chartLoading = true;
    try {
      const module = await loadChart();
      if (ticket !== chartTicket || !canChart()) return;
      chart = module.mountMarketChart(byId('market-chart'));
      chart.update(service.state.snapshot, mode);
    } catch {
      if (ticket === chartTicket && canChart()) { byId('market-chart-empty').hidden = false; byId('market-chart-empty').textContent = translate('图表暂时不可用。'); }
    } finally { if (ticket === chartTicket) chartLoading = false; }
  }
  function render(state) {
    if (disposed) return;
    if (!state.account) {
      const wasOpen = !screen.hidden;
      screen.hidden = true; entry.hidden = true; entry.removeAttribute('aria-current'); retireChart();
      if (wasOpen) onClose();
    }
    byId('market-count').textContent = `${state.symbols.length} / 12`;
    byId('market-status').textContent = localError || state.error || (state.saving ? translate('正在保存自选列表…') : state.loading ? translate('正在读取行情…') : state.snapshot?.errors.map(item => `${item.symbol}: ${item.message}`).join(' | ') || '');
    byId('market-status').dataset.error = String(Boolean(localError || state.error || state.snapshot?.errors.length));
    screen.setAttribute('aria-busy', String(state.loading));
    byId('market-refresh').disabled = !state.account || state.loading || state.saving || !state.symbols.length;
    byId('market-add').disabled = !state.loaded || state.loading || state.saving || state.symbols.length >= 12;
    byId('market-symbol').disabled = state.saving || state.symbols.length >= 12;
    byId('market-source').disabled = state.saving;
    byId('market-source').value = state.source;
    byId('market-auto').checked = !state.paused;
    const quotes = new Map((state.snapshot?.quotes || []).map(item => [item.symbol, item]));
    const visible = state.symbols.filter(symbol => `${symbol} ${quotes.get(symbol)?.name || ''}`.toLocaleLowerCase().includes(query.toLocaleLowerCase())).sort((first, second) => {
      if (sort === 'symbol') return direction * first.localeCompare(second);
      const firstValue = quotes.get(first)?.[sort], secondValue = quotes.get(second)?.[sort];
      if (!Number.isFinite(firstValue)) return Number.isFinite(secondValue) ? 1 : first.localeCompare(second);
      if (!Number.isFinite(secondValue)) return -1;
      return direction * (firstValue - secondValue) || first.localeCompare(second);
    });
    const rows = byId('market-rows'); rows.replaceChildren();
    for (const symbol of visible) {
      const quote = quotes.get(symbol);
      const row = node('tr'); row.dataset.symbol = symbol; row.dataset.selected = String(symbol === state.selected);
      const security = node('td'); const select = node('button', undefined, 'market-select'); select.type = 'button'; select.dataset.marketSelect = symbol;
      select.setAttribute('aria-current', String(symbol === state.selected));
      select.append(node('strong', symbol), node('small', quote?.name || '--')); select.title = quote?.name || symbol; security.append(select);
      const price = node('td', number(quote?.price), 'market-number');
      const change = node('td', `${number(quote?.change_percent)}${Number.isFinite(quote?.change_percent) ? '%' : ''}`, 'market-number');
      change.dataset.direction = quote?.change_percent > 0 ? 'up' : quote?.change_percent < 0 ? 'down' : 'flat';
      const action = node('td'); const remove = iconButton('trash-2', `${translate('删除证券')} ${symbol}`); remove.dataset.marketDelete = symbol; remove.disabled = state.loading || state.saving; action.append(remove);
      const issue = state.snapshot?.errors.find(item => item.symbol === symbol); if (issue) { price.title = issue.message; price.dataset.error = 'true'; }
      row.append(security, price, change, action); rows.append(row);
    }
    byId('market-empty').hidden = visible.length > 0;
    byId('market-empty').textContent = translate(state.symbols.length ? '暂无匹配的证券' : state.loading ? '正在读取行情…' : '自选列表为空');
    for (const button of screen.querySelectorAll('[data-market-sort]')) button.closest('th').setAttribute('aria-sort', button.dataset.marketSort === sort ? direction === 1 ? 'ascending' : 'descending' : 'none');
    const quote = selectedQuote();
    byId('market-selected').textContent = state.selected || '--'; byId('market-name').textContent = quote.name || '--';
    for (const field of screen.querySelectorAll('[data-market-field]')) {
      const key = field.dataset.marketField;
      field.textContent = ['currency', 'exchange'].includes(key) ? quote[key] || '--' : `${number(quote[key])}${key === 'change_percent' && Number.isFinite(quote[key]) ? '%' : ''}`;
      if (key.startsWith('change')) field.dataset.direction = quote[key] > 0 ? 'up' : quote[key] < 0 ? 'down' : 'flat';
    }
    byId('market-research').disabled = !state.selected;
    for (const button of screen.querySelectorAll('[data-market-range]')) { button.setAttribute('aria-pressed', String(button.dataset.marketRange === state.range)); button.disabled = state.saving; }
    for (const button of screen.querySelectorAll('[data-market-mode]')) button.setAttribute('aria-pressed', String(button.dataset.marketMode === mode));
    byId('market-chart-empty').hidden = Boolean(state.snapshot?.candles.length);
    byId('market-chart-empty').textContent = translate(!state.selected ? '选择证券查看日线' : state.loading ? '正在读取行情…' : '暂无日线数据');
    byId('market-provenance').textContent = state.snapshot ? `${state.snapshot.source === 'yfinance' ? 'Yahoo Finance' : 'Alpha Vantage'} · ${translate('日线 · 非实时')} · ${translate('读取时间')} ${time(state.snapshot.fetched_at)}` : '--';
    byId('market-as-of').textContent = `${translate('行情时间')} ${time(quote.as_of)}`;
    paintIcons(rows);
    void renderChart();
  }
  const service = createMarketService({ request, clock, onChange: render });
  service.setVisible(!doc.hidden);
  function close() {
    actionTicket += 1;
    const wasOpen = service.state.open;
    screen.hidden = true; entry.removeAttribute('aria-current'); retireChart(); service.close();
    if (wasOpen) onClose();
  }
  async function open() {
    if (!service.state.account || disposed) return;
    onOpen();
    screen.hidden = false; entry.setAttribute('aria-current', 'page');
    byId('research-tasks-entry').removeAttribute('aria-current');
    for (const id of ['workspace-home', 'workspace-tasks', 'workspace-run', 'research-assistant-screen']) byId(id).hidden = true;
    doc.documentElement.dataset.home = 'false'; doc.documentElement.dataset.screen = 'market'; byId('workbench').dataset.screen = 'market';
    byId('breadcrumb-studio').textContent = translate('行情');
    screen.dataset.marketView = 'list';
    await service.open();
  }
  const act = async callback => {
    const ticket = actionTicket;
    localError = '';
    try { await callback(() => ticket === actionTicket && !disposed && service.state.open); }
    catch (error) { if (ticket === actionTicket && service.state.open) localError = error.message; }
    if (ticket === actionTicket && service.state.open && !disposed) render(service.state);
  };
  on(entry, 'click', () => { void open(); });
  on(byId('market-add-form'), 'submit', event => { event.preventDefault(); void act(async current => { await service.save([...service.state.symbols, byId('market-symbol').value]); if (current() && !service.state.error) byId('market-symbol').value = ''; }); });
  on(byId('market-search'), 'input', event => { query = event.target.value.trim(); render(service.state); });
  on(byId('market-refresh'), 'click', () => { void act(() => service.state.loaded ? service.refresh() : service.open()); });
  on(byId('market-auto'), 'change', event => service.setPaused(!event.target.checked));
  on(byId('market-source'), 'change', event => { void act(() => service.configure({ source: event.target.value })); });
  on(screen, 'click', event => {
    const button = event.target.closest('button'); if (!button || button.disabled) return;
    if (button.dataset.marketSelect) { screen.dataset.marketView = 'detail'; void act(() => service.select(button.dataset.marketSelect)); }
    if (button.dataset.marketDelete) void act(() => service.save(service.state.symbols.filter(symbol => symbol !== button.dataset.marketDelete)));
    if (button.dataset.marketSort) { direction = sort === button.dataset.marketSort ? -direction : 1; sort = button.dataset.marketSort; render(service.state); }
    if (button.dataset.marketRange) void act(() => service.configure({ range: button.dataset.marketRange }));
    if (button.dataset.marketMode) { mode = button.dataset.marketMode; render(service.state); }
  });
  on(byId('market-back'), 'click', () => { screen.dataset.marketView = 'list'; byId('market-search').focus(); });
  on(byId('market-research'), 'click', () => { if (service.state.selected) onPrefill(selectedQuote()); });
  on(doc, 'visibilitychange', () => service.setVisible(!doc.hidden));
  on(win, 'pagehide', () => { retireChart(); service.setVisible(false); });
  on(win, 'pageshow', () => service.setVisible(!doc.hidden));
  on(win, 'resize', () => chart?.resize());
  bindLocale(doc, '#market-screen, #market-entry');
  const unsubscribe = onLocaleChange(() => { render(service.state); if (service.state.open) byId('breadcrumb-studio').textContent = translate('行情'); });
  render(service.state);
  return {
    open, close, state: service.state,
    setSession(account) {
      if (service.state.account !== account) { close(); mode = 'line'; sort = 'symbol'; direction = 1; query = ''; localError = ''; byId('market-symbol').value = ''; byId('market-search').value = ''; screen.dataset.marketView = 'list'; }
      service.setSession(account); entry.hidden = !account;
    },
    dispose() { disposed = true; close(); service.dispose(); unsubscribe(); listeners.forEach(remove => remove()); },
  };
}