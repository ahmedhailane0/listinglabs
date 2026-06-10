"""Consolidate EVERY piece of token information across all three reports into one
local research archive — the foundation for offline analysis.

LOCAL-ONLY BY DESIGN. The repo is public and CI commits cache/, so this writes to
`verifysheet/research/` instead — the root .gitignore whitelist tracks only
perps_correlation/ + cache/ + .github, so anything in research/ stays on this PC
and is never pushed. Run it locally whenever you want to capture the current state:

    python build_research_archive.py

What it does, for every token that appears in ANY report (Binance Alpha & Perps,
CEX → Korea funnel, Scam Watchlist):
  - Merges identity / market / supply / open-interest / funding / holders / venue
    listing dates + lags / price-reaction metrics into ONE unified record, and
    ALSO keeps each source's raw block verbatim under `raw{}` so nothing is lost.
  - Writes the current state to research/tokens_latest.json (+ a flat
    research/tokens_latest.csv for spreadsheets/pandas).
  - Appends a dated daily snapshot (research/snapshots/<YYYY-MM-DD>.json) and one
    flat row per token per day to research/timeseries.csv — so you can study how
    price / OI / funding / holders / supply evolve over time. Re-running on the
    same day overwrites that day (idempotent).

Outputs:
    research/tokens_latest.json      full unified records (dict keyed by SYMBOL)
    research/tokens_latest.csv       flat current snapshot, one row per token
    research/snapshots/<date>.json   full daily snapshot (accumulates)
    research/timeseries.csv          flat longitudinal table (accumulates)
"""
from __future__ import annotations

import csv
import datetime as dt
import glob
import json
import os
import sys
from pathlib import Path

import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))  # make lib./fetch./build. importable from anywhere
from lib import metrics

HERE = Path(__file__).resolve().parents[1]   # perps_correlation/ (project root, NOT this subfolder)
ROOT = HERE.parent                      # verifysheet/  (repo root)
# Path/date overrides let backfill_missing_days.py reconstruct a PAST day: it points
# CACHE/LISTINGS at a historical cache (extracted from that day's git commit) and
# sets ARCHIVE_ASOF to the day being rebuilt, while OUT still points at the real
# research/ so the recovered rows land in the canonical CSVs.
CACHE = Path(os.environ.get("ARCHIVE_CACHE") or (ROOT / "cache"))
OUT = Path(os.environ.get("ARCHIVE_OUT") or (ROOT / "research"))   # LOCAL-ONLY (git-ignored)
LISTINGS = Path(os.environ.get("ARCHIVE_LISTINGS") or (HERE / "listings"))
ASOF = os.environ.get("ARCHIVE_ASOF") or ""   # "YYYY-MM-DD" => backfill mode for that day

ALLOWED_PERP_VENUES = {"Binance", "OKX", "Bybit", "KuCoin", "Bitget", "Gate"}


def _load_json(p: Path, default=None):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return default


def _jsonable(o):
    """JSON default: stringify datetimes (metrics.reaction returns them)."""
    if isinstance(o, (dt.datetime, dt.date)):
        return o.isoformat()
    return str(o)


def _rec(records: dict, sym: str) -> dict:
    sym = (sym or "").upper()
    r = records.get(sym)
    if r is None:
        r = {"symbol": sym, "name": None, "reports": [], "identity": {}, "market": {},
             "supply": {}, "listings": {"venue_dates": {}, "lags_days": {}, "events_raw": [],
             "not_listed": []}, "open_interest": {}, "funding_round": {}, "holders": {},
             "metrics": {}, "memo": None, "raw": {}}
        records[sym] = r
    return r


def _add_report(r, name):
    if name not in r["reports"]:
        r["reports"].append(name)


def _set_if_empty(d: dict, key, val):
    if val not in (None, "") and d.get(key) in (None, ""):
        d[key] = val


# ── source ingestion ─────────────────────────────────────────────────────────

