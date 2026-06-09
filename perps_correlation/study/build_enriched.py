"""Join Listing CSV + all caches into enriched.csv for correlation analysis.

Inputs (in cache/ unless noted):
  ../Listing - Sheet1.csv     source rows (Project, Binance perp, Binance spot, OKX perp/spot, Coinbase spot, FDV)
  ../parsed_rows.json         cleaned name + ticker per row
  binance_perp_klines.json    { symbol -> [klines] }
  binance_funding.json        { symbol -> [funding] }
  perp_symbol_map.json        { ticker -> symbol }
  bithumb_dates.json          { ticker -> "YYYY-MM-DD" }
  upbit_listings.json         [ { ticker, date, is_listing, ... } ]
  cryptorank.json             { ticker -> {slug, coin, investors, ...} }
  rootdata.json               { ticker -> {project_id, item, ...} }

Output: enriched.csv with the schema documented in the planning file.
"""
import csv
import json
from datetime import datetime, timedelta
from pathlib import Path
from statistics import mean

HERE = Path(__file__).parent             # perps_correlation/
ROOT = HERE.parent.parent                # verifysheet/
CACHE = ROOT / "cache"
OUT = HERE / "enriched.csv"


def load_json(p, default=None):
    if not Path(p).exists():
        return default
    return json.loads(Path(p).read_text(encoding="utf-8"))


def parse_date(s):
    if not s or s in ("—", "-", ""):
        return None
    s = s.replace("Listed", "").strip()
    try:
        return datetime.strptime(s[:10], "%Y-%m-%d").date()
    except Exception:
        return None


def parse_fdv(s):
    if not s or s in ("—", "-", ""):
        return None
    s = s.replace("$", "").replace(",", "").strip()
    mult = 1
    if s.endswith("B"):
        mult, s = 1_000_000_000, s[:-1]
    elif s.endswith("M"):
        mult, s = 1_000_000, s[:-1]
    elif s.endswith("K"):
        mult, s = 1_000, s[:-1]
    try:
        return float(s) * mult
    except Exception:
        return None


def build_upbit_first_listed():
    """ticker -> earliest date across all upbit notices (proxy for listing date)."""
    out = {}
    for entry in load_json(CACHE / "upbit_listings.json", []) or []:
        t = (entry.get("ticker") or "").upper()
        d = entry.get("date")
        if not t or not d:
            continue
        if t not in out or d < out[t]:
            out[t] = d
    return out


def compute_kline_metrics(klines, funding):
    """klines: list of [openTime, open, high, low, close, volume, closeTime, quoteVolume, ...]"""
    if not klines or len(klines) < 2:
        return {}
    open0 = float(klines[0][1])
    if open0 <= 0:
        return {}
    closes = [float(k[4]) for k in klines]
    quote_vols = [float(k[7]) for k in klines]  # USD-quoted volume

    def ret_at(day):
        if day >= len(closes):
            return None
        return (closes[day] - open0) / open0

    out = {
        "ret_1d": ret_at(0),  # close of day 0 vs open of day 0
        "ret_7d": ret_at(6),
        "ret_30d": ret_at(29),
        "max_drawdown_30d": None,
    }
    # max drawdown over first 30 days
    window = closes[:30]
    if len(window) > 1:
        peak = window[0]
        dd = 0.0
        for c in window:
            peak = max(peak, c)
            dd = min(dd, (c - peak) / peak)
        out["max_drawdown_30d"] = dd
    out["vol_d1_usd"] = quote_vols[0] if quote_vols else None
    out["vol_avg_d2_7_usd"] = mean(quote_vols[1:7]) if len(quote_vols) >= 7 else None
    out["vol_avg_d8_30_usd"] = mean(quote_vols[7:30]) if len(quote_vols) >= 30 else None
    if out["vol_d1_usd"] and out["vol_avg_d8_30_usd"]:
        out["vol_decay_ratio"] = out["vol_avg_d8_30_usd"] / out["vol_d1_usd"]
    else:
        out["vol_decay_ratio"] = None
    if funding:
        rates = [float(f["fundingRate"]) for f in funding if f.get("fundingRate") is not None]
        out["avg_funding_rate_30d"] = mean(rates) if rates else None
    else:
        out["avg_funding_rate_30d"] = None
    return out


def cryptorank_features(entry):
    out = {
        "cr_slug": None, "cr_ico_raised_usd": None, "cr_has_funding_rounds": None,
        "cr_investor_count": None, "cr_tier1_count": None, "cr_lead_count": None,
        "cr_fdv": None,
    }
    if not entry:
        return out
    out["cr_slug"] = entry.get("slug")
    coin = entry.get("coin") or {}
    out["cr_has_funding_rounds"] = coin.get("hasFundingRounds")
    out["cr_fdv"] = coin.get("fullyDilutedMarketCap")
    ico = (coin.get("icoData") or {}).get("raised") or {}
    out["cr_ico_raised_usd"] = ico.get("USD")
    investors = entry.get("investors") or {}
    all_inv, tier1, lead = 0, 0, 0
    for tier_key, items in investors.items():
        if not isinstance(items, list):
            continue
        all_inv += len(items)
        if tier_key == "tier1":
            tier1 = len(items)
        for it in items:
            if it.get("isLead"):
                lead += 1
    out["cr_investor_count"] = all_inv
    out["cr_tier1_count"] = tier1
    out["cr_lead_count"] = lead
    return out


