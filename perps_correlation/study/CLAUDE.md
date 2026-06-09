# study/ — the original perps study (DONE, frozen)

"Does anything (FDV, prior CEX count, VC funding…) predict how a
Binance-Alpha-listed token's perp performs after launch?" This is finished
work — touch only if re-running the analysis.

- Pipeline: `build_enriched.py` (joins `verifysheet/parsed_rows.json` +
  `Listing - Sheet1.csv` + caches → `enriched.csv`, then auto-runs
  `clean_enriched.py` → `enriched_clean.csv`) → `analysis.ipynb` →
  `make_report.py` (notebook → `PERPS_REPORT.docx`).
- `HOW_TO_RUN.md` — the original run instructions (paths predate the layer
  reorg; scripts now live here in study/).
- `EXPLANATION.txt` — plain-language methodology write-up used by the report.

## Headline finding (also in global memory)

FDV at listing predicts 30d return: ρ = +0.19, p < 0.001 — bigger tokens hold
up BETTER. Treat high FDV-at-listing as a positive prior, not dilution risk.

Inputs that still live at the repo root (scripts reference them there):
`parsed_rows.json`, `Listing - Sheet1.csv`.
