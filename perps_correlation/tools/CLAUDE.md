# tools/ — maintenance one-offs (none run in the CI cron)

Run as `python tools/<name>.py` from `perps_correlation/`. Each is safe to
re-run; nothing here is part of the build.

- `add_alpha_token.py` — scaffold a new `listings/<token>.json` (pulls CMC
  detail for FDV/supply/slug). After it: seed candles locally, then push.
- `backfill_missing_days.py` — fills research/ CSV gaps for days the PC was
  off, reconstructing each day's cache from the cloud's git history and running
  build_research_archive with `ARCHIVE_ASOF`. Wired into
  `run_research_archive.cmd` (daily Windows task). Idempotent.
- `backfill_perp_history.py` — LOCAL-ONLY seeder for the trailing ~30d of
  `cache/perp_history/` (Binance/Bybit history endpoints 451 the CI IP).
  Anchors Binance USD OI + Bybit OI, OI-weighted funding. Commit the result.
- `clean_klines.py` — repair a contaminated kline cache (wrong-pool candles):
  trims bad rows, refetches from Binance spot where possible.
- `audit_listings.py` — re-probe venues for listed/not-listed mismatches
  against the sweep caches (read-only report).
- `audit_internal.py` / `audit_external.py` — structured audit dumps of config
  consistency / external data agreement (read-only).
- `reactions_fdv_audit.py` — checks listings FDV values against the funnel's
  contract-verified re-audit (`funnel/fdv_audit.json`); writes its log here.
- `probe_listings.py` / `probe_listings2.py` — ad-hoc venue listing probes
  (predecessors of sweep_venues).
- `nex_listing_chart.py` — the original hand-rolled NEX reaction chart
  (superseded by `lib/listing_chart.py` + listings configs). Kept with its
  `listing_events.json` for reference.
