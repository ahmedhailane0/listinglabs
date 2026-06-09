# lib/ — shared modules (imported as `from lib.x import …`)

Four modules, no side effects at import. Everything else in the project leans
on these — change them carefully and rebuild the site to verify.

- `listing_chart.py` — the chart workhorse:
  - `fetch_geckoterminal(chain, pool, start, end)` — 5m OHLCV from the token's
    on-chain Alpha pool (true Alpha price; raises SystemExit when empty).
  - `parse_iso`, `fmt_usd_compact`, `fmt_subscript_price` — used everywhere.
  - CLI: `python lib/listing_chart.py listings/<token>.json` renders the
    annotated PNG → `charts/<token>_listing_reaction.png` (the NEX→CTR→SLX
    reaction-chart workflow).
- `interactive_chart.py` — embedded TradingView Lightweight Charts HTML:
  - `chart_html()` — per-token area chart (5m/15m/1h/4h switcher, launch reset,
    listing + announcement markers) for the reactions report.
  - `timeseries_html()` — generic price series (Scam Watchlist price chart).
  - `_resolved_events` / `first_candle_dt` — derive the REAL Alpha listing
    moment from the pool's first candle; midnight placeholders get snapped,
    unresolvable CEX midnight events are dropped from the chart only.
  - `win_range` pins the default x-range to the launch window; pan/zoom reveals
    the live history. `_autofit_js` retained only for Plotly (scams report).
- `metrics.py` — Since/peak/drawdown/checkpoint math from
  `cache/<t>_klines_5m_alpha.json` relative to the Alpha listing time.
  Drawdown skips the opening settling window (first candles wick violently —
  fake −70% nobody traded).
- `venues.py` — `venue_color`, `venue_url`: canonical venue styling. The venue
  allowlist itself lives in `build/build_listing_report.py` (`CHIP_SPECS`).

## Known trap

If a Lightweight Chart ever shows no y-axis price labels, it's the global
`table{table-layout:fixed}` CSS leaking into the chart's internal table —
keep the `.tvchart table` reset in the report CSS.
