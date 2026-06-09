# fetch/ — everything that talks to the internet

Only this layer makes network calls. Every script writes into `../cache/`
(= `verifysheet/cache/`) so re-runs are incremental and CI commits the results.
All scripts are run as `python fetch/<name>.py` from `perps_correlation/`.

## CI scripts (run by the cloud cron every ~20 min, all keyless)

| Script | Writes | Notes |
|---|---|---|
| `refresh_klines.py` | `cache/<t>_klines_5m_alpha.json` | Extends each token's 5m candles to now, MERGED with launch window (union by ts). Incremental (~1 page/token). `REFRESH_LIMIT`/`REFRESH_NEWEST`/`REFRESH_WORKERS` env caps; newest listings refresh every run, older tail round-robins most-stale-first; dead pools (`cache/refresh_skip.json`) sink to the back. Binance-spot fallback + 8x price contamination guard. |
| `fetch_perp_markets.py` | `cache/perp_markets/<S>.json`, appends `cache/perp_history/<S>.json` | Per-venue perp OI+funding via CoinGecko /derivatives (one server-side call — CI IP can't reach Binance/OKX/Bybit directly). Coverage guard + carry-forward: an empty/throttled run NEVER clobbers a cached snapshot or logs a $0 history point. `--direct` = per-exchange APIs, LOCAL-ONLY. |
| `refresh_scam_prices.py` | updates `cache/scam_data.json`, `cache/scam_prices/` | Keyless CoinGecko price/series + CMC authoritative supply for watchlist tokens. `SCAM_LIMIT` cap, most-stale-first, writes after every token. |
| `fetch_holders.py` | `cache/scam_holders/` | GoPlus top-holders, multichain EVM; non-EVM degrades to "unavailable". |
| `fetch_scam_tge.py` | `cache/scam_tge.json` | TGE dates: CMC dateLaunched, CoinGecko genesis fallback. Static — only fetches new tokens. |
| `fetch_token_market.py` | `cache/token_market.json` | Live CMC FDV/MC/supply per listings token; the report prefers these over hand-entered values. |
| `fetch_perp_announce.py` | `cache/perp_announce.json` | Precise Binance perp-announce times (CMS publishDate) → auto spike annotations. |
| `sweep_venues.py` | `cache/venue_sweep.json` | Candle-verified CEX listing discovery. `--ci` skips OKX/Bybit (451 in CI); plain run locally fills them. Never caches a venue it couldn't reach. `build/merge_sweep.py` folds results into listings. |
| `fetch_bwenews.py` | `cache/bwenews_{feed,signals}.json` | BWEnews RSS poll → classified listing signals (venue + symbol + tracked?). Run inside build_all. The websocket is deliberately NOT used (cron can't hold a socket). |

## LOCAL-ONLY scripts (CI IP is blocked or a key is needed)

- `fetch_rootdata.py` — funding/investors; needs the RootData key from
  `~/.config/verifysheet/secrets.env`. Live API now returns empty; the cached
  `rootdata.json` is the real source.
- `fetch_cryptorank.py` — keyless funding rounds; merged by build_funding.
- `fetch_scam_data.py` — full watchlist build (needs local CSV + RootData);
  `refresh_scam_prices.py` is its CI-safe price-only subset.
- `fetch_oi_cmc.py` — current OI via CMC web data-api → `../oi_cmc.json`
  (NOTE: output sits at perps_correlation root, not cache/). Historical/at-listing
  aggregated OI still needs paid APIs — not wired.
- `fetch_announcements.py`, `fetch_token_socials.py`, `fetch_coinone.py`,
  `fetch_binance_spot.py`, `fetch_scam_chains.py`, `fetch_perp_klines.py` —
  occasional enrichment fetchers; outputs cached, rarely re-run.

## Throttling lessons (don't relearn)

- GeckoTerminal 429s the shared GitHub Actions IP hard: a CI run managed ~5
  gap-fills in 12 min. One-time gap-fills are done LOCALLY and committed; CI
  only does capped incremental refresh.
- Binance/OKX/Bybit return 451 to datacenter IPs → their direct fetches are
  local-only; CI goes through CoinGecko's server-side aggregator.
- GeckoTerminal's free 5m endpoint caps ~12k candles (~41d) per fetch — that's
  why refresh merges instead of refetching.
