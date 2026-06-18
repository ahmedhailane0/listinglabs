"""LOCAL-ONLY backfill: a REAL 180-day daily price history for every
Manipulated-tab token that lacks one.

Why this exists: the curated watchlist (scam_data.json) already gets a 180d
daily series from refresh_scam_prices (keyed on cg_id), which is what powers the
price-reaction stats (first / all-time high / all-time low / since / peak gain /
max drawdown) and the per-card chart. The ~115 screener-universe coins folded
into the Manipulated tab only carry a ~12-day hourly `spark` from the box — that
is NOT 180 days and NOT all-time, so it cannot honestly fill those stats. This
script pulls a genuine 180d daily series from CoinGecko for any token missing
cache/scam_prices/<SYM>.json:

  - by cg_id when known (curated),
  - else by contract+chain (CoinGecko asset-platform), which the screener
    carries for ~119 coins,
  - else a name/symbol search validated against the known price.

Each fetched series' tail price is sanity-checked against the screener's
price-verified market price; a >3x mismatch (wrong-coin contract/id) is rejected
rather than written, mirroring the project's price-verify discipline. CI can't
do this (CoinGecko rate-limits the shared Actions IP); run it locally and commit
the results.

    python fetch/fetch_scam_history.py            # all tokens missing a series
    python fetch/fetch_scam_history.py ACT AERGO  # only these (force refetch)
    FORCE=1 python fetch/fetch_scam_history.py     # refetch even if cached
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.parse
from pathlib import Path

import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))  # lib./fetch./build. importable
from fetch.fetch_scam_data import _get, CG, PACE, PRICES

HERE = Path(__file__).resolve().parents[1]
CACHE = HERE.parent / "cache"
SCAM_DATA = CACHE / "scam_data.json"
SCREENER = CACHE / "screener" / "screener.json"


def _series_by_cgid(cid: str) -> list:
    d = _get(f"{CG}/coins/{cid}/market_chart?vs_currency=usd&days=180&interval=daily")
    return (d or {}).get("prices") or []


def _series_by_contract(platform: str, addr: str) -> list:
    base = f"{CG}/coins/{platform}/contract/{addr}/market_chart?vs_currency=usd&days=180"
    d = _get(base + "&interval=daily")
    pr = (d or {}).get("prices") or []
    if not pr:  # some platforms reject interval=daily on free tier — auto-gran
        time.sleep(PACE)
        d = _get(base)
        pr = (d or {}).get("prices") or []
    return pr


def _series_by_search(name: str, ref_px, sym: str = "") -> list:
    """No contract/cg_id: resolve via CoinGecko search, validate the candidate's
    180d-tail price against the known price (3x window) before trusting it. Tries
    the symbol as well as the name (the screener name can be verbose, e.g.
    'ACT I : The AI Prophecy', which the search misses)."""
    seen: set[str] = set()
    for query in (q for q in (sym, name) if q):
        s = _get(f"{CG}/search?query={urllib.parse.quote(query)}")
        time.sleep(PACE)
        for c in ((s or {}).get("coins") or [])[:5]:
            cid = c["id"]
            if cid in seen:
                continue
            seen.add(cid)
            pr = _series_by_cgid(cid)
            time.sleep(PACE)
            if pr and _price_ok(pr[-1][1], ref_px):
                return pr
    return []


def _price_ok(last_px, ref_px) -> bool:
    """Tail price within 3x of the price-verified reference (or no reference)."""
    if not ref_px or not last_px:
        return True
    return (1 / 3) <= (last_px / ref_px) <= 3


def _identities() -> dict:
    """sym(upper) -> {cg_id?, chain?, contract?, price?, name} for every
    Manipulated token, curated first (carries cg_id) then screener universe."""
    out: dict[str, dict] = {}
    if SCAM_DATA.exists():
        for rec in json.loads(SCAM_DATA.read_text(encoding="utf-8")).values():
            out[rec["symbol"].upper()] = {
                "cg_id": rec.get("cg_id"), "chain": rec.get("chain"),
                "contract": rec.get("contract"),
                "price": rec.get("price") or rec.get("csv_price"),
                "name": rec.get("name") or rec["symbol"],
            }
    if SCREENER.exists():
        scr = json.loads(SCREENER.read_text(encoding="utf-8")).get("tokens") or {}
        for sym, sr in scr.items():
            key = sym.upper()
            if key in out:
                continue
            m = sr.get("market") or {}
            out[key] = {"cg_id": None, "chain": m.get("chain"),
                        "contract": m.get("contract"), "price": m.get("price"),
                        "name": m.get("name") or sym}
    return out


def fetch_one(sym: str, idy: dict) -> tuple[int, str]:
    """Returns (n_points, how). Writes scam_prices/<SYM>.json on success."""
    ref = idy.get("price")
    series, how = [], ""
    if idy.get("cg_id"):
        series, how = _series_by_cgid(idy["cg_id"]), "cg_id"
        time.sleep(PACE)
    if not series and idy.get("contract") and idy.get("chain"):
        series, how = _series_by_contract(idy["chain"], idy["contract"]), "contract"
        time.sleep(PACE)
    if not series and (idy.get("name") or sym):
        series, how = _series_by_search(idy.get("name") or sym, ref, sym), "search"
    if not series:
        return 0, "no-id"
    if not _price_ok(series[-1][1], ref):
        return 0, f"price-mismatch(last={series[-1][1]:.6g} ref={ref})"
    (PRICES / f"{sym}.json").write_text(json.dumps(series), encoding="utf-8")
    return len(series), how


def main(argv) -> int:
    PRICES.mkdir(parents=True, exist_ok=True)
    force = bool(os.environ.get("FORCE")) or bool(argv)
    ids = _identities()
    want = {a.upper() for a in argv} or set(ids)

    todo = [s for s in ids if s in want and (force or not (PRICES / f"{s}.json").exists())]
    print(f"fetch_scam_history: {len(todo)} token(s) to fetch "
          f"(force={force}, total universe={len(ids)})")
    ok = miss = 0
    for sym in sorted(todo):
        try:
            n, how = fetch_one(sym, ids[sym])
        except Exception as e:
            print(f"  {sym:9} ERROR {e}", flush=True)
            miss += 1
            continue
        if n:
            ok += 1
            print(f"  {sym:9} {n:4d} pts via {how}", flush=True)
        else:
            miss += 1
            print(f"  {sym:9} —    ({how})", flush=True)
    print(f"\ndone: {ok} written, {miss} unresolved -> {PRICES}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
