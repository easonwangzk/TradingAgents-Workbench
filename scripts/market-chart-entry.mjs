import { createChart, LineSeries, CandlestickSeries, ColorType } from 'lightweight-charts';

export function mountMarketChart(container) {
  const win = container.ownerDocument.defaultView;
  const root = container.ownerDocument.documentElement;
  const color = name => win.getComputedStyle(root).getPropertyValue(name).trim();
  const options = () => ({
    width: Math.max(1, container.clientWidth), height: Math.max(1, container.clientHeight),
    layout: { background: { type: ColorType.Solid, color: color('--cp-surface') }, textColor: color('--cp-text-muted'), fontFamily: win.getComputedStyle(container).fontFamily, fontSize: 11 },
    grid: { vertLines: { color: color('--cp-border') }, horzLines: { color: color('--cp-border') } },
    rightPriceScale: { borderColor: color('--cp-border') },
    timeScale: { borderColor: color('--cp-border'), timeVisible: false },
    handleScroll: { mouseWheel: true, pressedMouseMove: true, horzTouchDrag: true, vertTouchDrag: false },
  });
  const chart = createChart(container, options());
  let series = null, currentMode = null, currentSnapshot = null, currentKey = null, disposed = false;
  const seriesOptions = mode => mode === 'candles' ? {
    upColor: color('--cp-success'), downColor: color('--cp-danger'), wickUpColor: color('--cp-success'), wickDownColor: color('--cp-danger'), borderVisible: false,
  } : { color: color('--cp-accent'), lineWidth: 2 };
  function update(snapshot, mode = 'line') {
    if (disposed) return;
    if (mode !== currentMode) {
      if (series) chart.removeSeries(series);
      series = chart.addSeries(mode === 'candles' ? CandlestickSeries : LineSeries, seriesOptions(mode)); currentMode = mode;
    }
    currentSnapshot = snapshot;
    const rows = new Map();
    for (const candle of snapshot?.candles || []) {
      if (!/^\d{4}-\d{2}-\d{2}$/.test(candle.time) || !Number.isFinite(new Date(`${candle.time}T00:00:00Z`).getTime()) || new Date(`${candle.time}T00:00:00Z`).toISOString().slice(0, 10) !== candle.time || !Number.isFinite(candle.close)) continue;
      if (mode === 'candles') {
        if (!['open', 'high', 'low'].every(key => Number.isFinite(candle[key])) || candle.high < Math.max(candle.open, candle.close) || candle.low > Math.min(candle.open, candle.close) || candle.high < candle.low) continue;
        rows.set(candle.time, { time: candle.time, open: candle.open, high: candle.high, low: candle.low, close: candle.close });
      } else rows.set(candle.time, { time: candle.time, value: candle.close });
    }
    const data = [...rows.values()].sort((first, second) => first.time.localeCompare(second.time));
    series.setData(data);
    const key = snapshot ? `${snapshot.selected}/${snapshot.source}/${data[0]?.time}/${data.at(-1)?.time}/${mode}` : null;
    if (key !== currentKey && data.length) chart.timeScale().fitContent();
    currentKey = key;
  }
  const resize = () => { if (!disposed) chart.resize(Math.max(1, container.clientWidth), Math.max(1, container.clientHeight)); };
  const resizeObserver = win.ResizeObserver ? new win.ResizeObserver(resize) : null;
  resizeObserver?.observe(container);
  const appearanceObserver = new win.MutationObserver(() => {
    if (disposed) return;
    chart.applyOptions(options()); series?.applyOptions(seriesOptions(currentMode)); update(currentSnapshot, currentMode);
  });
  appearanceObserver.observe(root, { attributes: true, attributeFilter: ['data-theme', 'data-palette'] });
  win.addEventListener('resize', resize);
  return { update, resize, dispose() { if (disposed) return; disposed = true; resizeObserver?.disconnect(); appearanceObserver.disconnect(); win.removeEventListener('resize', resize); chart.remove(); } };
}