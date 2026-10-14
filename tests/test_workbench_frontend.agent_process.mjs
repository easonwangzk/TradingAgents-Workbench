import test from 'node:test';
import assert from 'node:assert/strict';

const moduleUrl = new URL('../tradingagents/workbench/static/agent-process.mjs', import.meta.url);
let processModule;
try {
  processModule = await import(moduleUrl);
} catch (error) {
  if (error.code !== 'ERR_MODULE_NOT_FOUND' || !error.message.includes('agent-process.mjs')) throw error;
}

const baseRun = {
  id: 'fcd47836-8555-4dcf-a016-4668182e4d64',
  status: 'running',
  request: {
    mode: 'analysis',
    ticker: 'MSFT',
    analysis_date: '2026-09-29',
    asset_type: 'stock',
    analysts: ['market', 'social'],
    debate_rounds: 1,
    risk_rounds: 1,
    language: 'Chinese',
    data_vendors: {
      core_stock_apis: 'yfinance',
      technical_indicators: 'yfinance',
      fundamental_data: 'yfinance',
      news_data: 'yfinance',
      macro_data: 'fred',
      prediction_markets: 'polymarket',
    },
  },
  events: [],
  result: null,
  error: null,
};

test('the agent process frontend module is available', () => {
  assert.ok(processModule, 'agent-process.mjs has not been implemented');
});

test('agent process graph keeps selected analyst order and observed tool loops', { skip: !processModule }, () => {
  const run = {
    ...baseRun,
    events: [
      { kind: 'phase', timestamp: '2026-09-29T10:00:00Z', payload: { stage: 'market' } },
      { kind: 'phase', timestamp: '2026-09-29T10:00:01Z', payload: { stage: 'tool:get_verified_market_snapshot' } },
      { kind: 'usage', timestamp: '2026-09-29T10:00:02Z', payload: { llm_calls: 1, tool_calls: 1, input_tokens: 10, output_tokens: 4 } },
      { kind: 'report', timestamp: '2026-09-29T10:00:03Z', payload: { section: 'market_report', content: 'market body' } },
      { kind: 'phase', timestamp: '2026-09-29T10:01:00Z', payload: { stage: 'social' } },
      { kind: 'phase', timestamp: '2026-09-29T10:01:01Z', payload: { stage: 'tool:get_news' } },
      { kind: 'phase', timestamp: '2026-09-29T10:02:00Z', payload: { stage: 'bull_researcher' } },
      { kind: 'phase', timestamp: '2026-09-29T10:03:00Z', payload: { stage: 'bear_researcher' } },
      { kind: 'phase', timestamp: '2026-09-29T10:04:00Z', payload: { stage: 'research_manager' } },
      { kind: 'phase', timestamp: '2026-09-29T10:05:00Z', payload: { stage: 'trader' } },
      { kind: 'phase', timestamp: '2026-09-29T10:06:00Z', payload: { stage: 'aggressive_risk' } },
      { kind: 'phase', timestamp: '2026-09-29T10:07:00Z', payload: { stage: 'conservative_risk' } },
      { kind: 'phase', timestamp: '2026-09-29T10:08:00Z', payload: { stage: 'neutral_risk' } },
      { kind: 'phase', timestamp: '2026-09-29T10:09:00Z', payload: { stage: 'portfolio_manager' } },
    ],
  };

  const projected = processModule.projectAgentProcess(run);
  assert.deepEqual(projected.nodes.map(node => node.id), [
    'market', 'social',
    'bull_researcher', 'bear_researcher', 'research_manager', 'trader',
    'aggressive_risk', 'conservative_risk', 'neutral_risk', 'portfolio_manager',
  ]);
  assert.equal(projected.nodes.find(node => node.id === 'market').toolCount, 1);
  assert.equal(projected.nodes.find(node => node.id === 'social').toolCount, 1);
  assert.equal(projected.nodes.find(node => node.id === 'portfolio_manager').status, 'active');
  assert.equal(projected.nodes.find(node => node.id === 'news'), undefined);
});

