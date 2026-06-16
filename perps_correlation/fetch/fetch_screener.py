"""Hourly perp-screener fetcher for the manipulated-coin Screener tab.

RUNS ON THE ALWAYS-ON BOX, not GitHub CI: Binance's futures API (fapi.binance.com)
returns 451 to datacenter IPs, so CI can't fetch this. For every screenable coin
in cache/manip_watchlist.json (has_perp) it pulls from Binance:

  * hourly open-interest history  (futures/data/openInterestHist, period=1h)
  * 1H klines                     (fapi/v1/klines, interval=1h)
  * funding history + interval    (fapi/v1/fundingRate, fapi/v1/fundingInfo)

plus current Bybit OI (one bulk call) for the page-level (BN+BYB OI)/FDV gate and
CMC FDV (cached, slow-moving). It runs the Buy v1/v2/v3 engine (lib.signals) and
writes ONE compact file the site builds from: cache/screener/screener.json.

Stateless on history: openInterestHist returns ~real hourly OI each call, so the
signals are correct from the very first run (no multi-day warm-up).

    python fetch/fetch_screener.py             # all screenable coins
    python fetch/fetch_screener.py SIREN GUA   # just these (debug)
"""
from __future__ import annotations

import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))  # make lib./fetch. importable
from fetch.fetch_perp_markets import _get, _f, _bulk_bybit, _binance_intervals
from fetch.fetch_token_market import fetch_one as cmc_fetch
from lib import signals
from lib.signals import evaluate

HERE = Path(__file__).resolve().parents[1]   # perps_correlation/ (project root, NOT this subfolder)
CACHE = HERE.parent / "cache"
WATCHLIST = CACHE / "manip_watchlist.json"
OUTDIR = CACHE / "screener"
OUT = OUTDIR / "screener.json"
FDV_CACHE = OUTDIR / "fdv.json"

FAPI = "https://fapi.binance.com"
OI_LIMIT = 300        # hours of OI history (~12.5d) — covers the 72h scan + EMA60 warmup
KLINE_LIMIT = 300     # hours of 1H klines
FUND_LIMIT = 20       # funding settlements (~6d at 8h)
GATE = 0.08           # page-level: (Binance OI + Bybit OI) / FDV >= 8%
FDV_TTL_S = 6 * 3600  # refresh a coin's FDV at most every 6h
FDV_MAX_PER_RUN = 25  # cap CMC calls per run (it throttles)
WORKERS = 6


def _now() -> int:
    return int(time.time())


