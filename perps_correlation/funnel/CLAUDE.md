# funnel/ — the Alpha → Binance Perp → Coinbase → Korea study

74-token funnel: which Alpha listings progressed to a Binance perp, a Coinbase
listing, a Korean listing — with timing gaps, FDV-at-listing, and OI.

- **Source of truth:** `funnel_master.json` (built by `master.py` from
  `funnel_dated.json` + enrichments).
- **Report:** `funnel_report.py` → `Listinglabs/funnel/report/` (run by
  `../build_all.py`; don't run builders out of order).
- `fdv_audit.py` / `fdv_audit.json` — the authoritative FDV re-audit: slugs
  resolved by CONTRACT ADDRESS, symbol-verified. The older
  `fdv_final.json`/`fdv_cmc.json` mis-resolved ~50 tokens via ticker/name
  heuristics (e.g. ETHGas $28M → $1.09B) — never trust those without checking
  against fdv_audit.json.
- `fdv_cmc.py` reads `cmc_map.json` from the repo root (verifysheet/).
- Stage scripts (`build_funnel.py`, `stage2_dates.py`, `enrich.py`,
  `analyze*.py`, `fdv2.py`, `to_main.py`, `funnel_chart.py`) were the build
  steps for the study; rarely re-run now.

## The lesson baked into this study

Test **FDV-at-listing** (perp price at listing × total supply), NOT current
FDV — current FDV is dump-biased low and flips conclusions.