def ingest_reactions(records):
    """listings/*.json + metrics.reaction (price metrics from klines)."""
    for lp in sorted(LISTINGS.glob("*.json")):
        cfg = _load_json(Path(lp), {})
        sym = (cfg.get("token") or Path(lp).stem).upper()
        r = _rec(records, sym)
        _add_report(r, "binance_alpha_perps")
        _set_if_empty(r, "name", cfg.get("name"))
        idn = r["identity"]
        for k_src, k_dst in [("chain", "chain"), ("token_contract", "contract"),
                             ("cmc_slug", "cmc_slug"), ("gecko_pool", "gecko_pool"),
                             ("category", "category")]:
            _set_if_empty(idn, k_dst, cfg.get(k_src))
        _set_if_empty(r["market"], "fdv", cfg.get("fdv_usd"))
        _set_if_empty(r["market"], "mcap", cfg.get("mcap_usd"))
        _set_if_empty(r["supply"], "circulating", cfg.get("circulating_supply"))
        _set_if_empty(r["supply"], "total", cfg.get("total_supply"))
        # venue listing events (verbatim + earliest-per-venue map)
        evs = cfg.get("events") or []
        if evs:
            r["listings"]["events_raw"] = evs
            for e in evs:
                ex, t = e.get("exchange"), e.get("iso_time_utc")
                if ex and t:
                    cur = r["listings"]["venue_dates"].get(ex)
                    if cur is None or t < cur:
                        r["listings"]["venue_dates"][ex] = t
        if cfg.get("not_listed"):
            r["listings"]["not_listed"] = cfg["not_listed"]
        m = None
        try:
            m = metrics.reaction(cfg)
        except Exception:
            m = None
        if m:
            r["metrics"].update(json.loads(json.dumps(m, default=_jsonable)))
        r["raw"]["reactions"] = cfg


def ingest_funnel(records):
    fm = _load_json(HERE / "funnel" / "funnel_master.json", []) or []
    for rec in fm:
        sym = (rec.get("symbol") or "").upper()
        if not sym:
            continue
        r = _rec(records, sym)
        _add_report(r, "cex_to_korea")
        _set_if_empty(r, "name", rec.get("name"))
        idn = r["identity"]
        for k in ("chain", "contract", "cmc_slug", "gecko_pool"):
            _set_if_empty(idn, k, rec.get(k))
        _set_if_empty(r["market"], "fdv", rec.get("fdv"))
        _set_if_empty(r["market"], "fdv_at_listing", rec.get("at_listing_fdv"))
        _set_if_empty(r["market"], "mcap", rec.get("mcap"))
        _set_if_empty(r["supply"], "circulating", rec.get("circ"))
        _set_if_empty(r["supply"], "total", rec.get("total"))
        _set_if_empty(r["supply"], "max", rec.get("max_supply"))
        vd = r["listings"]["venue_dates"]
        for src, key in [("alpha_date", "Binance Alpha"), ("perp_date", "Binance Perp"),
                         ("coinbase_date", "Coinbase"), ("upbit_date", "Upbit"),
                         ("bithumb_date", "Bithumb"), ("coinone_date", "Coinone"),
                         ("first_korean", "First Korean")]:
            if rec.get(src):
                vd.setdefault(key, rec[src])
        r["listings"]["lags_days"] = {k: rec[k] for k in rec if k.startswith("days_")}
        r["raw"]["funnel"] = rec


