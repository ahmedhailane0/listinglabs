# cache/ — all fetched data (committed by CI so state survives forever)

Every fetcher writes here; the cloud cron commits it back every ~20 min, so
this folder IS the project's memory. Safe to re-fetch anything — everything is
incremental. Never put a secret here.

## What lives here

- `<token>_klines_5m_alpha.json` — 5m OHLCV per reactions token
  (launch window + live tail, merged forever). `refresh_skip.json` tracks dead
  pools so they stop burning the CI refresh budget.
- `perp_markets/<SYM>.json` — latest per-venue perp OI+funding snapshot.
- `perp_history/<SYM>.json` — appended OI/funding time series (~4 pts/day,
  carry-forward points tagged `src:"carry"`).
- `token_market.json` — live CMC FDV/MC/supply for reactions tokens.
- `scam_data.json` + `scam_prices/` + `scam_holders/` + `scam_tge.json` —
  the Manipulated watchlist's record (market data, 180d series, top holders,
  TGE dates).
- `bwenews_feed.json` / `bwenews_signals.json` — rolling RSS feed + classified
  listing signals.
- `venue_sweep.json` — candle-verified CEX listing discovery state.
- `funding.json` — merged RootData+CryptoRank funding (rootdata.json is the
  cached raw; the live API now returns empty).
- `perp_announce.json` — precise Binance perp-announce timestamps.
- Korean/exchange probes (`bithumb_*`, coinone, coinbase caches) and other
  one-time enrichment outputs.

Rule of thumb: code never goes here, data never goes anywhere else.
