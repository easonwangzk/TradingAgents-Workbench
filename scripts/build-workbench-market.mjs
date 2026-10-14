import { readFile, writeFile } from 'node:fs/promises';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';
const staticUrl = new URL('../tradingagents/workbench/static/', import.meta.url);
const require = createRequire(new URL('package.json', staticUrl));
const { build } = require('esbuild');
await build({
  entryPoints: [fileURLToPath(new URL('market-chart-entry.mjs', import.meta.url))],
  nodePaths: [fileURLToPath(new URL('node_modules', staticUrl))],
  bundle: true, format: 'esm', minify: true, legalComments: 'external',
  outfile: fileURLToPath(new URL('market-chart.mjs', staticUrl)),
});
const packageUrl = new URL('node_modules/lightweight-charts/', staticUrl);
const legal = new URL('market-chart.mjs.LEGAL.txt', staticUrl);
const bundled = await readFile(legal, 'utf8').catch(() => '');
const license = await readFile(new URL('LICENSE', packageUrl), 'utf8');
const notice = await readFile(new URL('NOTICE', packageUrl), 'utf8').catch(() => '');
await writeFile(legal, `${bundled}\n${license}\n${notice}`, 'utf8');
await build({
  entryPoints: [fileURLToPath(new URL('vendor-entry.js', staticUrl))],
  nodePaths: [fileURLToPath(new URL('node_modules', staticUrl))],
  bundle: true, format: 'esm', minify: true, legalComments: 'external',
  outfile: fileURLToPath(new URL('vendor.js', staticUrl)),
});
console.log('Built same-origin market-chart.mjs, license and studio Lucide vendor.');