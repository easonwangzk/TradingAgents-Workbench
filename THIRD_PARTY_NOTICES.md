# Third-Party Notices

The repository's Apache-2.0 license does not replace licenses for bundled dependencies. The files below preserve the applicable copyright and grant texts. Existing generated `.LEGAL.txt` sidecars are retained as additional notices.

| Component | Version | License / Retained Text |
| --- | --- | --- |
| [Lucide](https://github.com/lucide-icons/lucide) | 1.38.0 | [ISC and included Feather-derived MIT notices](licenses/lucide-1.38.0-LICENSE) |
| [Marked](https://github.com/markedjs/marked) | 18.0.11 | [MIT and included Markdown notices](licenses/marked-18.0.11-LICENSE) |
| [Three.js](https://github.com/mrdoob/three.js) | 0.186.0 | [MIT](licenses/three-0.186.0-LICENSE) |
| [DOMPurify](https://github.com/cure53/DOMPurify) | 3.4.14 | [Apache-2.0 option](licenses/dompurify-3.4.14-LICENSE) and [MPL-2.0 text](licenses/dompurify-3.4.14-LICENSE-MPL) retained |
| [Lightweight Charts](https://github.com/tradingview/lightweight-charts) | 5.0.9 | [Apache-2.0](licenses/lightweight-charts-5.0.9-LICENSE), [original attribution NOTICE](licenses/lightweight-charts-5.0.9-NOTICE) |
| [Fancy Canvas](https://github.com/tradingview/fancy-canvas) | 2.1.0 | [MIT](licenses/fancy-canvas-2.1.0-LICENSE); included by Lightweight Charts |
| [tslib](https://github.com/microsoft/tslib) | Portions identified by Lightweight Charts | [BSD Zero Clause notice](licenses/tslib-LICENSE) retained |

Three.js is included in the distributed visual bundle even though it is listed as a development dependency. The chart bundle includes Fancy Canvas; the chart package also identifies incorporated tslib portions. Full notices apply to these redistributed portions. Libraries installed separately through Python or npm retain their own package licenses.

## Upstream Source And Illustrations

The research engine and the PNG illustrations in `assets/` originate from [TauricResearch/TradingAgents](https://github.com/TauricResearch/TradingAgents), source snapshot `9dee508c44662702281a8dbaad1f7b42179b5ba7`, distributed with its Apache-2.0 license. Original names and artwork are retained for attribution, not as this modified distribution's own branding or an endorsement claim. No separate grant of trademark rights is made.

## TradingView Attribution

Lightweight Charts is created by TradingView. Copyright (c) 2025 TradingView, Inc. See [TradingView](https://www.tradingview.com/). The original notice is copied without alteration, and the application's visible TradingView link remains in its footer. Keep these notices and the link when redistributing the chart application.

## License Provenance

Direct dependency license files were copied verbatim from the installed versions named in the lockfile. Fancy Canvas's license is retained from upstream tag `2.1.0`; the chart notice is from tag `v5.0.9`; the tslib notice is from its upstream license file. Copyright-holder names in these files are license provenance, not installation account data.