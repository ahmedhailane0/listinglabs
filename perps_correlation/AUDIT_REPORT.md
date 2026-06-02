# ListingLabs — Security & Quality Audit

**Date:** 2026-06-02  
**Target:** ListingLabs static site + autonomous build pipeline  
**Live:** https://ahmedhailane0.github.io/listinglabs/  
**Repo:** github.com/ahmedhailane0/listinglabs (public)  
**Auditor:** automated internal + external stress test (`audit_internal.py`, `audit_external.py`)

---

## 1. Executive summary

**Verdict: PASS — production-ready, no critical or high-severity defects.** The site
is fully available, the autonomous pipeline is self-healing, scraped third-party
content is safely escaped, and no secret material is exposed. The findings below are
**data-quality and polish** items, not stability or security risks.

| Severity | Count | Nature |
|---|---:|---|
| 🔴 Critical / High | **0** | — |
| 🟠 Medium | **4** | Data-quality + signal-robustness (consolidated) |
| 🟡 Low | **6** | Freshness, performance, hardening, hygiene |

**Remediation status:** **all findings have now been addressed** across two passes —
M-2/M-4/L-3/L-4 (pass 1, §5.1) and M-1/M-3/L-1/L-2/L-6 (pass 2, §5.2). The only residual
is a small set of tokens with no live price source anywhere (documented in L-1).

**Availability:** 78/78 token pages + landing/funnel/scams/assets all return HTTP 200.  
**Security posture:** no key in CI (250 staged files scanned clean), whitelist `.gitignore`
proven, scraped content escaped at two layers, HSTS enabled.  
**Resilience:** idempotent signal application, `[skip ci]` loop guard, graceful
fetch-failure fallback (keeps cache), capped/incremental refresh.

---

## 2. Scope & methodology

- **Internal (white-box):** parsed all 78 `listings/*.json`; validated schema, event
  integrity (parse/sort/dupes), TGE derivation, chart-window coverage, kline-cache
  presence + staleness, FDV/MC sanity; scanned all generated report HTML for unescaped
  scraped content; reviewed `apply_signals.py`, `refresh_klines.py`, `fetch_bwenews.py`,
  the workflow, and the `.gitignore` whitelist.
- **External (black-box):** crawled the live Pages site — HTTP status + size + timing of
  every page and shared asset, all 78 detail pages (parallel), live-vs-local build
  parity, response headers, and the health of the three external dependencies.
- **Reproducible:** both harnesses are committed; re-run any time with
  `python audit_internal.py` / `python audit_external.py`.

---

## 3. What's healthy (verified, not assumed)

- **Availability — 100%.** 78/78 detail pages, landing, funnel, scams, `style.css`,
  `plotly.min.js` all 200. Zero broken links or missing assets.
- **No stored XSS.** BWEnews titles are HTML-stripped on ingest (`_clean()`) *and*
  HTML-escaped on render (`html.escape(ev["note"])`). Scan of all 78 pages: **0**
  raw-tag/`javascript:` leaks in any auto-added note.
- **No secret exposure.** Both API-key values from the external `secrets.env` were
  searched across all 250 staged files → **0 hits**. CI runs **keyless**. The
  whitelist `.gitignore` was empirically proven to stage only `perps_correlation/`,
  `cache/`, `.github/`, `.gitignore`.
- **Self-healing pipeline.** `apply_signals` is idempotent (never duplicates/overwrites
  curated events); the bot commit carries `[skip ci]` (no infinite loop); a fetch 404
  keeps the cached candles instead of erroring; refresh is capped + incremental so a
  CI run can't run away (the earlier 27-min runaway is fixed — run #4 completed in <2 min).
- **External dependencies healthy.** BWEnews RSS (200/364ms), GeckoTerminal (200/179ms),
  GitHub API (200/359ms). HSTS enabled on the live host.

---

## 4. Findings

