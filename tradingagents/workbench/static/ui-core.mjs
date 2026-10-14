export const STATUS_LABELS = Object.freeze({
  preparing: '正在准备', prepared: '已准备', reconciliation_required: '待核对',
  starting: '正在启动', running: '运行中', collecting: '报告收集中',
  succeeded: '已完成', failed: '失败', stopped: '已停止',
});
export const ANALYST_LABELS = Object.freeze({ market: '市场', social: '社交', news: '新闻', fundamentals: '基本面' });
export const VENDOR_LABELS = Object.freeze({
  core_stock_apis: '行情数据', technical_indicators: '技术指标', fundamental_data: '基本面数据',
  news_data: '新闻数据', macro_data: '宏观数据', prediction_markets: '预测市场',
});
export const PROVIDER_LABELS = Object.freeze({ yfinance: 'Yahoo Finance', alpha_vantage: 'Alpha Vantage', fred: 'FRED', polymarket: 'Polymarket', foundry: 'Foundry' });
export const SECTION_LABELS = Object.freeze({
  market_report: '市场分析', sentiment_report: '社交情绪', social_report: '社交情绪', news_report: '新闻分析',
  fundamentals_report: '基本面分析', investment_debate_state: '投资辩论', investment_plan: '投资计划',
  trader_investment_plan: '交易员方案', risk_debate_state: '风险辩论', final_trade_decision: '最终决策',
  bull_history: '多方观点', bear_history: '空方观点', judge_decision: '研究裁决',
  'investment_debate_state.bull_history': '多方观点',
  'investment_debate_state.bear_history': '空方观点',
  'investment_debate_state.judge_decision': '研究裁决',
  'risk_debate_state.aggressive_history': '激进风险观点',
  'risk_debate_state.conservative_history': '保守风险观点',
  'risk_debate_state.neutral_history': '中性风险观点',
  'risk_debate_state.judge_decision': '风险裁决',
});
const ACTIVE = new Set(['starting', 'running', 'collecting']);
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
const STATS = ['llm_calls', 'tool_calls', 'input_tokens', 'output_tokens'];
const isObject = value => value !== null && typeof value === 'object' && !Array.isArray(value);

export function localDate(now = new Date()) {
  return now.toISOString().slice(0, 10);
}

function invalid(field, message) {
  const error = new Error(message);
  error.field = field;
  throw error;
}

export function buildRequest(values, config, today = localDate()) {
  const source = { ...config.defaults, ...values };
  const ticker = typeof source.ticker === 'string' ? source.ticker.trim().toUpperCase() : '';
  if (ticker.length > 24 || !/^\^?[A-Z0-9]+(?:[.=-][A-Z0-9]+)*$/.test(ticker)) invalid('ticker', '请输入有效证券代码，最多 24 个字符。');
  const date = source.analysis_date;
  const parsed = typeof date === 'string' ? new Date(`${date}T00:00:00Z`) : new Date(NaN);
  if (!/^\d{4}-\d{2}-\d{2}$/.test(date) || !Number.isFinite(parsed.getTime()) || parsed.toISOString().slice(0, 10) !== date || date > today) {
    invalid('analysis_date', '请选择有效日期，且不晚于今天。');
  }
  for (const [field, allowed] of Object.entries({ mode: ['analysis', 'connectivity'], asset_type: ['stock', 'etf'], language: ['Chinese', 'English'] })) {
    if (!allowed.includes(source[field])) invalid(field, `${field} 不受当前契约支持。`);
  }
  for (const field of ['debate_rounds', 'risk_rounds']) {
    if (!Number.isInteger(source[field]) || source[field] < 1 || source[field] > 3) invalid(field, '轮数必须为 1 到 3 的整数。');
  }
  const analysts = source.analysts;
  const allowedAnalysts = Object.keys(ANALYST_LABELS);
  if (!Array.isArray(analysts) || !analysts.length || analysts.some(value => !allowedAnalysts.includes(value)) || new Set(analysts).size !== analysts.length) {
    invalid('analysts', '至少选择一位分析师，不可重复。');
  }
  if (values.data_vendors !== undefined && !isObject(values.data_vendors)) invalid('data_vendors', '数据源配置无效。');
  const vendors = { ...config.defaults.data_vendors, ...values.data_vendors };
  if (Object.keys(vendors).some(category => !Object.hasOwn(VENDOR_LABELS, category))) invalid('data_vendors', '包含不受支持的数据源类别。');
  const data_vendors = {};
  for (const category of Object.keys(VENDOR_LABELS)) {
    const chain = vendors[category];
    const selected = typeof chain === 'string' ? chain.split(',').map(value => value.trim()) : [];
    const allowed = config.vendor_choices[category] || [];
    if (!selected.length || selected.some(value => !allowed.includes(value)) || new Set(selected).size !== selected.length) {
      invalid(category, `${VENDOR_LABELS[category]}的数据源链无效。`);
    }
    data_vendors[category] = selected.join(',');
  }
  if (source.asset_type === 'etf' && data_vendors.fundamental_data !== 'yfinance') {
    invalid('fundamental_data', 'ETF 基金资料使用 yfinance。');
  }
  return {
    mode: source.mode, ticker, analysis_date: date, asset_type: source.asset_type,
    analysts: allowedAnalysts.filter(value => analysts.includes(value)), debate_rounds: source.debate_rounds,
    risk_rounds: source.risk_rounds, language: source.language, data_vendors,
  };
}

