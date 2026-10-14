import { createIcons, icons, mountScenes } from './vendor.js';
import { projectRun, SECTION_LABELS, STATUS_LABELS } from './ui-core.mjs';
import { mountLocale, bindLocale, onLocaleChange, translate } from './locale.mjs';

const paint = root => createIcons({ icons, root, attrs: { 'aria-hidden': 'true', 'stroke-width': 1.7 } });
const openDialog = dialog => typeof dialog.showModal === 'function' ? dialog.showModal() : dialog.setAttribute('open', '');
const closeDialog = dialog => typeof dialog.close === 'function' ? dialog.close() : dialog.removeAttribute('open');

export function mountAppearance(doc) {
  const win = doc.defaultView;
  const interfaceLocale = doc.__interfaceLocale || mountLocale(doc);
  const root = doc.documentElement;
  const removers = [];
  const on = (target, type, handler) => { target.addEventListener(type, handler); removers.push(() => target.removeEventListener(type, handler)); };
  const media = win.matchMedia?.('(prefers-reduced-motion: reduce)');
  const motion = () => !media?.matches && root.dataset.motion !== 'reduced' && !doc.hidden;
  let transition = null;
  let launcher = null;
  let disposed = false;
  const palettes = { mono: '黑白', ice: '冰蓝', pine: '松绿', coral: '珊瑚', amber: '琥珀' };
  root.dataset.palette = 'mono';
  try {
    const saved = win.localStorage.getItem('tradingagents.studio.palette');
    if (Object.hasOwn(palettes, saved)) root.dataset.palette = saved;
  } catch {}
  try {
    const saved = JSON.parse(win.sessionStorage.getItem('ta-studio-appearance') || '{}');
    for (const [key, choices] of Object.entries({ theme: ['light', 'dark'], motion: ['full', 'reduced'], density: ['comfortable', 'compact'], grid: ['visible', 'hidden'] })) {
      if (choices.includes(saved[key]) && !(key === 'theme' && new URLSearchParams(win.location.search).has('scoutTheme'))) root.dataset[key] = saved[key];
    }
  } catch {}
  root.dataset.theme ||= win.matchMedia?.('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
  const dialog = doc.createElement('dialog');
  dialog.id = 'appearance-dialog'; dialog.className = 'studio-dialog appearance-dialog'; dialog.setAttribute('aria-labelledby', 'appearance-title');
  dialog.innerHTML = `<header class="studio-dialog-heading"><div><p class="section-label">APPEARANCE</p><h2 id="appearance-title">外观</h2></div><button class="icon-button" type="button" data-studio-close="appearance-dialog" aria-label="关闭外观设置" title="关闭"><i data-lucide="x"></i></button></header><div class="studio-settings"><div class="palette-strip"><span class="palette-swatches" role="img" aria-label="冰白、石墨、冷蓝"><i></i><i></i><i></i></span><div><strong>Graphite / Ice</strong><small>冰白 · 石墨 · 冷蓝</small></div></div><fieldset><legend>主题</legend><div class="segments"><label><input name="appearance-theme" type="radio" value="light"><span><i data-lucide="sun"></i>浅色</span></label><label><input name="appearance-theme" type="radio" value="dark"><span><i data-lucide="moon"></i>深色</span></label></div></fieldset><fieldset><legend>工作台密度</legend><div class="segments"><label><input name="appearance-density" type="radio" value="comfortable"><span>舒适</span></label><label><input name="appearance-density" type="radio" value="compact"><span>紧凑</span></label></div></fieldset><label class="studio-switch"><span>登录背景网格</span><input id="appearance-grid" type="checkbox" role="switch"></label><label class="studio-switch"><span>减少动态效果</span><input id="appearance-motion" type="checkbox" role="switch"></label></div>`;
  doc.body.append(dialog);
  const paletteOptions = doc.createElement('fieldset');
  paletteOptions.innerHTML = `<legend>配色方案</legend><div class="palette-options">${Object.entries(palettes).map(([value, label]) => `<label class="palette-option" data-palette="${value}" title="${label}"><input type="radio" name="appearance-palette" value="${value}"><span class="palette-preview" aria-hidden="true"><i data-lucide="check"></i></span><span>${label}</span></label>`).join('')}</div>`;
  dialog.querySelector('.palette-strip').replaceWith(paletteOptions);
  const languageControl = doc.createElement('label');
  languageControl.className = 'studio-switch language-switch';
  languageControl.innerHTML = '<span>界面语言</span><select id="appearance-language"><option value="auto">跟随浏览器</option><option value="zh">中文</option><option value="en">英语</option></select>';
  dialog.querySelector('.studio-settings').append(languageControl);
  const controls = doc.createElement('div'); controls.className = 'studio-tools';
  controls.innerHTML = '<button id="appearance-button" type="button" class="icon-button" title="外观设置" aria-label="外观设置" aria-haspopup="dialog"><i data-lucide="settings-2"></i></button><button id="theme-toggle" type="button" class="icon-button" title="切换主题" aria-label="切换主题"><i data-lucide="moon"></i></button>';
  (doc.querySelector('[data-studio-tools]') || doc.querySelector('.topbar-end'))?.prepend(controls);
  dialog.dataset.localeRoot = '';
  controls.dataset.localeRoot = '';
  bindLocale(doc, '#appearance-dialog, .studio-tools');
  const byId = id => doc.getElementById(id);
  const save = () => { try { win.sessionStorage.setItem('ta-studio-appearance', JSON.stringify({ theme: root.dataset.theme, motion: root.dataset.motion, density: root.dataset.density, grid: root.dataset.grid })); } catch {} };
  function sync() {
    const dark = root.dataset.theme === 'dark';
    const button = byId('theme-toggle'); button.title = translate(dark ? '切换浅色主题' : '切换深色主题'); button.setAttribute('aria-label', button.title); button.innerHTML = `<i data-lucide="${dark ? 'sun' : 'moon'}"></i>`;
    dialog.querySelectorAll('[name="appearance-theme"]').forEach(input => { input.checked = input.value === root.dataset.theme; });
    dialog.querySelectorAll('[name="appearance-palette"]').forEach(input => { input.checked = input.value === root.dataset.palette; });
    dialog.querySelectorAll('[name="appearance-density"]').forEach(input => { input.checked = input.value === (root.dataset.density || 'comfortable'); });
    byId('appearance-grid').checked = root.dataset.grid !== 'hidden';
    byId('appearance-language').value = interfaceLocale.preference;
    byId('appearance-motion').checked = Boolean(media?.matches || root.dataset.motion === 'reduced'); byId('appearance-motion').disabled = Boolean(media?.matches);
    root.dataset.hidden = String(doc.hidden);
    root.dataset.pageHidden = String(doc.hidden);
    if (!motion()) { transition?.skipTransition(); byId('login-grid-hover')?.setAttribute('hidden', ''); }
    paint(controls);
  }
  function setTheme(theme, origin) {
    transition?.skipTransition();
    const apply = () => { if (!disposed) { root.dataset.theme = theme; sync(); save(); } };
    if (!doc.startViewTransition || !motion() || doc.querySelector('dialog[open]')) { apply(); return; }
    const bounds = origin.getBoundingClientRect(); const centerX = bounds.left + bounds.width / 2; const centerY = bounds.top + bounds.height / 2;
    root.style.setProperty('--reveal-x', `${centerX}px`); root.style.setProperty('--reveal-y', `${centerY}px`);
    root.style.setProperty('--reveal-radius', `${Math.hypot(Math.max(centerX, win.innerWidth - centerX), Math.max(centerY, win.innerHeight - centerY))}px`);
    const next = doc.startViewTransition(apply); transition = next;
    next.ready.catch(() => {}); next.finished.catch(() => {}).finally(() => { if (transition === next) transition = null; });
  }
  on(byId('appearance-button'), 'click', event => { launcher = event.currentTarget; sync(); openDialog(dialog); });
  on(byId('theme-toggle'), 'click', event => setTheme(root.dataset.theme === 'dark' ? 'light' : 'dark', event.currentTarget));
  on(dialog.querySelector('[data-studio-close]'), 'click', () => { closeDialog(dialog); launcher?.focus(); });
  on(dialog, 'click', event => { if (event.target !== dialog) return; const bounds = dialog.getBoundingClientRect(); if (event.clientX < bounds.left || event.clientX > bounds.right || event.clientY < bounds.top || event.clientY > bounds.bottom) { closeDialog(dialog); launcher?.focus(); } });
  on(dialog, 'change', event => {
    const input = event.target;
    if (input.id === 'appearance-language') interfaceLocale.set(input.value);
    if (input.name === 'appearance-palette' && input.checked && Object.hasOwn(palettes, input.value)) {
      root.dataset.palette = input.value;
      try { win.localStorage.setItem('tradingagents.studio.palette', input.value); } catch {}
    }
    if (input.name === 'appearance-theme') setTheme(input.value, input);
    if (input.name === 'appearance-density') root.dataset.density = input.value;
    if (input.id === 'appearance-grid') { root.dataset.grid = input.checked ? 'visible' : 'hidden'; byId('login-grid-hover')?.setAttribute('hidden', ''); }
    if (input.id === 'appearance-motion') root.dataset.motion = input.checked ? 'reduced' : 'full';
    sync(); save();
  });
  on(doc, 'visibilitychange', sync);
  const offLocale = onLocaleChange(sync);
  if (media) on(media, 'change', sync);
  sync(); paint(dialog);
  return { motion, dispose() { disposed = true; offLocale(); transition?.skipTransition(); removers.forEach(remove => remove()); dialog.remove(); controls.remove(); } };
}

export function mountStudio(doc, { getRuns, getSelected, selectReport }) {
  const appearance = mountAppearance(doc);
  const disposeScenes = mountScenes(doc);
  const removers = [];
  const on = (target, type, handler) => { target.addEventListener(type, handler); removers.push(() => target.removeEventListener(type, handler)); };
  const byId = id => doc.getElementById(id);
  const tabs = doc.querySelector('.workspace-tabs');
  const indicator = doc.createElement('span');
  indicator.className = 'view-indicator'; indicator.setAttribute('aria-hidden', 'true'); tabs.append(indicator);
  const alignIndicator = () => {
    const selected = tabs.querySelector('[aria-selected="true"]');
    if (!selected) return;
    indicator.style.setProperty('--tab-left', `${selected.offsetLeft}px`);
    indicator.style.setProperty('--tab-width', `${selected.offsetWidth}px`);
  };
  const tabObserver = new doc.defaultView.MutationObserver(alignIndicator);
  tabObserver.observe(tabs, { subtree: true, attributes: true, attributeFilter: ['aria-selected'] });
  const tabResize = doc.defaultView.ResizeObserver ? new doc.defaultView.ResizeObserver(alignIndicator) : null;
  tabResize?.observe(tabs);
  on(doc.defaultView, 'resize', alignIndicator);
  removers.push(() => { tabObserver.disconnect(); tabResize?.disconnect(); indicator.remove(); });
  alignIndicator();
  const node = (tag, text, className) => { const result = doc.createElement(tag); if (text !== undefined) result.textContent = text; if (className) result.className = className; return result; };
  const launcher = node('button', undefined, 'icon-button'); launcher.id = 'command-button'; launcher.type = 'button'; launcher.title = '搜索任务与报告 (Ctrl+K)'; launcher.setAttribute('aria-label', '全局搜索'); launcher.setAttribute('aria-haspopup', 'dialog'); launcher.innerHTML = '<i data-lucide="search"></i>'; doc.querySelector('.studio-tools').prepend(launcher);
  const dialog = node('dialog', undefined, 'studio-dialog command-dialog'); dialog.id = 'command-dialog'; dialog.setAttribute('aria-labelledby', 'command-title');
  dialog.innerHTML = '<h2 id="command-title" class="sr-only">全局搜索</h2><div class="command-header"><i data-lucide="search"></i><input id="command-query" type="search" maxlength="200" placeholder="搜索证券、任务或报告…" role="combobox" aria-label="搜索任务与报告" aria-autocomplete="list" aria-controls="command-results" aria-expanded="false" autocomplete="off"><button class="icon-button" type="button" aria-label="关闭搜索" title="关闭" data-studio-close="command-dialog"><i data-lucide="x"></i></button></div><div id="command-results" role="listbox" aria-label="匹配任务与报告"></div><p id="command-empty" class="empty-inline" hidden>没有匹配的任务或报告</p><footer class="command-footer"><span id="command-count" role="status"></span></footer>';
  doc.body.append(dialog);
  dialog.dataset.localeRoot = '';
  launcher.dataset.localeRoot = '';
  bindLocale(doc, '#command-dialog, #command-button');
  let matches = []; let selectedIndex = 0; let disposed = false; let navigation = 0;
  const loadedRuns = new Map();
  const query = byId('command-query');
  function sync(reset = false) {
    const selected = getSelected();
    const available = new Map(getRuns().map(run => [run.id, run]));
    for (const id of loadedRuns.keys()) if (!available.has(id)) loadedRuns.delete(id);
    if (selected && (Array.isArray(selected.events) || selected.result)) loadedRuns.set(selected.id, selected);
    for (const [id, run] of loadedRuns) if (available.has(id)) available.set(id, run);
    if (!dialog.open) return;
    const previous = reset ? null : matches[selectedIndex];
    const terms = query.value.trim().toLowerCase().split(/\s+/).filter(Boolean);
    const records = getRuns().map(run => ({ id: run.id, section: 'all', label: run.request?.ticker || run.id, detail: `${run.request?.analysis_date || ''} / ${translate(STATUS_LABELS[run.status]) || run.status}`, search: `${run.id} ${run.request?.ticker} ${run.request?.analysis_date} ${run.status} ${STATUS_LABELS[run.status] || ''}` }));
    for (const run of available.values()) records.push(...projectRun(run).reports.map(report => ({ id: run.id, section: report.section, label: `${run.request?.ticker || ''} / ${translate(SECTION_LABELS[report.section]) || report.section}`, detail: run.request?.analysis_date || '', search: `${run.request?.ticker} ${report.section} ${SECTION_LABELS[report.section] || ''} ${report.content}` })));
    matches = records.filter(item => terms.every(term => item.search.toLowerCase().includes(term)));
    const preserved = previous ? matches.findIndex(item => item.id === previous.id && item.section === previous.section) : -1; selectedIndex = preserved < 0 ? 0 : preserved;
    byId('command-results').replaceChildren(...matches.map((item, index) => {
      const option = node('button', undefined, 'command-result'); option.type = 'button'; option.id = `command-result-${index}`; option.tabIndex = -1; option.dataset.index = String(index); option.setAttribute('role', 'option'); option.setAttribute('aria-selected', String(index === selectedIndex));
      const text = node('span'); text.append(node('strong', item.label), node('small', item.detail)); option.append(text); return option;
    }));
    byId('command-empty').hidden = matches.length > 0; byId('command-count').textContent = `${matches.length} ${translate('项结果 · 历史任务与已加载报告')}`;
    if (matches.length) query.setAttribute('aria-activedescendant', `command-result-${selectedIndex}`); else query.removeAttribute('aria-activedescendant');
  }
  function open() { if (disposed || doc.querySelector('dialog[open]')) return; query.value = ''; openDialog(dialog); query.setAttribute('aria-expanded', 'true'); sync(true); query.focus(); }
  function close() { closeDialog(dialog); query.setAttribute('aria-expanded', 'false'); launcher.focus(); }
  async function activate(index) {
    const match = matches[index]; if (!match) return;
    const version = ++navigation; close(); await selectReport(match.id, match.section);
    if (disposed || version !== navigation || getSelected()?.id !== match.id) return;
    const reader = byId('report-reader'); reader.tabIndex = -1; reader.focus({ preventScroll: true }); reader.scrollIntoView?.({ block: 'start', behavior: appearance.motion() ? 'smooth' : 'instant' });
  }
  on(launcher, 'click', open); on(query, 'input', () => sync(true)); on(dialog.querySelector('[data-studio-close]'), 'click', close);
  on(dialog, 'close', () => query.setAttribute('aria-expanded', 'false'));
  on(byId('command-results'), 'click', event => { const option = event.target.closest('[data-index]'); if (option) void activate(Number(option.dataset.index)); });
  on(query, 'keydown', event => {
    if (event.isComposing) return;
    if (event.key === 'Enter') { event.preventDefault(); void activate(selectedIndex); }
    else if (['ArrowUp', 'ArrowDown'].includes(event.key) && matches.length) {
      event.preventDefault(); selectedIndex = (selectedIndex + (event.key === 'ArrowDown' ? 1 : -1) + matches.length) % matches.length;
      byId('command-results').querySelectorAll('[role="option"]').forEach((option, index) => option.setAttribute('aria-selected', String(index === selectedIndex)));
      query.setAttribute('aria-activedescendant', `command-result-${selectedIndex}`); byId(`command-result-${selectedIndex}`).scrollIntoView?.({ block: 'nearest' });
    }
  });
  on(doc, 'keydown', event => { if (!event.isComposing && (event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'k' && !doc.querySelector('dialog[open]')) { event.preventDefault(); open(); } });
  paint(launcher); paint(dialog);
  const offLocale = onLocaleChange(() => sync());
  return { sync, motion: appearance.motion, clear() { navigation += 1; matches = []; loadedRuns.clear(); query.value = ''; byId('command-results').replaceChildren(); query.removeAttribute('aria-activedescendant'); if (dialog.open) close(); }, dispose() { disposed = true; offLocale(); navigation += 1; loadedRuns.clear(); matches = []; removers.forEach(remove => remove()); appearance.dispose(); disposeScenes(); dialog.remove(); launcher.remove(); } };
}