def ingest_scams(records):
    sd = _load_json(CACHE / "scam_data.json", {}) or {}
    for sym, rec in sd.items():
        sym = sym.upper()
        r = _rec(records, sym)
        _add_report(r, "scam_watchlist")
        _set_if_empty(r, "name", rec.get("name"))
        idn = r["identity"]
        for k_src, k_dst in [("chain", "chain"), ("contract", "contract"),
                             ("cmc_slug", "cmc_slug"), ("cg_id", "cg_id"),
                             ("website", "website"), ("twitter", "twitter")]:
            _set_if_empty(idn, k_dst, rec.get(k_src))
        mk = r["market"]
        # scams holds the freshest current market data — let it win
        for k_src, k_dst in [("price", "price"), ("mcap", "mcap"), ("fdv", "fdv"),
                             ("vol", "vol24h")]:
            if rec.get(k_src) is not None:
                mk[k_dst] = rec[k_src]
        sup = r["supply"]
        for k_src, k_dst in [("circ_supply", "circulating"), ("total_supply", "total"),
                             ("max_supply", "max"), ("circ_ratio", "circ_ratio"),
                             ("peak_mcap", "peak_mcap"), ("supply_source", "source")]:
            if rec.get(k_src) is not None:
                sup[k_dst] = rec[k_src]
        if rec.get("memo_en"):
            r["memo"] = rec["memo_en"]
        if rec.get("funding"):
            r["funding_round"] = rec["funding"]
        r["raw"]["scams"] = rec

        # per-exchange perp OI + funding (allowlist-filtered, total recomputed)
        perp = _load_json(CACHE / "perp_markets" / f"{sym}.json")
        if perp and perp.get("venues"):
            vs = [v for v in perp["venues"] if v.get("venue") in ALLOWED_PERP_VENUES]
            total = sum(v["oi_usd"] for v in vs) or 0.0
            vol_total = sum(v["vol24h_usd"] for v in vs if v.get("vol24h_usd")) or 0.0
            r["open_interest"].update(
                tracked_total_usd=total,
                pct_mcap=(total / mk["mcap"] * 100) if (total and mk.get("mcap")) else None,
                tracked_vol24h_usd=vol_total or None,
                oi_vol_ratio=(total / vol_total) if (total and vol_total) else None,
                venues=[{"venue": v["venue"], "oi_usd": v["oi_usd"],
                         "share_pct": (v["oi_usd"] / total * 100) if total else None,
                         "funding": v.get("funding"), "interval_h": v.get("interval_h"),
                         "funding_annualized": v.get("funding_annualized"),
                         "vol24h_usd": v.get("vol24h_usd"),
                         "oi_vol_ratio": v.get("oi_vol_ratio")} for v in vs],
                fetched_at=perp.get("fetched_at"))
        # CMC all-venue OI (the inflated whole-market figure — kept for context)
        if rec.get("oi_usd") is not None:
            r["open_interest"]["cmc_all_venue_usd"] = rec["oi_usd"]

        # top holders
        h = _load_json(CACHE / "scam_holders" / f"{sym}.json")
        if h and h.get("available"):
            r["holders"] = {k: h.get(k) for k in
                            ("source", "holder_count", "top10_share", "retail_share", "holders")}

        # OI/funding history series (local-only embed; cheap)
        hist = _load_json(CACHE / "perp_history" / f"{sym}.json")
        if hist:
            r["open_interest"]["history"] = hist


def ingest_funding(records):
    """Shared RootData/CryptoRank funding for tokens that didn't get it from scams."""
    fd = _load_json(CACHE / "funding.json", {}) or {}
    for sym, f in fd.items():
        sym = sym.upper()
        if sym in records and not records[sym].get("funding_round"):
            records[sym]["funding_round"] = f


def ingest_oi_cmc(records):
    """CMC current OI snapshot for the reactions/funnel tokens (keyed by token)."""
    oi = (_load_json(HERE / "oi_cmc.json", {}) or {}).get("tokens", {})
    for sym, o in oi.items():
        sym = sym.upper()
        if sym in records:
            records[sym]["raw"]["oi_cmc"] = o
            records[sym]["open_interest"].setdefault("cmc_all_venue_usd", o.get("oi_usd"))


# ── per-tab record files ─────────────────────────────────────────────────────
# Each tab keeps its OWN record folder under research/<tab>/, documenting only the
# tokens shown on that tab, with columns tailored to that tab. No single mixed
# time-series file.

TABS = {
    "binance_alpha_perps": "Binance Alpha & Perps",
    "cex_to_korea": "CEX → Korea",
    "scam_watchlist": "Scam Watchlist",
}

# Tab-specific flat CSV columns (what that tab actually documents).
TAB_COLS = {
    "binance_alpha_perps": [
        "date", "symbol", "name", "chain", "cmc_slug", "fdv", "mcap",
        "circ_supply", "total_supply", "change_pct", "ath_px", "atl_px",
        "max_drawdown_pct", "peak_gain_pct", "cmc_oi_usd",
        "binance_alpha_date", "binance_perp_date", "coinbase_date", "first_korean_date"],
    "cex_to_korea": [
        "date", "symbol", "name", "chain", "cmc_slug", "fdv", "fdv_at_listing", "mcap",
        "circ_supply", "total_supply", "days_alpha_to_perp", "days_alpha_to_coinbase",
        "days_alpha_to_korean", "days_coinbase_to_korean", "days_perp_to_korean",
        "binance_alpha_date", "binance_perp_date", "coinbase_date", "first_korean_date",
        "on_upbit", "on_bithumb", "on_coinone"],
    "scam_watchlist": [
        "date", "symbol", "name", "chain", "cmc_slug", "price", "mcap", "fdv", "vol24h",
        "circ_supply", "total_supply", "max_supply", "circ_ratio",
        "oi_tracked_usd", "oi_pct_mcap", "perp_vol24h_usd", "oi_vol_ratio",
        "cmc_all_venue_oi",
        "funding_amount", "holder_count", "top10_share", "retail_share"],
}


