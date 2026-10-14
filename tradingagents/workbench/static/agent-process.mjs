import { onLocaleChange, translate } from './locale.mjs';

const ANALYST_ORDER = ['market', 'social', 'news', 'fundamentals'];
const STAGE_LABELS = Object.freeze({
  market: '市场分析师',
  social: '社交情绪分析师',
  news: '新闻分析师',
  fundamentals: '基本面分析师',
  bull_researcher: '看涨研究员',
  bear_researcher: '看跌研究员',
  research_manager: '研究主管',
  trader: '交易员',
  aggressive_risk: '激进风险分析师',
  conservative_risk: '保守风险分析师',
  neutral_risk: '中性风险分析师',
  portfolio_manager: '投资组合经理',
});

const STATIC_SEQUENCE = [
  'bull_researcher', 'bear_researcher', 'research_manager', 'trader',
  'aggressive_risk', 'conservative_risk', 'neutral_risk', 'portfolio_manager',
];
const LANES = [
  { label: '01 · 多维分析', stages: ANALYST_ORDER, loop: '分析师 / 工具循环' },
  { label: '02 · 投资辩论', stages: ['bull_researcher', 'bear_researcher', 'research_manager'], loop: '多空辩论循环' },
  { label: '03 · 交易方案', stages: ['trader'], loop: '' },
  { label: '04 · 风险决策', stages: ['aggressive_risk', 'conservative_risk', 'neutral_risk', 'portfolio_manager'], loop: '三方风控循环' },
];

const KNOWN_STAGES = new Set([...ANALYST_ORDER, ...STATIC_SEQUENCE]);
const STATUS_LABELS = { pending: '未观察', observed: '已观察', active: '进行中', done: '已完成', error: '失败', stopped: '已停止' };
const REPORT_NODE_MAP = Object.freeze({
  market_report: 'market',
  sentiment_report: 'social',
  social_report: 'social',
  news_report: 'news',
  fundamentals_report: 'fundamentals',
  trader_investment_plan: 'trader',
  final_trade_decision: 'portfolio_manager',
  'investment_debate_state.bull_history': 'bull_researcher',
  'investment_debate_state.bear_history': 'bear_researcher',
  'investment_debate_state.judge_decision': 'research_manager',
  'risk_debate_state.aggressive_history': 'aggressive_risk',
  'risk_debate_state.conservative_history': 'conservative_risk',
  'risk_debate_state.neutral_history': 'neutral_risk',
  'risk_debate_state.judge_decision': 'portfolio_manager',
});

function isObject(value) {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}

function isToolStage(stage) {
  return stage === 'tool' || stage.startsWith('tool:');
}

function normalizeEvents(events) {
  return Array.isArray(events) ? events.filter(event => {
    return isObject(event)
      && typeof event.kind === 'string'
      && isObject(event.payload)
      && (event.timestamp === undefined || typeof event.timestamp === 'string');
  }) : [];
}

function selectedSequence(run) {
  const selected = Array.isArray(run?.request?.analysts) ? run.request.analysts.filter(value => ANALYST_ORDER.includes(value)) : [];
  return [...ANALYST_ORDER.filter(value => selected.includes(value)), ...STATIC_SEQUENCE];
}

function mapObservedStages(events) {
  const stageEvents = [];
  const unmapped = [];
  for (const event of events) {
    if (event.kind !== 'phase' || typeof event.payload.stage !== 'string') continue;
    const stage = event.payload.stage;
    if (isToolStage(stage)) {
      stageEvents.push({ type: 'tool', stage, timestamp: event.timestamp || '', event });
      continue;
    }
    if (KNOWN_STAGES.has(stage)) {
      stageEvents.push({ type: 'stage', stage, timestamp: event.timestamp || '', event });
      continue;
    }
    stageEvents.push({ type: 'unknown', stage, timestamp: event.timestamp || '', event });
    unmapped.push({ stage, timestamp: event.timestamp || '', event });
  }
  return { stageEvents, unmapped };
}

function countToolsForStages(sequence, stageEvents) {
  const counts = new Map(sequence.map(stage => [stage, 0]));
  let currentStage = '';
  for (const entry of stageEvents) {
    if (entry.type === 'stage') {
      currentStage = entry.stage;
      continue;
    }
    if (entry.type === 'unknown') { currentStage = ''; continue; }
    if (entry.type !== 'tool') continue;
    if (!currentStage || !counts.has(currentStage)) continue;
    counts.set(currentStage, counts.get(currentStage) + 1);
  }
  return counts;
}

