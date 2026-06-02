# perps_correlation — project context

Working notes so this folder can be edited without re-discovering everything.
Read this first; it points at the few files that actually matter.

## What this is

Two things live here:

1. **The original study** — does anything (FDV, prior CEX count, VC funding…)
   predict how a Binance-Alpha-listed token's perp performs after launch?
   Output: `enriched_clean.csv` + `analysis.ipynb` + `PERPS_REPORT.docx`.
   This is largely *done* and rarely touched now. See `HOW_TO_RUN.md`.
2. **ListingLabs** — the live static website. This is the active surface. It has
   three sub-reports under one folder. **Almost all edits happen here.**

## ListingLabs site — the part you'll edit

Everything is ONE folder: `Listinglabs/`. Built by ONE command:

```powershell
python build_all.py            # rebuild everything, then zip Listinglabs.zip
python build_all.py --no-zip   # skip the deploy zip
```

`build_all.py` runs four builders in order and writes the landing page:

| Builder | Writes | Source of truth |
|---|---|---|
| `fetch_bwenews.py` | `../cache/bwenews_{feed,signals}.json` | BWEnews RSS poll (keyless) |
| `build_funding.py` | `../cache/funding.json` (offline merge) | rootdata cache + cryptorank |
| `build_listing_report.py` | `Listinglabs/report/` | `listings/*.json` + `charts/` + caches |
| `funnel/funnel_report.py` | `Listinglabs/funnel/report/` | `funnel/funnel_master.json` |
| `build_scams.py` | `Listinglabs/scams/` | `fetch_scam_data.py` outputs |

`refresh_klines.py` is **not** in `build_all.py` (it makes network calls). The
cron runs it *before* `build_all.py` to extend each token's candles to now;
locally run `python refresh_klines.py [tokens…]` when you want fresh charts.

The site is static HTML/CSS + Plotly. `Listinglabs.zip` is the deploy artifact
(historically uploaded to Netlify). There is **no** separate `report/` / `share/`
copy anymore — builders write straight into `Listinglabs/`. Don't recreate the
old split (`build_share.py`, top-level `report/`, `share/` are gone on purpose).

### The Listing Reactions report — `build_listing_report.py`

The most-edited file. Builds the token grid + per-token detail pages.

- **Index table** = `LIST_COLS` (`build_listing_report.py`). Columns:
  `# | Token | TGE | Since | 24h | 7d | 30d | 90d | FDV | MC | OI% | Funding`.
  Rows built by `_list_row()`; the table is client-side sortable (header click)
  and filterable by venue chips + search (`FILTER_SCRIPT`). The sort JS reads
  `th` index dynamically and each `<td data-s="...">`, so **adding/removing a
  column needs no JS change** — but DO update the CSS `nth-child` widths block
  (currently hard-codes 12 cols) and the column-count comment.
- **TGE column**: `_tge_time(cfg)` = earliest *precisely observed* venue event,
  **skipping daily-resolution sweep artifacts** (`_is_sweep` — notes containing
  "earliest-candle sweep" / "daily resolution" sit at midnight UTC and predate
  the real listing, e.g. CTR's fake `Gate.io 00:00Z` before the real Alpha
  `13:00Z`). Falls back to Alpha time. Rendered by `_tge_cell` (date + UTC time,
  sorted by epoch).
- **Charts**: each token gets an interactive Plotly candlestick from
  `interactive_chart.chart_html()` (5m/15m/1h/4h switcher, listing markers,
  announcement arrow). Falls back to the static PNG in `charts/` if no klines.
- **Metrics** (`metrics.py`): Since/peak/drawdown/checkpoints, computed purely
  from `cache/<token>_klines_5m_alpha.json` relative to the Alpha listing time.

### Per-token config — `listings/<token>.json`

One file per tracked token. Schema (see `listings/ctr.json` for a full example):

```jsonc
{
  "token": "CTR", "name": "Citrea", "chain": "base",
  "token_contract": "0x…", "gecko_pool": "0x…",   // GeckoTerminal Alpha pool
  "fdv_usd": 259286830, "fdv_source": "…",
  "mcap_usd": …, "circulating_supply": …, "total_supply": …, "cmc_slug": "citrea",
  "category": "…",
  "window_start_utc": "…Z", "window_end_utc": "…Z",   // default chart view window
  "events":      [ {"exchange": "Binance Alpha", "iso_time_utc": "…Z", "note": "…"}, … ],
  "annotations": [ {"label": "BN perp announce", "iso_time_utc": "…Z", "note": "…"} ],
  "not_listed":  [ "Binance main spot", … ]
}
```

`events[].exchange` strings must match the chip prefixes in `CHIP_SPECS`
(`build_listing_report.py`) to light up venue filters.

### Charts pipeline

- `listing_chart.py` — pulls 5m OHLCV from the **on-chain Alpha pool via
  GeckoTerminal** (true Alpha price, CEX fallback), renders annotated PNG to
  `charts/<token>_listing_reaction.png`. Run: `python listing_chart.py listings/<t>.json`.
- `interactive_chart.py` — same cached klines → embedded Plotly div for detail pages.
- Klines cached at `../cache/<token>_klines_5m_alpha.json`.

## Venue allowlist (IMPORTANT)

