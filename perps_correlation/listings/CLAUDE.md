# listings/ — per-token config (the source of truth for the reactions report)

One JSON file per tracked token, lowercase symbol as filename (`ctr.json`).
`build/build_listing_report.py` renders these; `build/apply_signals.py` and
`build/merge_sweep.py` append to them autonomously; CI commits them back.

## Schema (see ctr.json for a full example)

```jsonc
{
  "token": "CTR", "name": "Citrea", "chain": "base",
  "token_contract": "0x…", "gecko_pool": "0x…",   // GeckoTerminal Alpha pool
  "fdv_usd": 259286830, "fdv_source": "…",
  "mcap_usd": 0, "circulating_supply": 0, "total_supply": 0, "cmc_slug": "citrea",
  "category": "…",
  "window_start_utc": "…Z", "window_end_utc": "…Z",  // default chart view window
  "events":      [ {"exchange": "Binance Alpha", "iso_time_utc": "…Z", "note": "…"} ],
  "annotations": [ {"label": "BN perp announce", "iso_time_utc": "…Z", "note": "…"} ],
  "not_listed":  [ "Binance main spot" ]
}
```

## Conventions that matter

- `events[].exchange` strings must match the chip prefixes in `CHIP_SPECS`
  (`build/build_listing_report.py`) or the venue filter won't light up.
- Times ending at exactly `00:00:00Z` are usually DATE-ONLY placeholders, not
  real moments. Sweep-derived events carry "earliest-candle sweep" /
  "daily resolution" in their note — `_is_sweep` filters them when a precise
  time is needed. Never hand-enter a fake midnight time for a known-time event.
- Auto-added events are tagged `"source": "bwenews"` and say
  "announcement time, approximate — not candle-verified" in the note. Curated,
  candle-verified events always win; automation never overwrites them.
- Live FDV/MC/supply come from `cache/token_market.json` at build time; the
  hand-entered `fdv_usd` here is the at-listing snapshot / fallback.
- New token? `tools/add_alpha_token.py` scaffolds the JSON, then seed its
  candles LOCALLY (`python fetch/refresh_klines.py <token>`) before pushing —
  CI can't do big first fetches (rate limits).