function buildNodeEventMap(sequence, events) {
  const result = new Map(sequence.map(stage => [stage, []]));
  let currentStage = '';
  for (const event of events) {
    if (event.kind === 'phase' && typeof event.payload.stage === 'string') {
      const stage = event.payload.stage;
      if (isToolStage(stage)) {
        if (currentStage && result.has(currentStage)) result.get(currentStage).push(event);
        continue;
      }
      if (result.has(stage)) {
        currentStage = stage;
        result.get(stage).push(event);
      } else currentStage = '';
      continue;
    }
    if (event.kind === 'report' && typeof event.payload.section === 'string') {
      const mappedStage = REPORT_NODE_MAP[event.payload.section];
      if (mappedStage && result.has(mappedStage)) result.get(mappedStage).push(event);
      continue;
    }
    if (currentStage && result.has(currentStage)) result.get(currentStage).push(event);
  }
  return result;
}

export function mergeAgentProcessEvents(baseEvents, incomingEvents) {
  const all = [...normalizeEvents(baseEvents), ...normalizeEvents(incomingEvents)];
  const deduped = [];
  const seen = new Set();
  for (const event of all) {
    const timestamp = typeof event.timestamp === 'string' ? event.timestamp : '';
    if (timestamp) {
      const key = JSON.stringify([event.kind, timestamp, event.payload]);
      if (seen.has(key)) continue;
      seen.add(key);
    }
    deduped.push(event);
  }
  deduped.sort((left, right) => {
    const leftTime = Date.parse(left.timestamp || '') || 0;
    const rightTime = Date.parse(right.timestamp || '') || 0;
    return leftTime - rightTime;
  });
  return deduped;
}

export function projectAgentProcess(run) {
  const events = normalizeEvents(run?.events);
  const sequence = selectedSequence(run);
  const { stageEvents, unmapped } = mapObservedStages(events);
  const toolCounts = countToolsForStages(sequence, stageEvents);
  const nodeEvents = buildNodeEventMap(sequence, events);
  const visits = new Map();
  const statuses = new Map(sequence.map(stage => [stage, 'pending']));
  let previousStage = '';
  for (const event of events) {
    if (event.kind === 'phase' && typeof event.payload.stage === 'string' && !isToolStage(event.payload.stage)) {
      const stage = event.payload.stage;
      if (stage !== previousStage) {
        if (statuses.get(previousStage) === 'active') statuses.set(previousStage, 'observed');
        if (statuses.has(stage)) {
          visits.set(stage, (visits.get(stage) || 0) + 1);
          statuses.set(stage, 'active');
        }
      }
      previousStage = statuses.has(stage) ? stage : '';
    } else if (event.kind === 'report' && typeof event.payload.content === 'string' && event.payload.content.trim()) {
      const stage = REPORT_NODE_MAP[event.payload.section];
      if (statuses.has(stage)) statuses.set(stage, 'done');
    } else if (event.kind === 'error' && previousStage) {
      statuses.set(previousStage, 'error');
    } else if (event.kind === 'result' && run?.status === 'succeeded' && statuses.get(previousStage) === 'active') {
      statuses.set(previousStage, 'done');
    }
  }
  const isError = run?.status === 'failed' || run?.status === 'stopped';
  const nodes = sequence.map((stage, index) => {
    let status = statuses.get(stage);
    if (isError && stage === previousStage && status === 'active') status = run.status === 'stopped' ? 'stopped' : 'error';
    return {
      id: stage,
      index,
      label: STAGE_LABELS[stage] || stage,
      status,
      toolCount: toolCounts.get(stage) || 0,
      visitCount: visits.get(stage) || 0,
      observed: status !== 'pending',
      events: nodeEvents.get(stage) || [],
    };
  });
  const summaryLabel = run?.status === 'stopped' ? '已停止' : isError ? '失败' : run?.status === 'succeeded' ? '已完成' : run?.status === 'running' ? '进行中' : '待处理';
  return { nodes, unmapped, summaryLabel, events };
}

function element(doc, tag, text, className) {
  const node = doc.createElement(tag);
  if (text !== undefined) node.textContent = String(text);
  if (className) node.className = className;
  return node;
}

