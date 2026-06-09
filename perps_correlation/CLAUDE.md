# perps_correlation — the program

The ListingLabs pipeline. Layers (each has its own CLAUDE.md with the detail):

- `fetch/` — every script that talks to the internet (refresh candles, OI,
  holders, news, market data…). CI-safe vs LOCAL-ONLY split documented there.
- `build/` — every script that turns cached data into output (the website's
  three reports, the landing page, the research CSV archive).
- `lib/` — shared modules imported by the others: `listing_chart` (PNG charts +
  GeckoTerminal fetch + parse/format helpers), `interactive_chart` (Lightweight
  Charts HTML), `metrics` (since/peak/drawdown math), `venues` (colors/URLs).
- `listings/` — one JSON config per tracked token (the report's source of truth).
- `funnel/` — the CEX→Korea funnel study and its report builder.
- `study/` — the original "what predicts perp performance" study (frozen).
- `tools/` — maintenance one-offs (audits, backfills, cache repair).
- `docs/` — SETUP_GITHUB.md (Actions/Pages hand-holding) + audit reports.
- `charts/` — generated PNGs. `vendor/` — vendored Lightweight Charts JS.
- `Listinglabs/` — THE BUILD OUTPUT (the website). Never hand-edit; rebuilt by
  `build_all.py`; deployed to GitHub Pages as an artifact (not read from git).

## Import convention (matters when editing/adding scripts)

Scripts in subfolders import shared code as packages:
`from lib.listing_chart import parse_iso`, `from fetch import fetch_oi_cmc`,
`from build.merge_sweep import classify`. Each runnable script carries a 3-line
sys.path bootstrap so it works from any working directory. Moved scripts keep a
`HERE = Path(__file__).resolve().parents[1]` constant that points at THIS folder
(perps_correlation/), not their subfolder — all data paths hang off it
(`HERE.parent / "cache"`, `HERE / "listings"`, …). Keep that convention.

## One command builds everything

```powershell
python build_all.py            # rebuild all reports + landing, then zip
python build_all.py --no-zip   # what CI runs
```

Order inside: `fetch/fetch_bwenews.py` (RSS poll) → `build/apply_signals.py`
(fold new listing signals into listings/*.json) → `build/build_funding.py` →
`build/build_listing_report.py` → `funnel/funnel_report.py` →
`build/build_scams.py` → landing page. Network-heavy refreshers are NOT in
build_all — the CI workflow runs them as separate capped steps first
(see `fetch/CLAUDE.md`).

## Automation (forever-autonomous, $0)

`.github/workflows/update-site.yml` (repo root): cron every ~20 min, runs the
capped fetch steps → build_all → uploads `Listinglabs/` as the Pages artifact →
commits cache/ + listings/ back (rebase-retry push, best-effort). Every fetch
step is `continue-on-error` + timeout so nothing flaky blocks a deploy.
Workflow paths reference `fetch/...` and `build/...` — keep them in sync when
moving scripts.

## Venue allowlist (IMPORTANT)

Only annotate/track: **Binance Alpha / Spot / Perp**, majors **Coinbase / OKX /
Bybit / Kraken / KuCoin / Bitget / Gate.io**, Korean **Upbit / Bithumb /
Coinone**. **Drop** MEXC / BitMart / HTX / LBank / XT / BingX — excluded by
design. Canonical chip set: `CHIP_SPECS` in `build/build_listing_report.py`.

## Gotchas (learned the hard way — don't relearn)

- **FDV at listing ≠ current FDV.** Use perp price × total supply at listing;
  current FDV is dump-biased low. (Study: higher FDV-at-listing → better 30d
  return, ρ≈+0.19.)
- **Daily-resolution sweep events are not real times** — they sit at 00:00Z and
  predate the true listing. Filter with `_is_sweep` when you need a precise
  moment (TGE, first-listing).
- **OI matching:** CMC OI joins on `cg_id`-as-CMC-slug, not symbol match.
- **Drawdown** ignores the opening settling window (first on-chain candles wick
  violently). See `lib/metrics.py`.
- **Chart y-axis blank?** A global `table{table-layout:fixed}` leaking into
  Lightweight Charts' internal table — fix is the `.tvchart table` CSS reset.
- GeckoTerminal & CoinGecko **rate-limit the shared CI IP hard**; Binance/OKX/
  Bybit **451 datacenter IPs**. Heavy/blocked fetches run LOCALLY and get
  committed; CI only does capped incremental work. Details in `fetch/CLAUDE.md`.
- Re-running any fetcher is safe; caches make everything incremental.

## Secrets

RootData API key lives outside the repo: `C:\Users\PC\.config\verifysheet\
secrets.env`. Never commit a key; CI is 100% keyless by design.
