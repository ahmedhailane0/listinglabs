# build/ — cached data in, site + archives out

No network calls here (except none — fetching belongs in `fetch/`). Run as
`python build/<name>.py` from `perps_correlation/`. `../build_all.py` chains
the site builders in the right order.

## The builders

- `apply_signals.py` — folds `cache/bwenews_signals.json` into
  `listings/<token>.json`: adds an event (tagged `source:"bwenews"`,
  announcement-time, approximate), prunes `not_listed`, extends
  `window_end_utc`. Idempotent; curated/candle-verified events always win;
  short tickers (≤2 chars) need the token NAME in the headline too.
- `build_funding.py` — offline merge of cached RootData + CryptoRank →
  `cache/funding.json`.
- `build_listing_report.py` — THE most-edited file. Builds
  `Listinglabs/report/`: index grid + per-token detail pages.
  - Index table = `LIST_COLS` (12 cols: # Token TGE Since 24h 7d 30d 90d FDV MC
    OI% Funding). Sort JS reads `th` index + `<td data-s>` dynamically, so
    adding/removing a column needs NO JS change — but DO update the CSS
    `nth-child` widths block and the column-count comment.
  - TGE column: `_tge_time()` = earliest precisely-observed venue event,
    skipping daily-resolution sweep artifacts (`_is_sweep`); falls back to the
    pool's first candle.
  - Charts: `lib.interactive_chart.chart_html()` (Lightweight Charts, vendored
    lib copied into report/ at build — CSP blocks CDNs). Plotly is still
    emitted only because the Scam Watchlist uses it.
  - Listing-time SYNC: most Alpha listings are stored as a date (00:00Z);
    `_resolved_events` snaps placeholder events onto the pool's first candle.
  - Venue chips: `CHIP_SPECS` — `events[].exchange` strings must match its
    prefixes to light up filters.
- `merge_sweep.py` — folds `cache/venue_sweep.json` (candle-verified CEX
  listings) into listings/*.json. Only ADDS; curated events win.
- `build_scams.py` — `Listinglabs/scams/`: the Manipulated watchlist (tiles +
  detail pages, OI/funding history chart from `cache/perp_history/`, holders,
  TGE column with newest-first default). Reuses reactions CSS/cells.
- `build_research_archive.py` — LOCAL-ONLY projection: appends one dated row
  per token per day to `research/<tab>/{daily.csv,latest.csv}`. Driven by the
  Windows task; `ARCHIVE_ASOF`/`ARCHIVE_CACHE`/... env vars let
  `tools/backfill_missing_days.py` reconstruct past days from git history.

## Output contract

Site builders write straight into `Listinglabs/` (report/, funnel/report/,
scams/). There is no separate report/ or share/ copy — don't recreate one.
`build_all.py` writes the landing page and (without `--no-zip`) the deploy zip.