Only annotate/track: **Binance Alpha + Binance Spot + Binance Perp**, and the
majors **Coinbase / OKX / Bybit / Kraken / KuCoin / Bitget / Gate.io**, plus the
Korean venues **Upbit / Bithumb / Coinone**. **Drop** MEXC / BitMart / HTX /
LBank / XT / BingX — they are excluded by design (see `note_dropped_venues` in
some configs). This is the canonical chip set in `CHIP_SPECS`.

## Data sources & caches

Fetchers save into `../cache/` (sibling of this folder) so re-runs are incremental:

- Prices/perp: Binance `fapi`/`api`, GeckoTerminal (Alpha pools), Kraken/KuCoin/
  Gate/Coinbase public OHLC (`sweep_venues.py`, `venues.py`, `probe_listings*.py`).
- FDV / MC / supply: CoinMarketCap web `data-api` (keyless) — `fetch_oi_cmc.py`
  → `oi_cmc.json` for **current** open interest. At-listing/historical aggregated
  OI still needs paid APIs (Coinalyze/Coinglass) — not wired.
- Funding/investors: RootData (`fetch_rootdata.py`, **needs API key**) +
  CryptoRank (`fetch_cryptorank.py`, keyless) → merged by `build_funding.py`.
- Announcements / socials: `fetch_announcements.py`, `fetch_token_socials.py`.
- Korean listing dates: `fetch_coinone.py`, plus cached `bithumb_*`, coinbase caches.

## Secrets & security (do not leak)

- The RootData API key lives **outside** the repo at
  `C:\Users\PC\.config\verifysheet\secrets.env`. Scripts read it from there.
- **Never commit** `secrets.env`, the key, or anything under `../cache/` that
  embeds a key. If this folder is ever pushed to a public GitHub repo, a
  `.gitignore` must exclude secrets and the key must be supplied to CI via an
  encrypted **GitHub Actions secret**, never hard-coded.

## Gotchas (learned the hard way)

- **FDV at listing ≠ current FDV.** Use perp price × total supply for
  at-listing FDV; current FDV is dump-biased low. (Study finding: higher
  FDV-at-listing → better 30d return, ρ≈+0.19.)
- **Daily-resolution sweep events are not real times** — they sit at 00:00Z and
  predate the true listing. Always filter them when you need a precise moment
  (TGE, first-listing). See `_is_sweep`.
- **OI matching**: CMC OI joins on `cg_id`-as-CMC-slug, not symbol match.
- **Drawdown** ignores the opening settling window (first on-chain candles wick
  violently — fake −70% no one traded). See `metrics.py`.
- Re-run any fetcher safely; caches make it incremental.

## Automation — forever-autonomous, $0 (built)

Keeps the site updating forever via **GitHub Actions cron** (no PC needed).
Setup is hand-held in `SETUP_GITHUB.md`. Pieces:

- **`.github/workflows/update-site.yml`** (repo root = `verifysheet/`): every
  ~20 min runs `refresh_klines.py` → `build_all.py --no-zip`, commits refreshed
  caches back (also keeps the schedule from auto-disabling), and deploys
  `Listinglabs/` to **GitHub Pages**. Cron is best-effort (GitHub delays/skips).
- **`fetch_bwenews.py`** — polls the **BWEnews RSS feed**
  (`https://rss-public.bwe-ws.com/`, keyless). Classifies listing headlines →
  `bwenews_signals.json` (venue + symbol + whether tracked). Rendered as a live
  signal strip on the reactions index (`_news_strip`, built server-side → no
  CORS). The websocket (`wss://bwenews-api.bwe-ws.com/ws`) is intentionally NOT
  used in the cloud path — a cron job can't hold a socket open; it's only an
  option for an always-on local listener.
- **`refresh_klines.py`** — re-pulls GeckoTerminal candles to *now* and MERGES
  with the cached launch-window candles (union by ts), so charts extend forever
  while the launch reaction is never lost. **Incremental**: only fetches candles
  *since the last cached one* (~1 page), so steady-state is ~0.8s/token. Tokens
  are processed **most-stale-first**; `REFRESH_LIMIT` env (or `--limit N`) caps how
  many a run touches. The interactive chart pins its default x-range to the launch
  window (`win_range` in `interactive_chart.py`); pan/zoom reveals the live history.
  NOTE: GeckoTerminal's free 5m endpoint caps ~12k candles (~41d) per fetch.

  **⚠️ Throttling lesson (don't relearn):** GeckoTerminal rate-limits (429) the
  shared **GitHub Actions IP hard** — a CI run managed only ~5 tokens' gap-fills in
  12 min. So: the expensive **one-time gap-fill is done LOCALLY** (`python
  refresh_klines.py`, unthrottled) and committed; **CI only does capped incremental
  refresh** (`REFRESH_LIMIT: "20"` in the workflow), round-robining all 78 tokens
  every ~80 min. If you add many new tokens, seed them locally first, then push —
  don't expect CI to do large fetches. Local IP is not throttled.

### Security model (must preserve)

- Repo is **public** (needed for free Pages + unlimited Actions). The cloud build
  uses **no API key** — only public keyless endpoints — so nothing secret is in
  CI. The RootData key stays in `~/.config/verifysheet/secrets.env` (outside repo).
- The **root `.gitignore` is a whitelist**: only `perps_correlation/` + `cache/`
  (+ `.github/`, `.gitignore`) are tracked; everything else in `verifysheet/`
  stays local even on `git add .`. Verified clean: no embedded keys in scripts or
  cache (only "RootData" as a source name, and token descriptions mentioning
  "password"). Don't relax this whitelist.