def rootdata_features(entry):
    out = {
        "rd_project_id": None, "rd_total_funding_usd": None, "rd_investor_count": None,
        "rd_lead_count": None, "rd_rt_score": None, "rd_establishment_date": None,
    }
    if not entry:
        return out
    out["rd_project_id"] = entry.get("project_id")
    item = entry.get("item") or {}
    out["rd_total_funding_usd"] = item.get("total_funding")
    out["rd_rt_score"] = item.get("rt_score")
    out["rd_establishment_date"] = item.get("establishment_date")
    invs = item.get("investors") or []
    out["rd_investor_count"] = len(invs)
    out["rd_lead_count"] = sum(1 for i in invs if i.get("lead_investor"))
    return out


def funding_comparison(cr_usd, rd_usd):
    if cr_usd and rd_usd:
        lo, hi = sorted([cr_usd, rd_usd])
        delta = (hi - lo) / hi
        match = "agree" if delta < 0.2 else "disagree"
        consensus = max(cr_usd, rd_usd)
    elif cr_usd:
        match, consensus = "cr_only", cr_usd
    elif rd_usd:
        match, consensus = "rd_only", rd_usd
    else:
        match, consensus = "none", None
    return match, consensus


def main():
    parsed = load_json(ROOT / "parsed_rows.json", [])
    klines = load_json(CACHE / "binance_perp_klines.json", {}) or {}
    funding = load_json(CACHE / "binance_funding.json", {}) or {}
    sym_map = load_json(CACHE / "perp_symbol_map.json", {}) or {}
    bithumb = load_json(CACHE / "bithumb_dates.json", {}) or {}
    upbit = build_upbit_first_listed()
    cryptorank = load_json(CACHE / "cryptorank.json", {}) or {}
    rootdata = load_json(CACHE / "rootdata.json", {}) or {}

    fieldnames = [
        "project", "ticker", "binance_perp_date", "perp_symbol", "fdv_at_listing",
        "n_exchanges_before", "n_exchanges_within_7d", "days_since_first_listing",
        "had_binance_spot", "had_okx_perp", "had_okx_spot", "had_coinbase_spot",
        "had_bithumb", "had_upbit",
        "cr_slug", "cr_ico_raised_usd", "cr_has_funding_rounds",
        "cr_investor_count", "cr_tier1_count", "cr_lead_count", "cr_fdv",
        "rd_project_id", "rd_total_funding_usd", "rd_investor_count",
        "rd_lead_count", "rd_rt_score", "rd_establishment_date",
        "funding_match", "funding_consensus_usd",
        "ret_1d", "ret_7d", "ret_30d", "max_drawdown_30d",
        "vol_d1_usd", "vol_avg_d2_7_usd", "vol_avg_d8_30_usd", "vol_decay_ratio",
        "avg_funding_rate_30d",
    ]

    written = 0
    with OUT.open("w", encoding="utf-8", newline="") as fp:
        w = csv.DictWriter(fp, fieldnames=fieldnames)
        w.writeheader()
        for row in parsed:
            bp_date = parse_date(row.get("binance_perp"))
            if not bp_date:
                continue
            ticker = (row.get("symbol") or "").upper()

            # exchange footprint
            other_dates = {
                "binance_spot": parse_date(row.get("binance_spot")),
                "okx_perp": parse_date(row.get("okx_perp")),
                "okx_spot": parse_date(row.get("okx_spot")),
                "coinbase_spot": parse_date(row.get("coinbase_spot")),
                "bithumb": parse_date(bithumb.get(ticker)),
                "upbit": parse_date(upbit.get(ticker)),
            }
            n_before = sum(1 for d in other_dates.values() if d and d < bp_date)
            n_within_7d = sum(1 for d in other_dates.values() if d and abs((d - bp_date).days) <= 7)
            present_dates = [d for d in other_dates.values() if d]
            days_since_first = (bp_date - min(present_dates)).days if present_dates else None
            had = {f"had_{k}": (v is not None and v <= bp_date + timedelta(days=7)) for k, v in other_dates.items()}

            # perf
            symbol = sym_map.get(ticker)
            metrics = compute_kline_metrics(klines.get(symbol, []), funding.get(symbol, [])) if symbol else {}

            cr = cryptorank_features(cryptorank.get(ticker))
            rd = rootdata_features(rootdata.get(ticker))
            match, consensus = funding_comparison(cr["cr_ico_raised_usd"], rd["rd_total_funding_usd"])

            out = {
                "project": row.get("name"),
                "ticker": ticker,
                "binance_perp_date": bp_date.isoformat(),
                "perp_symbol": symbol,
                "fdv_at_listing": parse_fdv(row.get("fdv")),
                "n_exchanges_before": n_before,
                "n_exchanges_within_7d": n_within_7d,
                "days_since_first_listing": days_since_first,
                **had,
                **cr, **rd,
                "funding_match": match,
                "funding_consensus_usd": consensus,
                **{k: metrics.get(k) for k in (
                    "ret_1d", "ret_7d", "ret_30d", "max_drawdown_30d",
                    "vol_d1_usd", "vol_avg_d2_7_usd", "vol_avg_d8_30_usd",
                    "vol_decay_ratio", "avg_funding_rate_30d",
                )},
            }
            w.writerow(out)
            written += 1
    print(f"wrote {written} rows -> {OUT}")
    # Chain straight into cleaning so enriched.csv is just an intermediate.
    import subprocess, sys
    subprocess.run([sys.executable, str(HERE / "clean_enriched.py")], check=True)
    try:
        OUT.unlink()
        print(f"removed intermediate {OUT.name}")
    except OSError:
        pass


if __name__ == "__main__":
    main()