test('an unknown phase does not attribute a later error to the previous analyst', { skip: !processModule }, () => {
  const run = {
    ...baseRun,
    status: 'failed',
    events: [
      { kind: 'phase', timestamp: '2026-09-29T10:00:00Z', payload: { stage: 'market' } },
      { kind: 'phase', timestamp: '2026-09-29T10:00:01Z', payload: { stage: 'unknown_custom_stage' } },
      { kind: 'error', timestamp: '2026-09-29T10:00:02Z', payload: { message: 'provider failed' } },
    ],
  };

  const projected = processModule.projectAgentProcess(run);
  assert.equal(projected.nodes.find(node => node.id === 'market').status, 'observed');
  assert.equal(projected.nodes.find(node => node.id === 'social').status, 'pending');
  assert.match(projected.summaryLabel, /失败/);
  assert.equal(projected.unmapped.length, 1);
  assert.equal(projected.unmapped[0].stage, 'unknown_custom_stage');
});

test('event snapshot merge deduplicates identical events and preserves streamed events across stale polling', { skip: !processModule }, () => {
  const polled = [
    { kind: 'phase', timestamp: '2026-09-29T10:00:00Z', payload: { stage: 'market' } },
    { kind: 'phase', timestamp: '2026-09-29T10:01:00Z', payload: { stage: 'social' } },
  ];
  const streamed = [
    { kind: 'phase', timestamp: '2026-09-29T10:01:00Z', payload: { stage: 'social' } },
    { kind: 'phase', timestamp: '2026-09-29T10:02:00Z', payload: { stage: 'bull_researcher' } },
  ];

  const merged = processModule.mergeAgentProcessEvents(polled, streamed);
  assert.deepEqual(merged.map(event => event.payload.stage), ['market', 'social', 'bull_researcher']);
});

test('visits follow stage transitions, unknown phases release ownership, and final decision belongs to portfolio manager', () => {
  const events = [
    { kind: 'phase', payload: { stage: 'bull_researcher' } },
    { kind: 'phase', payload: { stage: 'bull_researcher' } },
    { kind: 'phase', payload: { stage: 'bear_researcher' } },
    { kind: 'phase', payload: { stage: 'bull_researcher' } },
    { kind: 'phase', payload: { stage: 'unknown_stage' } },
    { kind: 'phase', payload: { stage: 'tool:orphan' } },
    { kind: 'report', payload: { section: 'final_trade_decision', content: 'HOLD' } },
  ];
  const projected = processModule.projectAgentProcess({ ...baseRun, status: 'succeeded', events });
  assert.equal(projected.nodes.find(node => node.id === 'bull_researcher').visitCount, 2);
  assert.equal(projected.nodes.find(node => node.id === 'bull_researcher').status, 'observed');
  assert.equal(projected.nodes.find(node => node.id === 'bull_researcher').toolCount, 0);
  assert.equal(projected.nodes.find(node => node.id === 'portfolio_manager').status, 'done');
  assert.equal(projected.nodes.find(node => node.id === 'portfolio_manager').events.at(-1).payload.content, 'HOLD');
  assert.equal(projected.nodes.find(node => node.id === 'trader').status, 'pending');
});

test('a truncated completed run and a partial replay never complete unobserved nodes', () => {
  const events = [{ kind: 'phase', timestamp: '2026-09-29T10:00:00Z', payload: { stage: 'market' } }];
  const partial = processModule.projectAgentProcess({ ...baseRun, status: 'succeeded', events });
  assert.equal(partial.nodes.find(node => node.id === 'market').status, 'active');
  assert.equal(partial.nodes.find(node => node.id === 'social').status, 'pending');
});

test('generic tool events retain their current analyst ownership', () => {
  const projected = processModule.projectAgentProcess({ ...baseRun, events: [
    { kind: 'phase', payload: { stage: 'market' } },
    { kind: 'phase', payload: { stage: 'tool' } },
    { kind: 'usage', payload: { tool_calls: 1 } },
  ] });
  const market = projected.nodes.find(node => node.id === 'market');
  assert.equal(market.status, 'active');
  assert.equal(market.toolCount, 1);
  assert.equal(market.events.length, 3);
});