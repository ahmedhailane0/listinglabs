"""Refresh the Scam Watchlist's volatile market data — keyless, CI-safe.

The full `fetch_scam_data.py` reads a local CSV and resolves identities/funding
(RootData), so it can't run in CI. This is the *price-only subset*: for every
token already in `cache/scam_data.json` (which carries cg_id / cmc_slug / chain /
contract from a prior full run), re-pull from keyless endpoints:

  - CoinGecko `coins/{id}` (current price/MC/FDV/vol + supply) and
    `coins/{id}/market_chart?days=180&interval=daily` (the sparkline/chart series).
  - CoinMarketCap detail (authoritative supply + live MC/FDV/OI), which WINS over
    CoinGecko's supply — that's the supply-accuracy fix.

Supply-derived fields (circulation ratio, peak market cap) are recomputed from the
trusted numbers. A merge-guard never lets a throttled/empty refetch clobber a
value we already have. This is what keeps the watchlist charts from going stale
(they were ~2 days behind because nothing refreshed them in the cron).

    python refresh_scam_prices.py            # refresh all watchlist tokens
    python refresh_scam_prices.py LAB VVV    # only these
"""
from __future__ import annotations

import json
import os
import sys
import time

import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))  # make lib./fetch./build. importable from anywhere
from fetch.fetch_scam_data import _get, CG, PACE, OUT, PRICES
from fetch import fetch_oi_cmc as oimod

# Trusted Binance-sourced price reference: the box's hourly screener spark (1H
# closes straight off Binance) catches CoinGecko aggregator spikes — a glitched
# aggregator print disagrees with the real venue, while a genuine pump shows up
# on BOTH and they agree, so cross-checking can't eat real moves. Reject a
# CoinGecko price/series tail that diverges from this reference by > this factor.
SCREENER = OUT.parent / "screener" / "screener.json"
GUARD_FACTOR = 1.5
_SCREENER_SPARK: dict | None = None


def _binance_ref(sym: str):
    """Most recent Binance-sourced price for `sym` from the box screener spark,
    or None when the token isn't screened / there's no data to cross-check."""
    global _SCREENER_SPARK
    if _SCREENER_SPARK is None:
        try:
            toks = json.loads(SCREENER.read_text(encoding="utf-8")).get("tokens") or {}
        except Exception:
            toks = {}
        _SCREENER_SPARK = {}
        for s, t in toks.items():
            for _ts, v in reversed((t or {}).get("spark") or []):
                if v:
                    _SCREENER_SPARK[s.upper()] = v
                    break
    return _SCREENER_SPARK.get(sym.upper())


def _diverges(a, b) -> bool:
    """True when a and b disagree by more than GUARD_FACTOR (either direction)."""
    if not a or not b:
        return False
    return a / b > GUARD_FACTOR or b / a > GUARD_FACTOR


