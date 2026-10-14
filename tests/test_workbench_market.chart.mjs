import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
const staticUrl = new URL('../tradingagents/workbench/static/', import.meta.url);

test('Market chart is an allowlisted same-origin standalone bundle with licensed proven engine', async () => {
  const entry = await readFile(new URL('../scripts/market-chart-entry.mjs', import.meta.url), 'utf8');
  assert.match(entry, /from 'lightweight-charts'/);
  const build = await readFile(new URL('../scripts/build-workbench-market.mjs', import.meta.url), 'utf8');
  assert.match(build, /market-chart\.mjs/);
  assert.match(build, /legalComments: 'external'/);
  const bundle = await readFile(new URL('market-chart.mjs', staticUrl), 'utf8');
  assert.doesNotMatch(bundle, /from\s*["']https?:|import\(["']https?:/);
  assert.match(bundle, /mountMarketChart/);
  const license = await readFile(new URL('market-chart.mjs.LEGAL.txt', staticUrl), 'utf8');
  assert.match(license, /TradingView|Apache/);
});

test('Market uses existing theme tokens, dense unframed chart and list-detail mobile layout', async () => {
  const css = await readFile(new URL('styles.css', staticUrl), 'utf8');
  assert.match(css, /\.market-layout\s*\{[^}]*grid-template-columns:/);
  assert.match(css, /#market-chart\s*\{[^}]*height:/);
  assert.match(css, /data-market-view="detail"[\s\S]*?\.market-list/);
  assert.match(css, /\.market-number[^}]*font-variant-numeric:\s*tabular-nums/);
});