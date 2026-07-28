#!/usr/bin/env python3
"""LOCAL-ONLY: full-depth, gap-checked extraction of the Buy-signal inputs.

The hourly screener (`fetch/fetch_screener.py`) pulls only a shallow window
(OI_LIMIT=300 ≈ 12.5d) and PERSISTS NOTHING raw — `screener.json` keeps just the
fired verdict, dropping the OI series and the per-condition metrics. That makes a
signal impossible to audit after the fact, and it caps any longer-window metric.

This script fixes both, from a non-datacenter IP (Binance 451s CI, not your PC):
for every screenable watchlist coin it pulls the DEEPEST continuous hourly series
Binance will serve —

  * hourly open interest  (futures/data/openInterestHist, period=1h)  — paged
    back to Binance's ~30-day retention wall for this endpoint;
  * hourly klines         (fapi/v1/klines, interval=1h)               — paged to
    cover the same (or deeper) window;
  * funding history       (fapi/v1/fundingRate);

aligns OI + klines onto a strict hourly grid, REPORTS every missing hour, and (a
real fix, not a paper-over) refetches klines to fill kline gaps. OI has no keyless
backfill, so OI gaps are reported, never invented. Output:

  cache/screener/raw/<SYM>.json      — {oi:[[t,usd]],
                                        klines:[[t,o,h,l,c,v,
                                                 quoteVol,nTrades,
                                                 takerBuyBase,takerBuyQuote]],
                                        funding:[[t,rate]], meta:{...}}
     The four order-flow fields are APPENDED (since 2026-07-27) and are absent
     from caches written before that, so readers must length-check rather than
     assume 10 — lib.signals._maps already indexes 0-5 with a guard.
  cache/screener/extract_report.json — per-coin span / point-count / gap summary

Run:  python tools/extract_signal_data.py [SYM ...]   (no args = all screenable)
Env:  EXTRACT_DAYS (default 30)  EXTRACT_WORKERS (default 8)
"""
from __future__ import annotations
import json
import os
import sys
import time
import urllib.request
import urllib.error
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]            # perps_correlation/
CACHE = HERE.parent / "cache"
WATCHLIST = CACHE / "manip_watchlist.json"
OUTDIR = CACHE / "screener" / "raw"
REPORT = CACHE / "screener" / "extract_report.json"
FAPI = "https://fapi.binance.com"
HOUR = 3600

DAYS = int(os.environ.get("EXTRACT_DAYS", "30"))
WORKERS = int(os.environ.get("EXTRACT_WORKERS", "8"))
UA = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}


def _get(url: str, tries: int = 4):
    """GET JSON with simple backoff; returns None on persistent failure."""
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            last = e
            # 429/418 = rate limited -> back off harder; 400 = bad/too-old -> stop
            if e.code in (418, 429):
                time.sleep(1.5 * (i + 1))
            elif e.code == 400:
                return None
            else:
                time.sleep(0.5 * (i + 1))
        except Exception as e:
            last = e
            time.sleep(0.5 * (i + 1))
    return None


def _now() -> int:
    return int(time.time())