def refresh_one(rec: dict) -> dict:
    """Re-pull volatile market data for one record (mutated copy returned)."""
    rec = dict(rec)
    sym = rec["symbol"].upper()
    # trusted fallbacks if CoinGecko prints a spike (restored by the guard below)
    prev_px, prev_fdv, prev_mcap = rec.get("price"), rec.get("fdv"), rec.get("mcap")
    ref = _binance_ref(sym)             # Binance ground-truth price, when screened
    cid = rec.get("cg_id")
    if cid:
        d = _get(f"{CG}/coins/{cid}?localization=false&tickers=false&market_data=true"
                 "&community_data=false&developer_data=false")
        time.sleep(PACE)
        md = (d or {}).get("market_data") or {}
        if md:
            rec.update({
                "price": md.get("current_price", {}).get("usd") or rec.get("price"),
                "mcap": md.get("market_cap", {}).get("usd") or rec.get("mcap"),
                "fdv": md.get("fully_diluted_valuation", {}).get("usd") or rec.get("fdv"),
                "vol": md.get("total_volume", {}).get("usd") or rec.get("vol"),
                "circ_supply": md.get("circulating_supply") or rec.get("circ_supply"),
                "total_supply": md.get("total_supply") or rec.get("total_supply"),
                "max_supply": md.get("max_supply") or rec.get("max_supply"),
                "ath_price": (md.get("ath") or {}).get("usd") or rec.get("ath_price"),
            })
            rec["supply_source"] = "coingecko"
            # Reject a CoinGecko spot price that disagrees with the live Binance
            # price by >GUARD_FACTOR (an aggregator glitch, e.g. SIREN 2026-06-19):
            # keep the prior trusted price so a bad print can't reorder the table
            # or fake a +100% move. Derived FDV/MC fall out of the supply math below.
            if ref and _diverges(rec.get("price"), ref):
                print(f"{sym:9} CG price {rec.get('price')} diverges from Binance "
                      f"{ref} (>{GUARD_FACTOR}x) — keeping prior {prev_px}", flush=True)
                # restore the prior trusted price + the FDV/MC it implied; the CG
                # values just written were computed at the spiked price.
                rec["price"], rec["fdv"], rec["mcap"] = prev_px, prev_fdv, prev_mcap
                rec["price_guarded"] = True
        # daily price series for the chart/sparkline (don't clobber good with empty)
        mc = _get(f"{CG}/coins/{cid}/market_chart?vs_currency=usd&days=180&interval=daily")
        time.sleep(PACE)
        prices = (mc or {}).get("prices") or []
        # Trim contaminated trailing samples: CoinGecko tacks a near-real-time point
        # onto the daily series, and that's where the aggregator spike lands. Drop
        # trailing points that diverge from the Binance reference; never touch
        # historical daily closes (only the tail, only when we have a reference).
        if ref:
            dropped = 0
            while len(prices) >= 2 and _diverges(prices[-1][1], ref):
                prices.pop()
                dropped += 1
            if dropped:
                print(f"{sym:9} trimmed {dropped} contaminated trailing price "
                      f"point(s) vs Binance ref {ref}", flush=True)
        if prices or not (PRICES / f"{sym}.json").exists():
            (PRICES / f"{sym}.json").write_text(json.dumps(prices), encoding="utf-8")
            rec["n_prices"] = len(prices)

    # CMC supply (authoritative) + live OI/MC — prefer over CoinGecko's supply
    slug = rec.get("cmc_slug")
    if slug:
        oi = oimod.fetch_one(slug)
        mcm = oimod.fetch_mcap(slug)
        amt = oi.get("oi_usd")
        # CMC mcap preferred; fall back to the CoinGecko mcap refreshed above so a
        # fresh OI never gets paired with a stale ratio.
        mcv = mcm.get("mcap_now_usd") or rec.get("mcap")
        if amt is not None:
            rec["oi_usd"] = amt
        rec["oi_pct_mcap"] = (amt / mcv * 100) if (amt and mcv) else rec.get("oi_pct_mcap")
        if mcm.get("circulating_supply") is not None:
            rec["circ_supply"] = mcm["circulating_supply"]
        if mcm.get("total_supply") is not None:
            rec["total_supply"] = mcm["total_supply"]
        if mcm.get("max_supply") is not None:
            rec["max_supply"] = mcm["max_supply"]
        if mcm.get("circulating_supply") or mcm.get("total_supply"):
            rec["supply_source"] = "coinmarketcap"

    # recompute supply-derived metrics from the trusted numbers
    circ = rec.get("circ_supply")
    denom = rec.get("total_supply") or rec.get("max_supply")
    rec["circ_ratio"] = (circ / denom) if (circ and denom) else rec.get("circ_ratio")
    athp = rec.get("ath_price")
    rec["peak_mcap"] = (athp * circ) if (athp and circ) else rec.get("peak_mcap")
    return rec


def main(argv) -> int:
    if not OUT.exists():
        print(f"{OUT} missing — run fetch_scam_data.py once first")
        return 1
    PRICES.mkdir(parents=True, exist_ok=True)
    data = json.loads(OUT.read_text(encoding="utf-8"))
    want = {a.upper() for a in argv} or None

    # Round-robin so a throttled/timed-out CI run never starves the tail: walk
    # tokens MOST-STALE-FIRST (oldest/absent `refreshed_at` go first) and cap each
    # run at SCAM_LIMIT tokens. The script writes after every token, so a timeout is
    # harmless — the next run simply picks up the tokens that have waited longest.
    # An explicit token list (CLI args) bypasses both the cap and the ordering.
    order = list(data.items())
    if want:
        order = [(s, r) for s, r in order if s.upper() in want]
    else:
        order.sort(key=lambda kv: kv[1].get("refreshed_at") or 0)
        limit = int(os.environ.get("SCAM_LIMIT") or 0)
        if limit > 0:
            order = order[:limit]

    n = 0
    for sym, rec in order:
        try:
            data[sym] = refresh_one(rec)
        except Exception as e:
            print(f"{sym:9} refresh error: {e}", flush=True)
            continue
        r = data[sym]
        r["refreshed_at"] = time.time()   # stamp so the next run rotates past it
        print(f"{sym:9} px={r.get('price')} mc={r.get('mcap')} fdv={r.get('fdv')} "
              f"circ={r.get('circ_supply')} tot={r.get('total_supply')} "
              f"src={r.get('supply_source')}", flush=True)
        n += 1
        OUT.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nrefreshed {n} token(s) -> {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
