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
from pathlib import Path

import metrics

HERE = Path(__file__).parent
ROOT = HERE.parent                      # verifysheet/  (repo root)
CACHE = ROOT / "cache"
OUT = ROOT / "research"                 # LOCAL-ONLY (git-ignored by the whitelist)
LISTINGS = HERE / "listings"

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
            r["open_interest"].update(
                tracked_total_usd=total,
                pct_mcap=(total / mk["mcap"] * 100) if (total and mk.get("mcap")) else None,
                venues=[{"venue": v["venue"], "oi_usd": v["oi_usd"],
                         "share_pct": (v["oi_usd"] / total * 100) if total else None,
                         "funding": v.get("funding"), "interval_h": v.get("interval_h"),
                         "funding_annualized": v.get("funding_annualized")} for v in vs],
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


# ── flat CSV projection ──────────────────────────────────────────────────────

FLAT_COLS = ["date", "symbol", "name", "reports", "chain", "cmc_slug", "price", "mcap",
             "fdv", "fdv_at_listing", "vol24h", "circ_supply", "total_supply", "max_supply",
             "circ_ratio", "oi_tracked_usd", "oi_pct_mcap", "cmc_all_venue_oi",
             "funding_amount", "holder_count", "top10_share", "retail_share",
             "binance_alpha_date", "binance_perp_date", "coinbase_date", "first_korean_date"]


def _flat_row(date_str, r) -> dict:
    vd = r["listings"]["venue_dates"]
    oi = r["open_interest"]
    return {
        "date": date_str, "symbol": r["symbol"], "name": r.get("name"),
        "reports": "|".join(r["reports"]), "chain": r["identity"].get("chain"),
        "cmc_slug": r["identity"].get("cmc_slug"),
        "price": r["market"].get("price"), "mcap": r["market"].get("mcap"),
        "fdv": r["market"].get("fdv"), "fdv_at_listing": r["market"].get("fdv_at_listing"),
        "vol24h": r["market"].get("vol24h"),
        "circ_supply": r["supply"].get("circulating"), "total_supply": r["supply"].get("total"),
        "max_supply": r["supply"].get("max"), "circ_ratio": r["supply"].get("circ_ratio"),
        "oi_tracked_usd": oi.get("tracked_total_usd"), "oi_pct_mcap": oi.get("pct_mcap"),
        "cmc_all_venue_oi": oi.get("cmc_all_venue_usd"),
        "funding_amount": (r["funding_round"] or {}).get("amount"),
        "holder_count": (r["holders"] or {}).get("holder_count"),
        "top10_share": (r["holders"] or {}).get("top10_share"),
        "retail_share": (r["holders"] or {}).get("retail_share"),
        "binance_alpha_date": vd.get("Binance Alpha"), "binance_perp_date": vd.get("Binance Perp"),
        "coinbase_date": vd.get("Coinbase"), "first_korean_date": vd.get("First Korean"),
    }


def _append_timeseries(date_str, records):
    """One flat row per token per day; re-running the same day replaces that day."""
    path = OUT / "timeseries.csv"
    existing = []
    if path.exists():
        with path.open(encoding="utf-8", newline="") as f:
            existing = [row for row in csv.DictReader(f) if row.get("date") != date_str]
    rows = existing + [_flat_row(date_str, r) for r in records.values()]
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FLAT_COLS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    return len(rows)


README = """# Token research archive (LOCAL-ONLY)

Built by `perps_correlation/build_research_archive.py`. This folder is **not**
tracked by git (the repo's .gitignore whitelist only tracks perps_correlation/,
cache/, .github/), so it stays on this PC and is never published.

- `tokens_latest.json` — full unified record per token (current state). Each record
  merges identity / market / supply / open-interest / funding / holders / venue
  listing dates + lags / price metrics, and keeps every source's raw block under
  `raw{}` so nothing is dropped.
- `tokens_latest.csv` — flat current snapshot, one row per token (for spreadsheets).
- `snapshots/<YYYY-MM-DD>.json` — full daily snapshots (accumulate over time).
- `timeseries.csv` — flat longitudinal table, one row per token per day. Load with
  pandas: `pd.read_csv('timeseries.csv', parse_dates=['date'])`.

Re-run `python build_research_archive.py` to capture today; same-day re-runs
overwrite the day (idempotent).
"""


def main():
    records: dict = {}
    ingest_reactions(records)
    ingest_funnel(records)
    ingest_scams(records)
    ingest_funding(records)
    ingest_oi_cmc(records)

    now = dt.datetime.now(dt.timezone.utc)
    date_str = now.strftime("%Y-%m-%d")
    stamp = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    for r in records.values():
        r["captured_at"] = stamp

    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "snapshots").mkdir(exist_ok=True)
    (OUT / "README.md").write_text(README, encoding="utf-8")

    blob = json.dumps(records, indent=2, ensure_ascii=False, default=_jsonable)
    (OUT / "tokens_latest.json").write_text(blob, encoding="utf-8")
    (OUT / "snapshots" / f"{date_str}.json").write_text(blob, encoding="utf-8")

    # flat current CSV
    with (OUT / "tokens_latest.csv").open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FLAT_COLS, extrasaction="ignore")
        w.writeheader()
        for r in records.values():
            w.writerow(_flat_row(date_str, r))
    ts_rows = _append_timeseries(date_str, records)

    by_report = {}
    for r in records.values():
        for rep in r["reports"]:
            by_report[rep] = by_report.get(rep, 0) + 1
    print(f"research archive -> {OUT}")
    print(f"  {len(records)} unique tokens  ({by_report})")
    print(f"  tokens_latest.json/.csv + snapshots/{date_str}.json + timeseries.csv "
          f"({ts_rows} rows total)")


if __name__ == "__main__":
    main()