export function vendorChains(options) {
  const singles = [...new Set(options.filter(value => typeof value === 'string' && value && !value.includes(',')))];
  return [...singles, ...singles.flatMap(first => singles.filter(second => second !== first).map(second => `${first},${second}`))];
}

export function projectRun(record) {
  const reports = new Map();
  const stats = Object.fromEntries(STATS.map(key => [key, null]));
  let stage = '';
  let eventError = '';
  let result = null;
  let connectivity = [];
  const applyConnectivity = rows => {
    const valid = Array.isArray(rows) ? rows.filter(isObject) : [];
    if (valid.length) connectivity = valid;
  };
  const applyStats = values => {
    if (!isObject(values)) return;
    for (const key of STATS) {
      if (Number.isSafeInteger(values[key]) && values[key] >= 0) stats[key] = values[key];
    }
  };
  for (const event of Array.isArray(record?.events) ? record.events : []) {
    if (!isObject(event?.payload)) continue;
    const payload = event.payload;
    if (event.kind === 'phase' && typeof payload.stage === 'string') stage = payload.stage;
    if (event.kind === 'report' && typeof payload.section === 'string' && typeof payload.content === 'string') reports.set(payload.section, payload.content);
    if (event.kind === 'usage') applyStats(payload);
    if (event.kind === 'connectivity') applyConnectivity(payload.connectivity);
    if (event.kind === 'result') result = payload;
    if (event.kind === 'error' && typeof payload.message === 'string') eventError = payload.message;
  }
  if (isObject(record?.result)) result = record.result;
  if (isObject(result?.reports)) {
    for (const [section, content] of Object.entries(result.reports)) {
      if (typeof content === 'string') reports.set(section, content);
    }
  }
  applyStats(result?.stats);
  applyConnectivity(result?.connectivity);
  return {
    active: ACTIVE.has(record?.status), stage, stats,
    reports: [...reports].map(([section, content]) => ({ section, content })),
    decision: typeof result?.decision === 'string' ? result.decision : '',
    connectivity,
    error: typeof record?.error === 'string' && record.error ? record.error : eventError,
  };
}

export function filterRuns(runs, query) {
  const needle = query.trim().toLocaleLowerCase();
  return runs.filter(run => [
    run.id, run.execution_name, run.created_at, run.request?.ticker, run.request?.analysis_date,
    run.request?.mode, run.request?.mode === 'connectivity' ? '连通测试' : '研究分析', STATUS_LABELS[run.status],
  ].some(value => typeof value === 'string' && value.toLocaleLowerCase().includes(needle)));
}

export function pollDelays(status, visible = true) {
  return { run: visible && (ACTIVE.has(status) || ['preparing', 'prepared', 'reconciliation_required'].includes(status)) ? 5000 : null, history: visible ? 30000 : null };
}

export function downloadUrl(id, format) {
  if (!UUID.test(id) || !['json', 'markdown'].includes(format)) throw new Error('下载参数无效。');
  return `/api/runs/${id}/download?format=${format}`;
}

