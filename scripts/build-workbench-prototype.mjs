import { readFile, writeFile } from 'node:fs/promises';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';

const staticUrl = new URL('../tradingagents/workbench/static/', import.meta.url);
const require = createRequire(new URL('package.json', staticUrl));
const { build } = require('esbuild');
const prototypeUrl = new URL('../docs/prototypes/research-studio.html', import.meta.url);
const iconNames = [
  'Activity', 'ArrowUpRight', 'Plus', 'Search', 'ShieldCheck', 'ChevronRight',
  'Moon', 'Sun', 'Sparkles', 'PanelLeft', 'CalendarDays', 'Users', 'Play', 'Pause',
  'Square', 'RotateCcw', 'Download', 'FileText', 'Braces', 'Database', 'Layers',
  'ChartNoAxesColumnIncreasing', 'Cpu', 'Globe', 'X', 'ChartNoAxesCombined',
  'Newspaper', 'Landmark', 'MessagesSquare', 'Settings2', 'ChevronDown', 'Check',
  'FileCheck2', 'ScanLine', 'CircleAlert', 'Clock3', 'Eye', 'EyeOff', 'Mail', 'LockKeyhole', 'ArrowRight',
];
const result = await build({
  stdin: {
    contents: `import { createIcons, ${iconNames.join(', ')} } from 'lucide';
      window.prototypeIcons = () => createIcons({ icons: { ${iconNames.join(', ')} }, attrs: { 'aria-hidden': 'true' } });`,
    resolveDir: fileURLToPath(staticUrl),
    sourcefile: 'prototype-icons.js',
  },
  bundle: true, write: false, minify: true, format: 'iife', target: 'es2022', legalComments: 'inline',
});
const motion = await build({
  stdin: {
    contents: `${await readFile(new URL('prototype-motion.mjs', import.meta.url), 'utf8')}\nmountScenes(document);`,
    resolveDir: fileURLToPath(staticUrl), sourcefile: 'prototype-motion.mjs',
  },
  bundle: true, write: false, minify: true, format: 'iife', target: 'es2022', legalComments: 'inline',
});
const html = await readFile(prototypeUrl, 'utf8');
const slot = /<script id="lucide-vendor">[\s\S]*?<\/script>/g;
if ([...html.matchAll(slot)].length !== 1) throw new Error('Expected exactly one Lucide bundle slot');
const bundle = result.outputFiles[0].text.replaceAll('</script', '<\\/script');
const motionSlot = /<script id="motion-vendor">[\s\S]*?<\/script>/g;
if ([...html.matchAll(motionSlot)].length !== 1) throw new Error('Expected exactly one motion bundle slot');
const motionBundle = motion.outputFiles[0].text.replaceAll('</script', '<\\/script');
await writeFile(prototypeUrl, html.replace(slot, () => `<script id="lucide-vendor">${bundle}</script>`).replace(motionSlot, () => `<script id="motion-vendor">${motionBundle}</script>`), 'utf8');
console.log(`Embedded ${iconNames.length} local Lucide icons (${Buffer.byteLength(bundle)} bytes); no CDN dependency.`);
console.log(`Embedded original Three.js scenes (${Buffer.byteLength(motionBundle)} bytes).`);