### 🟠 M-1 — Bitget "earliest-candle sweep" events carry a bogus uniform date
**Area:** data quality · **Evidence:** 31 tokens have a `Bitget Spot` event (and 1 an
`OKX Spot` event) timestamped **identically** `2025-08-04T00:00:00Z`, all noted
*"earliest-candle sweep (daily resolution)"*. An identical date across 31 unrelated
tokens is a data artifact (the API's earliest-available daily candle / sweep date), not
a real listing time.  
**Impact:** the events table presents a misleading listing date for those venues. **TGE
is unaffected** — `_tge_time` already excludes sweep events.  
**Remediation:** render sweep-sourced events with a "≈ date only" qualifier (or hide the
time), or re-verify Bitget/OKX first-trade times via their public OHLC APIs (the
`sweep_venues.py` infra already supports this). Low effort, high credibility gain.

### 🟠 M-2 — 14 tokens: chart window ends before their last listing event
**Area:** chart coverage · **Evidence:** e.g. `spx` window_end `2025-09-11` < last event
`2026-01-08`; also kite, flock, gwei, irys, morpho, prompt + the M-1 artifact dates.  
**Impact:** those listing markers fall outside the **default** chart view (still visible
on pan + in the events table). For genuine late co-listings this hides real signal.  
**Remediation:** at build time, clamp `window_end_utc = max(window_end, last real event
+ headroom)` — mirroring what `apply_signals` already does for BWEnews events. Exclude
M-1 artifact events from this so the view isn't stretched by a bogus date.

### 🟠 M-3 — 3 tokens have no `gecko_pool` → charts frozen
**Area:** data/charts · **Evidence:** `bard`, `kat`, `sxt` have `gecko_pool: null`;
`refresh_klines` skips them ("no pool/chain"), so their cached charts (kat 25.5k, sxt
28.1k candles) never update.  
**Impact:** three charts silently stop updating; not visible to users but breaks the
"forever-updating" guarantee for them.  
**Remediation:** backfill the GeckoTerminal pool address for each (or mark them
explicitly as CEX-sourced/static so the gap is intentional and documented).

### 🟠 M-4 — Short tickers risk false-positive BWEnews matches
**Area:** signal robustness · **Evidence:** tracked tokens `2Z, B3, G, IP, LA, YB`.
`apply_signals` matches on bare symbol; a 1–2 char ticker can appear incidentally in an
unrelated headline, attaching a wrong venue event to the wrong token.  
**Impact:** potential incorrect auto-added event (would be tagged `source: bwenews`, so
detectable, but still wrong).  
**Remediation:** for symbols ≤2 chars, require a stronger match (token **name** present
in the headline, or the `SYMBOL+USDT/(SYMBOL)` pattern) before applying.

### 🟡 L-1 — 35 charts older than 36h
**Area:** freshness · **Evidence:** two distinct causes — (a) ~20 ETH-network pools
return **404** on GeckoTerminal's recent-candle endpoint (kept cached, see commit log);
(b) tokens whose on-chain Alpha pool has **no recent trades** (migrated to main/CEX
listings) legitimately end at their last real trade.  
**Impact:** mixed — (b) is correct behavior (no trades = no candles); (a) is a
data-source gap. Neither breaks the page.  
**Remediation:** classify the two in the audit (don't lump as "stale"); for (a),
optionally add a CEX-OHLC fallback for ETH pools.

### 🟡 L-2 — Heavy client payload
**Area:** performance · **Evidence:** `plotly.min.js` = 4.7 MB on every detail page;
heaviest page `pump` = 1.9 MB inline candle data; avg page 546 KB.  
**Impact:** slow first load on mobile/poor connections (Plotly is browser-cached after
first hit, so amortized).  
**Remediation:** ship a partial Plotly bundle (`plotly-basic`/finance build, ~1 MB) and
downsample very old tokens' inline 5m series to 15m/1h beyond the launch window.

### 🟡 L-3 — No Content-Security-Policy / X-Content-Type-Options
**Area:** hardening · **Evidence:** live headers lack CSP + `X-Content-Type-Options`
(GitHub Pages doesn't allow custom response headers).  
**Impact:** low — XSS is already mitigated by escaping; this is defense-in-depth.  
**Remediation:** add a `<meta http-equiv="Content-Security-Policy" …>` to the page
`<head>` (works on Pages without server headers).

### 🟡 L-4 — News-strip external link not scheme-validated
**Area:** hardening · **Evidence:** `build_listing_report.py:651` renders
`href="{html.escape(link)}"`; `html.escape` does not neutralize a `javascript:` scheme.  
**Impact:** low — links come from the trusted BWEnews RSS (`t.me/…`).  
**Remediation:** guard with `link.startswith(("http://","https://"))` before rendering.

### 🟡 L-5 — Live build differs from local build
**Area:** parity · **Evidence:** live `report/index` hash ≠ local. **Expected** — CI
rebuilds every ~20 min with fresh "as of" timestamps + news-strip time; not a defect.
Noted for completeness so future parity checks aren't misread.

### 🟡 L-6 — Audit/utility scripts shipped in the public repo
**Area:** hygiene · **Evidence:** `audit_internal.py`, `audit_external.py`,
`probe_listings*.py`, `*.log` (logs are git-ignored). Harmless (no secrets), but the repo
mixes build + research + audit tooling.  
**Remediation:** optional — move audit/research scripts to a `tools/` subfolder for a
cleaner production surface.

---

## 5.1 Remediation applied in this pass (verified)

| Finding | Fix shipped | Verification |
|---|---|---|
| **M-4** short-ticker false match | `_confident_match()` in `apply_signals.py`: symbols ≤2 chars (G, IP, LA…) require the token **name** in the headline before an event is attached | dry-run clean; SLX (3-char) unaffected |
| **L-4** unvalidated link scheme | news strip drops any href not starting `http(s)://` | rebuilt index, t.me links preserved |
| **M-2** late events off default view | `interactive_chart` extends the default x-range to the latest **non-sweep** event | spx view now reaches 2026-01-08; mog reaches its real Coinone 07-23 and ignores the bogus 08-04 sweep |
| **L-3** no CSP | `<meta http-equiv="Content-Security-Policy">` added to every page (`self` + inline for Plotly; blocks external script/exfil) | present on all pages; site renders |

The full pipeline + both audit harnesses were re-run after the fixes — build green, no
regressions.

## 5.2 Remediation applied — pass 2 (verified)

| Finding | Fix shipped | Verification |
|---|---|---|
| **M-1** bogus uniform Bitget/OKX sweep dates | Root-caused: Bitget API retains only ~300 daily candles, so `2025-08-04` is a **data floor, not a listing date**. Sweep-sourced times now render muted "≈ date" with a "data-limited, not verified" tooltip — never as a precise time. Generalizes to all daily-resolution sweeps. | mog Bybit sweep now shows "≈ 2024-10-08" (its real announcement was 2024-06) |
| **M-3** missing `gecko_pool` | bard + sxt backfilled with their top-liquidity pools (now refresh: SXT +41, BARD +13 candles); kat has **0** GeckoTerminal pools → explicitly marked `chart_note: static` so the gap is documented, not silent | refreshed live |
| **L-1** 404'ing ETH pools | Added **Binance-spot OHLC fallback** in `refresh_klines`: when a token's on-chain pool is gone/empty, pull `<SYM>USDT` 5m klines from Binance (capped to recent ~120d to bound cache), merged with the on-chain launch history | MORPHO/ONDO now current (latest 0.0h); ZETA/MOG not on Binance spot → honestly kept cached |
| **L-2** heavy Plotly payload | `interactive_chart` keeps full 5m only in the default view + last 3 days, decimates older history to hourly | ONDO page 110k-candle → **864 KB** (was multi-MB); pump 1.9MB→1.3MB |
| **L-6** mixed script surface | audit + probe scripts moved to `tools/` (imports/paths fixed) | `python tools/audit_internal.py` runs clean |

## 5.3 Residual (by design, not a defect)

- A few tokens (e.g. ZETA, MOG) have **no live price source** — their on-chain pool was
  pruned from GeckoTerminal *and* they aren't on Binance spot under `<SYM>USDT`. These
  keep their cached launch-window charts (graceful), correctly frozen at last real data.
- The internal audit's "window_end < last event" check inspects the **stored JSON**; that
  case is fixed at **render time** (M-2 extends the default view to the latest real
  event), so it is closed in the live product even though the JSON field is unchanged.

---

## 6. Conclusion

ListingLabs passes a hard internal + external audit with **no critical or high-severity
findings**. It is available, secure against the realistic threat (escaped third-party
content, no exposed secrets), and operationally resilient. The open items are
data-quality refinements and front-end polish that will raise it from "working" to
"polished production." Re-run `audit_internal.py` + `audit_external.py` after each batch
of fixes to track closure.