def _row(tab, date_str, r) -> dict:
    """Flat per-tab row for one token, picking the fields that tab documents."""
    vd = r["listings"]["venue_dates"]
    oi, sup, mk, m = r["open_interest"], r["supply"], r["market"], r["metrics"]
    base = {"date": date_str, "symbol": r["symbol"], "name": r.get("name"),
            "chain": r["identity"].get("chain"), "cmc_slug": r["identity"].get("cmc_slug")}
    if tab == "binance_alpha_perps":
        base.update(
            fdv=mk.get("fdv"), mcap=mk.get("mcap"),
            circ_supply=sup.get("circulating"), total_supply=sup.get("total"),
            change_pct=m.get("change_pct"), ath_px=m.get("ath_px"), atl_px=m.get("atl_px"),
            max_drawdown_pct=m.get("max_drawdown_pct"), peak_gain_pct=m.get("peak_gain_pct"),
            cmc_oi_usd=oi.get("cmc_all_venue_usd"),
            binance_alpha_date=vd.get("Binance Alpha"), binance_perp_date=vd.get("Binance Perp"),
            coinbase_date=vd.get("Coinbase") or vd.get("Coinbase Spot"),
            first_korean_date=vd.get("First Korean"))
    elif tab == "cex_to_korea":
        fn = r["raw"].get("funnel", {}) or {}
        base.update(
            fdv=mk.get("fdv"), fdv_at_listing=mk.get("fdv_at_listing"), mcap=mk.get("mcap"),
            circ_supply=sup.get("circulating"), total_supply=sup.get("total"),
            days_alpha_to_perp=fn.get("days_alpha_to_perp"),
            days_alpha_to_coinbase=fn.get("days_alpha_to_coinbase"),
            days_alpha_to_korean=fn.get("days_alpha_to_korean"),
            days_coinbase_to_korean=fn.get("days_coinbase_to_korean"),
            days_perp_to_korean=fn.get("days_perp_to_korean"),
            binance_alpha_date=vd.get("Binance Alpha"), binance_perp_date=vd.get("Binance Perp"),
            coinbase_date=vd.get("Coinbase"), first_korean_date=vd.get("First Korean"),
            on_upbit=fn.get("on_upbit"), on_bithumb=fn.get("on_bithumb"),
            on_coinone=fn.get("on_coinone"))
    else:  # scam_watchlist
        fr, h = (r["funding_round"] or {}), (r["holders"] or {})
        base.update(
            price=mk.get("price"), mcap=mk.get("mcap"), fdv=mk.get("fdv"), vol24h=mk.get("vol24h"),
            circ_supply=sup.get("circulating"), total_supply=sup.get("total"),
            max_supply=sup.get("max"), circ_ratio=sup.get("circ_ratio"),
            oi_tracked_usd=oi.get("tracked_total_usd"), oi_pct_mcap=oi.get("pct_mcap"),
            perp_vol24h_usd=oi.get("tracked_vol24h_usd"), oi_vol_ratio=oi.get("oi_vol_ratio"),
            cmc_all_venue_oi=oi.get("cmc_all_venue_usd"),
            funding_amount=fr.get("amount"), holder_count=h.get("holder_count"),
            top10_share=h.get("top10_share"), retail_share=h.get("retail_share"))
    return base