def _oi_hist(sym: str) -> list[tuple[int, float]]:
    """Hourly (ts_sec, OI_usd) from Binance, ascending. Empty on failure."""
    d = _get(f"{FAPI}/futures/data/openInterestHist?symbol={sym}USDT&period=1h&limit={OI_LIMIT}")
    out = []
    for p in d or []:
        v = _f(p.get("sumOpenInterestValue"))
        t = p.get("timestamp")
        if v is not None and t is not None:
            out.append((int(t) // 1000, v))
    return sorted(out)


def _klines_1h(sym: str) -> list[tuple]:
    """Hourly (open_ts_sec, o, h, l, c, vol) — CLOSED candles only, ascending."""
    d = _get(f"{FAPI}/fapi/v1/klines?symbol={sym}USDT&interval=1h&limit={KLINE_LIMIT}")
    now_ms = _now() * 1000
    out = []
    for k in d or []:
        # k = [openTime, o, h, l, c, vol, closeTime, ...]
        if len(k) < 7 or k[6] >= now_ms:      # drop the still-forming candle
            continue
        out.append((int(k[0]) // 1000, _f(k[1]), _f(k[2]), _f(k[3]), _f(k[4]), _f(k[5])))
    return out


def _funding(sym: str) -> list[tuple[int, float]]:
    """(ts_sec, rate) funding settlements, ascending."""
    d = _get(f"{FAPI}/fapi/v1/fundingRate?symbol={sym}USDT&limit={FUND_LIMIT}")
    out = []
    for x in d or []:
        r, t = _f(x.get("fundingRate")), x.get("fundingTime")
        if r is not None and t is not None:
            out.append((int(t) // 1000, r))
    return sorted(out)


def _load_json(p: Path, default):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return default


def _fdv_for(slug: str, cache: dict) -> tuple[float | None, float | None, bool]:
    """Cached FDV/mcap for a CMC slug. Returns (fdv, mcap, did_fetch)."""
    if not slug:
        return None, None, False
    rec = cache.get(slug)
    if rec and (_now() - rec.get("ts", 0)) < FDV_TTL_S:
        return rec.get("fdv"), rec.get("mcap"), False
    res = cmc_fetch(slug)
    fdv, mcap = res.get("fdv_usd"), res.get("mcap_usd")
    if "error" not in res:
        cache[slug] = {"fdv": fdv, "mcap": mcap, "ts": _now()}
    return fdv, mcap, True


def _fetch_token(sym: str, intervals: dict) -> dict:
    """Per-coin Binance pull + signal compute (FDV/gate filled in by the caller)."""
    oi = _oi_hist(sym)
    kl = _klines_1h(sym)
    fund = _funding(sym)
    interval_h = intervals.get(f"{sym}USDT", 8) or 8
    cur_funding = fund[-1][1] if fund else None
    sig = evaluate(oi, kl, funding=fund, current_funding=cur_funding,
                   funding_interval_h=float(interval_h))
    oi_bn = oi[-1][1] if oi else None
    ok = bool(oi and kl)
    return {"oi_bn": oi_bn, "funding": cur_funding, "funding_interval_h": float(interval_h),
            "signals": sig, "as_of": sig.get("as_of"), "data": "ok" if ok else "partial"}


def main(argv: list[str]) -> int:
    OUTDIR.mkdir(parents=True, exist_ok=True)
    wl = _load_json(WATCHLIST, {})
    if not wl:
        print(f"{WATCHLIST} missing/empty — run tools/seed_manip_watchlist.py first")
        return 1
    only = {a.upper() for a in argv}
    screenable = [s for s, r in wl.items() if r.get("has_perp") and (not only or s in only)]
    screenable.sort()

    # Shared one-shot pulls.
    intervals = {}
    try:
        intervals = _binance_intervals()
    except Exception:
        pass
    try:
        byb = _bulk_bybit()                       # {base: {oi_usd, ...}}
    except Exception:
        byb = {}
    fdv_cache = _load_json(FDV_CACHE, {})

    # Per-coin Binance pull + signals (concurrent).
    recs: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futs = {ex.submit(_fetch_token, s, intervals): s for s in screenable}
        for fut, s in futs.items():
            try:
                recs[s] = fut.result()
            except Exception as e:
                recs[s] = {"data": "error", "error": f"{type(e).__name__}: {e}",
                           "signals": evaluate([], []), "oi_bn": None}

    # FDV (cached/slow) + Bybit OI + the page-level gate.
    fdv_fetches = 0
    for s in screenable:
        rec = recs[s]
        slug = wl[s].get("cmc_slug")
        do_fetch = fdv_fetches < FDV_MAX_PER_RUN
        fdv, mcap, fetched = (_fdv_for(slug, fdv_cache) if do_fetch
                              else (fdv_cache.get(slug, {}).get("fdv"),
                                    fdv_cache.get(slug, {}).get("mcap"), False))
        fdv_fetches += 1 if fetched else 0
        oi_byb = (byb.get(s) or {}).get("oi_usd")
        oi_bn = rec.get("oi_bn")
        oi_comb = (oi_bn or 0) + (oi_byb or 0) if (oi_bn or oi_byb) else None
        rec.update({
            "oi_byb": oi_byb, "oi_combined": oi_comb, "fdv": fdv, "mcap": mcap,
            "oi_fdv_pct": (oi_comb / fdv * 100) if (oi_comb and fdv) else None,
            "pass_gate": bool(oi_comb and fdv and (oi_comb / fdv) >= GATE),
            "sections": wl[s].get("sections", []), "sources": wl[s].get("sources", []),
        })

    # Carry the not-screenable coins through so the page can show them.
    for s, r in wl.items():
        if not r.get("has_perp") and (not only or s in only):
            recs[s] = {"data": "no_perp", "sections": r.get("sections", []),
                       "sources": r.get("sources", []), "signals": evaluate([], [])}

    def _fired(strat):
        return sum(1 for r in recs.values()
                   if (r.get("signals") or {}).get(strat, {}).get("fired"))

    as_of = max((r.get("as_of") for r in recs.values() if r.get("as_of")), default=None)
    payload = {
        "generated_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "as_of_hour": as_of,
        "gate": GATE,
        "thresholds": {k: getattr(signals, k) for k in dir(signals)
                       if k.isupper() and isinstance(getattr(signals, k), (int, float))},
        "counts": {
            "universe": len(recs), "screenable": len(screenable),
            "passing_gate": sum(1 for r in recs.values() if r.get("pass_gate")),
            "v1": _fired("v1"), "v2": _fired("v2"), "v3": _fired("v3"),
        },
        "tokens": recs,
    }
    OUT.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
    FDV_CACHE.write_text(json.dumps(fdv_cache, separators=(",", ":")), encoding="utf-8")
    c = payload["counts"]
    print(f"wrote {OUT}")
    print(f"  screenable={c['screenable']}  gate-pass={c['passing_gate']}  "
          f"v1={c['v1']} v2={c['v2']} v3={c['v3']}  (FDV fetched {fdv_fetches})")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