def _hour_floor(t: int) -> int:
    return (t // HOUR) * HOUR


# ── per-series fetchers (all paged back to `start_ms`) ───────────────────────

def fetch_oi(tkr: str, start_ms: int) -> list[tuple[int, float]]:
    """Hourly OI (USD) paged back to start_ms. openInterestHist caps at limit=500
    and only retains ~30 days; we page by endTime until empty or past start."""
    out: dict[int, float] = {}
    end_ms = _now() * 1000
    for _ in range(12):                                  # safety cap on pages
        url = (f"{FAPI}/futures/data/openInterestHist?symbol={tkr}&period=1h"
               f"&limit=500&endTime={end_ms}")
        d = _get(url)
        if not d:
            break
        for p in d:
            t = int(p["timestamp"]) // 1000
            v = p.get("sumOpenInterestValue")
            if v is not None:
                out[_hour_floor(t)] = float(v)
        oldest = min(int(p["timestamp"]) for p in d)
        if oldest <= start_ms or len(d) < 500:
            break
        end_ms = oldest - 1
    return sorted(out.items())


def fetch_klines(tkr: str, start_ms: int) -> list[tuple]:
    """Hourly OHLCV paged back to start_ms (klines allow limit=1500).
    Drops the still-FORMING candle (closeTime in the future) — same guard as
    fetch_screener._klines_1h. Without it every raw series ended on a mid-hour
    snapshot with partial close/volume (2026-07-09 audit F-05)."""
    out: dict[int, tuple] = {}
    now_ms0 = _now() * 1000
    end_ms = now_ms0
    for _ in range(8):
        url = (f"{FAPI}/fapi/v1/klines?symbol={tkr}&interval=1h"
               f"&limit=1500&endTime={end_ms}")
        d = _get(url)
        if not d:
            break
        for k in d:
            if len(k) > 6 and int(k[6]) >= now_ms0:
                continue                       # forming candle — not settled yet
            t = _hour_floor(int(k[0]) // 1000)
            # Fields 7-10 (quote volume, trade count, taker-BUY base + quote) ride
            # along in the SAME response and were being thrown away. They are the
            # only order-flow this pipeline can get keylessly: trade count is a
            # crowd counter, quoteVol/numTrades is average trade size (whale vs
            # crowd), takerBuyQuote/quoteVol is the aggressive-buy share.
            # Appended, never inserted — every existing reader indexes 0-5 and
            # must keep working against caches written before this change.
            row = [t, float(k[1]), float(k[2]), float(k[3]), float(k[4]), float(k[5])]
            if len(k) > 10:
                row += [float(k[7]), float(k[8]), float(k[9]), float(k[10])]
            out[t] = tuple(row)
        oldest = min(int(k[0]) for k in d)
        if oldest <= start_ms or len(d) < 1500:
            break
        end_ms = oldest - 1
    return [out[t] for t in sorted(out)]


def fetch_funding(tkr: str, start_ms: int) -> list[tuple[int, float]]:
    out: dict[int, float] = {}
    end_ms = _now() * 1000
    for _ in range(6):
        url = (f"{FAPI}/fapi/v1/fundingRate?symbol={tkr}"
               f"&limit=1000&endTime={end_ms}")
        d = _get(url)
        if not d:
            break
        for x in d:
            out[int(x["fundingTime"]) // 1000] = float(x["fundingRate"])
        oldest = min(int(x["fundingTime"]) for x in d)
        if oldest <= start_ms or len(d) < 1000:
            break
        end_ms = oldest - 1
    return sorted(out.items())


# ── gap analysis on a strict hourly grid ─────────────────────────────────────

def grid_gaps(times: list[int]) -> dict:
    """Given ascending hour-floored timestamps, find missing hours in [min,max]."""
    if not times:
        return {"points": 0, "span_h": 0, "expected": 0, "missing": 0,
                "max_gap_h": 0, "gaps": []}
    lo, hi = times[0], times[-1]
    have = set(times)
    expected = (hi - lo) // HOUR + 1
    missing = [t for t in range(lo, hi + 1, HOUR) if t not in have]
    # collapse consecutive missing hours into ranges
    ranges = []
    for t in missing:
        if ranges and t == ranges[-1][1] + HOUR:
            ranges[-1][1] = t
        else:
            ranges.append([t, t])
    max_gap = max(((b - a) // HOUR + 1 for a, b in ranges), default=0)
    return {"points": len(times), "span_h": (hi - lo) // HOUR + 1,
            "span_d": round((hi - lo) / 86400, 1), "expected": expected,
            "missing": len(missing), "max_gap_h": max_gap,
            "gaps": [[datetime.utcfromtimestamp(a).strftime("%m-%d %H:%M"),
                      (b - a) // HOUR + 1] for a, b in ranges[:10]]}


def fill_kline_gaps(tkr: str, kl: list[tuple], gaps: dict) -> list[tuple]:
    """Klines should never be missing — refetch any reported gap window and merge."""
    if not gaps["missing"]:
        return kl
    by_t = {k[0]: k for k in kl}
    lo = kl[0][0]
    d = _get(f"{FAPI}/fapi/v1/klines?symbol={tkr}&interval=1h&limit=1500&startTime={lo*1000}")
    for k in (d or []):
        t = _hour_floor(int(k[0]) // 1000)
        by_t.setdefault(t, (t, float(k[1]), float(k[2]), float(k[3]),
                            float(k[4]), float(k[5])))
    return [by_t[t] for t in sorted(by_t)]


def resolve_ticker(sym: str, rec: dict) -> str:
    return ((rec.get("perp_sym") or sym) + "USDT").upper()


def extract_one(sym: str, rec: dict) -> dict:
    tkr = resolve_ticker(sym, rec)
    start_ms = (_now() - DAYS * 86400) * 1000
    oi = fetch_oi(tkr, start_ms)
    kl = fetch_klines(tkr, start_ms)
    fund = fetch_funding(tkr, start_ms)

    oi_gaps = grid_gaps([t for t, _ in oi])
    kl_gaps = grid_gaps([k[0] for k in kl])
    if kl_gaps["missing"]:
        kl = fill_kline_gaps(tkr, kl, kl_gaps)
        kl_gaps = grid_gaps([k[0] for k in kl])

    OUTDIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "symbol": sym, "ticker": tkr,
        "fetched_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "oi": [[t, round(v, 2)] for t, v in oi],
        # rows are [t,o,h,l,c,v] and, since 2026-07-27, + [quoteVol, nTrades,
        # takerBuyBase, takerBuyQuote]. Written as list(k) so the flow tail is
        # preserved when present and simply absent on older cached rows —
        # consumers must length-check, never assume 10.
        "klines": [list(k) for k in kl],
        "funding": [[t, r] for t, r in fund],
        "meta": {"oi": oi_gaps, "klines": kl_gaps, "funding_pts": len(fund)},
    }
    (OUTDIR / f"{sym}.json").write_text(
        json.dumps(payload, separators=(",", ":")), encoding="utf-8")
    return {"symbol": sym, "ticker": tkr, "oi": oi_gaps, "klines": kl_gaps,
            "funding_pts": len(fund)}


def main(argv: list[str]) -> int:
    wl = json.loads(WATCHLIST.read_text(encoding="utf-8"))
    want = [s.upper() for s in argv] if argv else None
    coins = [(s, r) for s, r in wl.items()
             if r.get("has_perp") and (want is None or s.upper() in want)]
    print(f"extracting {len(coins)} coins · target depth {DAYS}d · {WORKERS} workers")

    results = []
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futs = {ex.submit(extract_one, s, r): s for s, r in coins}
        for i, fut in enumerate(futs, 1):
            s = futs[fut]
            try:
                results.append(fut.result())
            except Exception as e:
                print(f"  ! {s}: {type(e).__name__}: {e}")
            if i % 20 == 0:
                print(f"  ...{i}/{len(coins)}")

    # ── summary ──────────────────────────────────────────────────────────────
    results.sort(key=lambda r: r["symbol"])
    clean = [r for r in results if r["oi"]["missing"] == 0 and r["klines"]["missing"] == 0]
    oi_gappy = [r for r in results if r["oi"]["missing"] > 0]
    kl_gappy = [r for r in results if r["klines"]["missing"] > 0]
    short48 = [r for r in results if r["oi"]["span_h"] < 49]   # < 48h of OI -> v4 can't run

    REPORT.write_text(json.dumps({
        "generated_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "days_target": DAYS, "n": len(results), "clean": len(clean),
        "results": results}, indent=2), encoding="utf-8")

    spans = sorted(r["oi"]["span_d"] for r in results if r["oi"]["points"])
    med = spans[len(spans) // 2] if spans else 0
    print("\n=== EXTRACTION SUMMARY ===")
    print(f"coins extracted     : {len(results)}")
    print(f"OI span days        : min={min(spans) if spans else 0} "
          f"median={med} max={max(spans) if spans else 0}")
    print(f"fully gap-free      : {len(clean)}/{len(results)}")
    print(f"OI gaps remaining   : {len(oi_gappy)}  (no keyless backfill exists)")
    print(f"kline gaps remaining: {len(kl_gappy)}  (should be 0 after refill)")
    print(f"< 48h OI (v4 N/A)   : {len(short48)}  {[r['symbol'] for r in short48]}")
    if oi_gappy:
        print("\nOI-gappy coins (sym: missing_hours, max_gap_h, first gaps):")
        for r in oi_gappy[:25]:
            g = r["oi"]
            print(f"  {r['symbol']:10} missing={g['missing']:3d} max_gap={g['max_gap_h']:3d}h  {g['gaps'][:3]}")
    print(f"\nraw series -> {OUTDIR}\nreport     -> {REPORT}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
