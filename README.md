# ListingLabs

**Live site → https://ahmedhailane0.github.io/listinglabs/**

ListingLabs studies how newly‑listed crypto tokens behave around their listing —
focused on **Binance Alpha** launches and the venues that follow (Binance
spot/perp, Coinbase, OKX, Bybit, Kraken, KuCoin, Bitget, Gate.io, and the Korean
exchanges). It's a **static site** that rebuilds and redeploys itself on a
schedule with **no server and no API keys** — only public, keyless data sources.

The site has three sub‑reports under one roof:

| Report | What it shows |
|---|---|
| **Listing Reactions** | Per‑token launch charts + a sortable/filterable grid (TGE, since‑launch, 24h/7d/30d/90d, FDV, MC, OI%, funding). ~78 tokens. |
| **Listing Funnel** | The Alpha → Perp → Coinbase → Korea progression across ~74 tokens. |
| **Scam Watchlist** | FDV‑behaviour flags and references for tokens worth watching. |

---

## How the autonomous pipeline works

Everything is driven by one GitHub Actions workflow,
[`.github/workflows/update-site.yml`](.github/workflows/update-site.yml), which
runs on a `*/20 * * * *` cron (every ~20 min, **best‑effort** — GitHub delays and
skips scheduled runs on free public repos, so refreshes land roughly **every
2–4 hours** in practice). Each run:

1. **`fetch/refresh_klines.py`** — re‑pulls each token's 5‑minute candles from its
   GeckoTerminal pool up to *now* and merges them into the cache, so charts stay
   current while the original launch‑reaction candles are never lost.
2. **`build_all.py`** — polls the BWEnews RSS feed for new listing signals, folds
   them into the token data, and rebuilds all three reports + the landing page.
3. **Commits** the refreshed `cache/` and `listings/` back to the repo (this is
   the autonomous state) and **deploys** the built site to **GitHub Pages**.

### Refresh priority — newest listings first

GeckoTerminal rate‑limits the shared CI IP hard, so each run only refreshes a
capped set of tokens (`REFRESH_LIMIT`, default **20**). Those slots are spent
**weighted toward the newest listings**:

- the **newest `REFRESH_NEWEST` listings** (default **14**, ordered by listing
  date) refresh on **every run**, so the actively‑watched charts stay current;
- the remaining slots cycle the **most‑stale of the older tokens**, so the long
  tail still updates — just less often;
- on‑chain pools that keep coming back empty are tracked in
  `cache/refresh_skip.json` and **sink to the back** of the older tier instead of
  wasting the budget.

Because the order is keyed off each token's **actual listing timestamp**
(`window_start_utc` / earliest Binance‑Alpha event), the moment a newer listing
is added it automatically becomes the **#1 refresh priority** — no manual
reordering. *(So: any new `listings/<token>.json` must carry a real
`window_start_utc`.)*

---

## Is it updating? How to check

Three quick checks, in order of effort:

1. **Actions tab** → <https://github.com/ahmedhailane0/listinglabs/actions> — a
   green run means it built and deployed. Yellow ⚠️ annotations (e.g. Node.js
   deprecation notices) are warnings, **not** failures.
2. **Bot commits** — every successful data refresh is one `listinglabs-bot`
   commit, and the file list *is* what changed that run:
   ```bash
   git fetch origin main && git log origin/main --author=listinglabs-bot --stat -5
   ```
3. **The live site** — each per‑token detail page carries an "as of HH:MM UTC"
   stamp.

---

## Repository layout

```
verifysheet/                     ← repo root (GitHub repo "listinglabs")
├─ .github/workflows/update-site.yml   the cron build + deploy
├─ cache/                        accumulated candles, news feed, refresh state
└─ perps_correlation/            all the code and the site
   ├─ build_all.py               one command rebuilds the whole site
   ├─ fetch/                     everything that talks to the internet (refresh_klines, OI, news…)
   ├─ build/                     turns cached data into the site (build_listing_report, build_scams…)
   ├─ lib/                       shared chart/metric helpers
   ├─ listings/<token>.json      one config per tracked token
   ├─ funnel/                    the Listing Funnel report
   ├─ study/                     the original perps study (frozen)
   ├─ tools/                     maintenance one‑offs (audits, backfills)
   └─ Listinglabs/               the built static site (deployed as a Pages artifact)
```

## Security model

- The repo is **public** (needed for free Pages + unlimited Actions), and the
  cloud build uses **no API keys** — only public, keyless endpoints — so there is
  nothing secret in CI to leak.
- The root **`.gitignore` is a security‑first whitelist**: only
  `perps_correlation/`, `cache/`, `.github/`, this README and the `.gitignore`
  itself are tracked. Everything else in the working tree stays local even on
  `git add .`.
- Any keyed data source (used only for local enrichment) reads its key from
  **outside the repo**, never from a tracked file.