def _write_csv(path, cols, rows):
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def _append_daily_csv(path, date_str, cols, rows):
    """Append a day's rows to a per-tab accumulating CSV; re-running for a date
    replaces that date (idempotent), so each token gets one row per day. Kept sorted
    by (date, symbol) so a backfilled past day lands in chronological order, not at
    the end."""
    existing = []
    if path.exists():
        with path.open(encoding="utf-8", newline="") as f:
            existing = [row for row in csv.DictReader(f) if row.get("date") != date_str]
    merged = existing + rows
    merged.sort(key=lambda r: (r.get("date") or "", r.get("symbol") or ""))
    _write_csv(path, cols, merged)
    return len(merged)


def _cleanup_legacy():
    """Remove the old single-archive outputs replaced by the per-tab layout."""
    for name in ("tokens_latest.json", "tokens_latest.csv", "timeseries.csv"):
        (OUT / name).unlink(missing_ok=True)
    snaps = OUT / "snapshots"
    if snaps.is_dir():
        for p in snaps.glob("*.json"):
            p.unlink(missing_ok=True)
        try:
            snaps.rmdir()
        except OSError:
            pass


README = """# Token research archive (LOCAL-ONLY)

Built by `perps_correlation/build_research_archive.py`. This folder is **not**
tracked by git (the .gitignore whitelist tracks only perps_correlation/, cache/,
.github/), so it stays on this PC and is never published.

**Each tab has its own record folder** documenting only that tab's tokens:

- `binance_alpha_perps/`  — the "Binance Alpha & Perps" tab
- `cex_to_korea/`         — the "CEX → Korea" funnel tab
- `scam_watchlist/`       — the "Scam Watchlist" tab

Inside each:
- `latest.json` — full unified record per token (current state); keeps every source's
  raw block under `raw{}` so nothing is dropped.
- `latest.csv`  — flat current snapshot, one row per token (columns tailored to the tab).
- `daily/<YYYY-MM-DD>.json` — full daily snapshots (accumulate every build).
- `daily.csv`   — flat accumulating log, one row per token **per day** (load with
  `pd.read_csv('daily.csv', parse_dates=['date'])`).

Re-run `python build_research_archive.py` to record today; same-day re-runs overwrite
the day (idempotent). A token shown on multiple tabs is recorded in each tab's folder.
"""


def main():
    # Windows' default console is cp1252 and chokes on the "→" in a tab label
    # (UnicodeEncodeError), which previously crashed the run AFTER two tabs were
    # written but BEFORE the third — leaving scam_watchlist unrecorded. Force UTF-8
    # (or at least never let a print kill the build).
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    records: dict = {}
    ingest_reactions(records)
    ingest_funnel(records)
    ingest_scams(records)
    ingest_funding(records)
    ingest_oi_cmc(records)

    now = dt.datetime.now(dt.timezone.utc)
    backfill = bool(ASOF)               # rebuilding a past day, not "now"
    date_str = ASOF if backfill else now.strftime("%Y-%m-%d")
    stamp = (f"{date_str}T00:00:00Z" if backfill
             else now.strftime("%Y-%m-%dT%H:%M:%SZ"))
    for r in records.values():
        r["captured_at"] = stamp

    OUT.mkdir(parents=True, exist_ok=True)
    if not backfill:
        _cleanup_legacy()
        (OUT / "README.md").write_text(README, encoding="utf-8")

    mode = f"BACKFILL {date_str}" if backfill else f"captured {date_str}"
    print(f"research archive -> {OUT}  ({mode})")
    for tab, label in TABS.items():
        recs = {sym: r for sym, r in records.items() if tab in r["reports"]}
        if not recs:
            continue
        d = OUT / tab
        (d / "daily").mkdir(parents=True, exist_ok=True)
        blob = json.dumps(recs, indent=2, ensure_ascii=False, default=_jsonable)
        (d / "daily" / f"{date_str}.json").write_text(blob, encoding="utf-8")
        cols = TAB_COLS[tab]
        rows = [_row(tab, date_str, r) for r in recs.values()]
        # latest.* = the CURRENT snapshot; a recovered past day must never overwrite it.
        if not backfill:
            (d / "latest.json").write_text(blob, encoding="utf-8")
            _write_csv(d / "latest.csv", cols, rows)
        total = _append_daily_csv(d / "daily.csv", date_str, cols, rows)
        print(f"  {label:22} {len(recs):3} tokens -> {tab}/ "
              f"(daily/{date_str}.json, daily.csv {total} rows)")


if __name__ == "__main__":
    main()