export function createApi({ token = '', fetchImpl = globalThis.fetch, publicMode = false, emailMode = false } = {}) {
  return async (path, { method = 'GET', body, signal, revision } = {}) => {
    const localPath = /^\/api\/(config|runs(?:\/[0-9a-f-]+(?:\/stop)?)?)$/.test(path);
    const publicPath = /^\/(?:auth\/logout|api\/(?:me|profiles(?:\/[0-9a-f-]+(?:\/providers)?)?|initializations(?:\/[0-9a-f-]+(?:\/(?:confirm|apply))?)?|runs\/[0-9a-f-]+\/reconcile|resources\/(?:subscriptions|groups|catalog|deployments|validate)(?:\?[^#\s]*)?))$/.test(path);
    const emailPath = /^\/(?:auth\/(?:logout|password)|api\/(?:me|settings(?:\/catalog)?|budget|admin\/(?:users(?:\/[0-9a-f-]+\/disable)?|approved-origins|invitations(?:\/[A-Za-z0-9_-]+)?|recoveries)|assistant\/sessions(?:\/[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}(?:\/messages)?)?|runs(?:\/[0-9a-f-]+(?:\/(?:reconcile|resume|stop))?)?))$/.test(path);
    const assistantPath = /^\/api\/assistant\/sessions(?:\/[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}(?:\/messages)?)?$/.test(path);
    const marketPath = /^\/api\/market\/(?:watchlist|snapshot)$/.test(path);
    const adminAccountPath = /^\/api\/admin\/accounts\/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/.test(path);
    const adminReadPath = /^\/api\/admin\/(?:accounts|overview|audit|usage\?days=(?:7|30|90)(?:&account_id=[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})?)$/.test(path);
    if (!localPath && !(publicMode && publicPath) && !(emailMode && (emailPath || marketPath || adminAccountPath || adminReadPath))) throw new Error('Only same-origin workbench API paths are allowed.');
    if (adminReadPath && method !== 'GET' || adminAccountPath && method !== 'PATCH') throw new Error('Unsupported admin request method.');
    if (adminAccountPath && !revision) throw new Error('Account revision is required.');
    if (marketPath && !(path.endsWith('/watchlist') ? ['GET', 'PUT'] : ['POST']).includes(method)) throw new Error('Unsupported market request method.');
    if (!((publicMode || emailMode) ? ['GET', 'POST', 'PUT', 'DELETE', ...(emailMode && (assistantPath || adminAccountPath) ? ['PATCH'] : [])] : ['GET', 'POST']).includes(method)) throw new Error('不支持的请求方法。');
    const write = method !== 'GET';
    if (write && !token) throw new Error('会话令牌缺失，请刷新配置后重试。');
    const headers = { Accept: 'application/json' };
    if (write) {
      headers['X-Workbench-Token'] = token;
      if (body !== undefined) headers['Content-Type'] = 'application/json';
      if (revision) headers['If-Match'] = revision;
    }
    const timeout = AbortSignal.timeout(20000);
    const response = await fetchImpl(path, {
      method, headers, credentials: 'same-origin', cache: 'no-store', redirect: 'error',
      signal: signal ? AbortSignal.any([signal, timeout]) : timeout,
      ...(body === undefined ? {} : { body: JSON.stringify(body) }),
    });
    if (!response.ok) {
      const messages = {
        401: '登录或资源授权已失效，请重新授权。',
        403: '会话令牌已过期或来源校验失败，请刷新后重试。',
        404: '记录或接口不存在，请刷新历史。', 409: '已有任务运行或状态已变化，请刷新后重试。',
        422: '请求未通过校验，请检查配置。', 503: '执行服务暂不可用，请检查部署状态后重试。',
      };
      const error = new Error(messages[response.status] || `请求失败 (HTTP ${response.status})，请稍后重试。`);
      error.status = response.status;
      if (publicMode && response.status === 403) error.message = '当前身份或请求无权执行此操作，请核对 Azure 权限和会话。';
      if (emailMode && response.status === 403) error.message = '当前邮箱会话无权执行此操作，请重新登录或联系管理员。';
      if ((publicMode || emailMode) && response.status === 401) {
        try {
          const detail = await response.json();
          if (detail?.code === 'authentication_required') {
            error.code = detail.code;
            if (['https://management.azure.com/.default', 'https://api.loganalytics.io/.default', 'https://cognitiveservices.azure.com/.default'].includes(detail.scope)) error.scope = detail.scope;
          }
        } catch { /* Ignore untrusted error bodies. */ }
      }
      throw error;
    }
    return response.json();
  };
}