function eventTimestamp(event) {
  return typeof event?.timestamp === 'string' ? event.timestamp : '';
}

function eventDetailLabel(event) {
  if (!event) return '--';
  if (event.kind === 'phase') return event.payload.stage;
  if (event.kind === 'report') return event.payload.section || 'report';
  if (event.kind === 'usage') return `模型 ${event.payload.llm_calls ?? '--'} / 工具 ${event.payload.tool_calls ?? '--'}`;
  if (event.kind === 'error') return event.payload.message || '错误';
  if (event.kind === 'result') return '结果';
  if (event.kind === 'connectivity') return '连通性';
  return event.kind;
}

function sourceLabel(mode, stream) {
  if (mode === 'replay') return '过程回放';
  if (stream?.connection === 'reconnecting') return '重连中';
  if (stream?.connection === 'connecting') return '连接中';
  if (stream?.source === 'mixed') return '混合事件 / 含日志补齐';
  if (stream?.source === 'direct') return '事件更新 / 实时';
  if (stream?.source === 'logs') return '延迟日志';
  return '轮询快照';
}

export function mountAgentProcess(doc, { renderMarkdown, paintIcons = () => {}, clock = doc.defaultView, onModeChange } = {}) {
  const panel = doc.getElementById('agent-process-panel');
  panel.replaceChildren();
  const header = element(doc, 'div', undefined, 'panel-heading');
  const title = element(doc, 'h2', 'Agent 过程');
  const status = element(doc, 'span', '轮询快照', 'muted small');
  status.id = 'agent-process-source';
  header.append(title, status);

  const controls = element(doc, 'div', undefined, 'agent-process-controls');
  const modeBadge = element(doc, 'span', '实时', 'agent-process-mode');
  modeBadge.id = 'agent-process-mode';
  const play = element(doc, 'button', undefined, 'secondary-button');
  play.type = 'button';
  play.id = 'agent-process-play';
  play.setAttribute('aria-label', '过程回放播放');
  const live = element(doc, 'button', undefined, 'secondary-button');
  live.type = 'button';
  live.id = 'agent-process-live';
  live.title = '返回实时';
  live.setAttribute('aria-label', '返回实时');
  live.innerHTML = '<i data-lucide="activity"></i><span>当前</span>';
  const speed = element(doc, 'select');
  speed.id = 'agent-process-speed';
  speed.setAttribute('aria-label', '回放速度');
  for (const value of ['1', '2', '4']) {
    const option = element(doc, 'option', `${value}x`);
    option.value = value;
    speed.append(option);
  }
  const range = element(doc, 'input');
  range.type = 'range';
  range.id = 'agent-process-range';
  range.min = '0';
  range.max = '0';
  range.step = '1';
  range.value = '0';
  range.setAttribute('aria-label', '过程回放进度');
  const time = element(doc, 'span', '--', 'mono small');
  time.id = 'agent-process-time';
  controls.append(modeBadge, play, live, speed, range, time);

  const layout = element(doc, 'div', undefined, 'agent-process-layout');
  const graph = element(doc, 'div', undefined, 'agent-process-graph');
  graph.id = 'agent-process-graph';
  graph.setAttribute('aria-label', 'Agent 过程图');
  const detail = element(doc, 'div', undefined, 'agent-process-detail');
  const detailTitle = element(doc, 'h3', '等待过程数据');
  detailTitle.id = 'agent-process-detail-title';
  const detailMeta = element(doc, 'p', '尚未收到事件。', 'muted small');
  detailMeta.id = 'agent-process-detail-meta';
  const detailBody = element(doc, 'div', undefined, 'agent-process-detail-body');
  detailBody.id = 'agent-process-detail-body';
  detail.append(detailTitle, detailMeta, detailBody);
  layout.append(graph, detail);
  panel.append(header, controls, layout);
  panel.dataset.localeRoot = '';

  let playbackTimer = null;
  let replayEvents = [];
  let replayRunStatus = '';
  let viewState = { run: null, stream: null, mode: 'live', selectedNodeId: '', replayIndex: 0, speed: 1, playing: false };
  const buttons = new Map();
  let graphKey = '';
  let detailKey = '';

  function stopPlayback() {
    if (playbackTimer !== null) clock.clearTimeout(playbackTimer);
    playbackTimer = null;
    viewState.playing = false;
  }

  function schedulePlayback() {
    if (playbackTimer !== null) clock.clearTimeout(playbackTimer);
    playbackTimer = null;
    if (!viewState.playing || viewState.mode !== 'replay') return;
    const events = replayEvents;
    if (viewState.replayIndex >= events.length) {
      viewState.playing = false;
      render();
      return;
    }
    playbackTimer = clock.setTimeout(() => {
      viewState.replayIndex += 1;
      render();
      schedulePlayback();
    }, Math.max(100, 1000 / viewState.speed));
  }

  function snapshotEvents() {
    const events = normalizeEvents(viewState.run?.events);
    return viewState.mode === 'replay' ? replayEvents.slice(0, viewState.replayIndex) : events;
  }

  function beginReplay() {
    if (viewState.mode === 'replay') return;
    replayEvents = normalizeEvents(viewState.run?.events).slice();
    replayRunStatus = viewState.run?.status;
    viewState.mode = 'replay';
    onModeChange?.('replay');
  }

  function selectDefaultNode(projected) {
    return projected.nodes.find(node => node.status === 'error')?.id
      || [...projected.nodes].reverse().find(node => node.events.some(event => event.kind === 'report'))?.id
      || projected.nodes.find(node => node.status === 'active')?.id
      || projected.nodes.find(node => node.status === 'done')?.id
      || projected.nodes[0]?.id
      || '';
  }

  function renderDetail(node) {
    detailTitle.textContent = translate(node?.label || '等待过程数据');
    detailMeta.textContent = node ? `${translate(STATUS_LABELS[node.status])} · ${translate('访问')} ${node.visitCount} · ${translate('工具')} ${node.toolCount}` : translate('尚未收到事件。');
    const nextKey = JSON.stringify([node?.id, node?.events]);
    if (nextKey === detailKey) return;
    detailKey = nextKey;
    detailBody.replaceChildren();
    if (!node) return;
    if (!node.events.length) {
      detailBody.append(element(doc, 'p', translate('该节点尚无已观察到的事件。'), 'empty-inline'));
      return;
    }
    for (const event of node.events) {
      const block = element(doc, 'section', undefined, 'agent-process-event');
      const meta = element(doc, 'div', undefined, 'agent-process-event-meta');
      meta.append(element(doc, 'strong', event.kind), element(doc, 'span', eventTimestamp(event) || '--', 'mono small'));
      block.append(meta);
      if (event.kind === 'report' && typeof event.payload.content === 'string') {
        const report = element(doc, 'div', undefined, 'agent-process-report');
        renderMarkdown(report, event.payload.content);
        block.append(report);
      } else {
        block.append(element(doc, 'pre', JSON.stringify(event.payload, null, 2), 'raw-json'));
      }
      detailBody.append(block);
    }
  }

  function render() {
    const events = snapshotEvents();
    const replayStatus = viewState.mode === 'replay' ? viewState.replayIndex < replayEvents.length ? 'running' : replayRunStatus : viewState.run?.status;
    const projected = projectAgentProcess(viewState.run ? { ...viewState.run, status: replayStatus, events } : null);
    const selectedNodeId = projected.nodes.some(node => node.id === viewState.selectedNodeId) ? viewState.selectedNodeId : selectDefaultNode(projected);
    viewState.selectedNodeId = selectedNodeId;
    title.textContent = translate('Agent 过程');
    status.textContent = translate(sourceLabel(viewState.mode, viewState.stream));
    const terminal = ['succeeded', 'failed', 'stopped'].includes(viewState.run?.status);
    modeBadge.textContent = translate(viewState.mode === 'replay' ? '回放' : terminal ? '运行快照' : '实时');
    const playLabel = viewState.playing ? '暂停' : '播放';
    play.title = translate(`过程回放${playLabel}`);
    play.setAttribute('aria-label', play.title);
    play.innerHTML = `<i data-lucide="${viewState.playing ? 'pause' : 'play'}"></i><span>${translate(playLabel)}</span>`;
    paintIcons(controls);
    live.hidden = viewState.mode === 'live';
    const allEvents = viewState.mode === 'replay' ? replayEvents : normalizeEvents(viewState.run?.events);
    play.disabled = !allEvents.length;
    range.max = String(allEvents.length);
    range.value = String(viewState.mode === 'replay' ? viewState.replayIndex : allEvents.length);
    const activeTimestamp = events.at(-1)?.timestamp || (viewState.mode === 'replay' ? '' : allEvents.at(-1)?.timestamp) || '--';
    time.textContent = activeTimestamp;
    const nextGraphKey = projected.nodes.map(node => node.id).join('|');
    if (nextGraphKey !== graphKey) {
      graphKey = nextGraphKey;
      buttons.clear();
      const groups = LANES.map((lane, laneIndex) => {
      const wrapper = element(doc, 'section', undefined, 'agent-process-lane');
      const heading = element(doc, 'div', undefined, 'agent-process-lane-heading');
      heading.append(element(doc, 'strong', translate(lane.label)), element(doc, 'span', translate(lane.loop), 'agent-process-loop'));
      const track = element(doc, 'div', undefined, 'agent-process-track');
      const laneNodes = projected.nodes.filter(item => lane.stages.includes(item.id));
      track.style.setProperty('--node-count', String(Math.max(1, laneNodes.length)));
      for (const node of laneNodes) {
        const button = element(doc, 'button', undefined, 'agent-process-node');
        button.type = 'button';
        const titleLine = element(doc, 'span', undefined, 'agent-process-node-title');
        titleLine.append(element(doc, 'strong'), element(doc, 'small'));
        button.append(titleLine);
        button.addEventListener('click', () => { viewState.selectedNodeId = node.id; render(); });
        buttons.set(node.id, button);
        track.append(button);
      }
      if (!laneNodes.length) track.append(element(doc, 'span', translate('未选择分析师'), 'muted small'));
      wrapper.append(heading, track);
      if (laneIndex < LANES.length - 1) wrapper.append(element(doc, 'div', '↓', 'agent-process-connector'));
      return wrapper;
      });
      graph.replaceChildren(...groups);
    }
    for (const node of projected.nodes) {
      const button = buttons.get(node.id);
      button.dataset.status = node.status;
      button.dataset.nodeId = node.id;
      button.setAttribute('aria-pressed', String(node.id === selectedNodeId));
      button.setAttribute('aria-label', `${translate(node.label)} ${translate(STATUS_LABELS[node.status])}`);
      button.querySelector('strong').textContent = translate(node.label);
      button.querySelector('small').textContent = [translate(STATUS_LABELS[node.status]), node.visitCount > 1 ? `${translate('访问')} ${node.visitCount}` : '', node.toolCount ? `${translate('工具')} ${node.toolCount}` : ''].filter(Boolean).join(' · ');
    }
    renderDetail(projected.nodes.find(node => node.id === selectedNodeId));
  }

  play.addEventListener('click', () => {
    if (viewState.mode === 'live') {
      beginReplay();
      viewState.replayIndex = Math.min(1, replayEvents.length);
    }
    if (viewState.replayIndex >= replayEvents.length) viewState.replayIndex = 0;
    viewState.playing = !viewState.playing;
    if (!viewState.playing) onModeChange?.('paused');
    render();
    schedulePlayback();
  });
  live.addEventListener('click', () => {
    stopPlayback();
    viewState.mode = 'live';
    viewState.replayIndex = normalizeEvents(viewState.run?.events).length;
    onModeChange?.('live');
    render();
  });
  speed.addEventListener('change', () => {
    viewState.speed = Number(speed.value) || 1;
    schedulePlayback();
  });
  range.addEventListener('input', () => {
    stopPlayback();
    beginReplay();
    viewState.replayIndex = Number(range.value) || 0;
    onModeChange?.('replay');
    render();
  });

  const offLocale = onLocaleChange(() => { graphKey = ''; detailKey = ''; render(); });
  return {
    render(run, stream) {
      if (viewState.run?.id && viewState.run.id !== run?.id) {
        stopPlayback();
        viewState.mode = 'live';
        viewState.selectedNodeId = '';
      }
      viewState.run = run;
      viewState.stream = stream;
      if (viewState.mode === 'live') viewState.replayIndex = normalizeEvents(run?.events).length;
      render();
    },
    stopPlayback() {
      stopPlayback();
      render();
    },
    setLiveMode() {
      stopPlayback();
      viewState.mode = 'live';
      onModeChange?.('live');
      render();
    },
    dispose() {
      offLocale();
      stopPlayback();
      buttons.clear();
    },
  };
}