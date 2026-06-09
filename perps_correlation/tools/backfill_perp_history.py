"""Backfill the per-token perp OI + funding HISTORY so the Scam Watchlist's
"OI & funding over time" chart shows the trailing days immediately — not just the
points the cron accumulates going forward.

Why a separate, LOCAL script (mirrors refresh_klines's gap-fill split):
  - The exchange history endpoints (Binance/Bybit) geo-block GitHub's runner IP
    (451), so they can't run in CI. They DO work from a normal local IP.
  - CoinGecko's /derivatives aggregator (the CI path) has no history — it's a
    live snapshot only.
So: run this LOCALLY once (and occasionally), commit cache/perp_history/, and the
CI's fetch_perp_markets keeps appending live points on top forever.

Coverage (kept honest in the chart subtitle): we sum the two largest venues that
expose keyless daily history — **Binance** (OI already in USD via openInterestHist)
and **Bybit** (OI in coins, valued at Binance's daily close). Daily funding is the
OI-weighted mean of the two. That tracks the multi-day TREND closely; the live
snapshot above the chart remains the full all-venue total.

Output merges into cache/perp_history/<SYM>.json as daily points tagged
{"src":"backfill"}; live points (no src / "live") are preserved. Re-running
replaces only the backfill points.

    python backfill_perp_history.py            # all watchlist tokens
    python backfill_perp_history.py LAB VVV    # only these
"""
from __future__ import annotations

import json
import sys
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]   # perps_correlation/ (project root, NOT this subfolder)
CACHE = HERE.parent / "cache"
HIST = CACHE / "perp_history"
SCAM = CACHE / "scam_data.json"
UA = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
DAY_MS = 86_400_000


def _get(url, tries=3):
    for i in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=20) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception:
            if i == tries - 1:
                return None
            time.sleep(0.6)
    return None


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _day(ms: int) -> int:
    """Floor a ms timestamp to its UTC midnight (the daily bucket key)."""
    return (int(ms) // DAY_MS) * DAY_MS


def _binance_daily_close(sym: str) -> dict[int, float]:
    """UTC-day -> close price, from Binance daily futures klines (for valuing
    coin-denominated OI from other venues)."""
    kl = _get(f"https://fapi.binance.com/fapi/v1/klines?symbol={sym}USDT&interval=1d&limit=400") or []
    return {_day(k[0]): _f(k[4]) for k in kl if _f(k[4])}


def _binance_oi_usd(sym: str) -> dict[int, float]:
    """UTC-day -> Binance perp OI in USD (sumOpenInterestValue is already USD)."""
    oi = _get(f"https://fapi.binance.com/futures/data/openInterestHist?symbol={sym}USDT&period=1d&limit=30") or []
    return {_day(r["timestamp"]): _f(r.get("sumOpenInterestValue"))
            for r in oi if _f(r.get("sumOpenInterestValue"))}


def _binance_funding_daily(sym: str) -> dict[int, float]:
    """UTC-day -> mean Binance funding rate that day (per-interval rate)."""
    fr = _get(f"https://fapi.binance.com/fapi/v1/fundingRate?symbol={sym}USDT&limit=1000") or []
    return _avg_by_day((r.get("fundingTime"), r.get("fundingRate")) for r in fr)


def _bybit_oi_coins(sym: str) -> dict[int, float]:
    """UTC-day -> Bybit perp OI in COINS (needs valuing by a daily price)."""
    d = _get(f"https://api.bybit.com/v5/market/open-interest?category=linear&symbol={sym}USDT&intervalTime=1d&limit=200")
    lst = ((d or {}).get("result") or {}).get("list") or []
    return {_day(r["timestamp"]): _f(r.get("openInterest"))
            for r in lst if _f(r.get("openInterest"))}


def _bybit_funding_daily(sym: str) -> dict[int, float]:
    d = _get(f"https://api.bybit.com/v5/market/funding/history?category=linear&symbol={sym}USDT&limit=200")
    lst = ((d or {}).get("result") or {}).get("list") or []
    return _avg_by_day((r.get("fundingRateTimestamp"), r.get("fundingRate")) for r in lst)


def _avg_by_day(pairs) -> dict[int, float]:
    """(ts_ms, rate) pairs -> UTC-day -> mean rate."""
    acc: dict[int, list] = {}
    for ts, rate in pairs:
        r = _f(rate)
        if ts is None or r is None:
            continue
        acc.setdefault(_day(int(ts)), []).append(r)
    return {d: sum(v) / len(v) for d, v in acc.items()}


def build_series(sym: str) -> list[dict]:
    """Daily [{t, total_oi_usd, funding_avg, src:'backfill'}] for one token,
    summing Binance + Bybit OI (Bybit coins valued at Binance daily close) and
    OI-weighting their daily funding."""
    sym = sym.upper()
    price = _binance_daily_close(sym)
    bn_oi = _binance_oi_usd(sym)
    bn_fund = _binance_funding_daily(sym)
    by_oi_coins = _bybit_oi_coins(sym)
    by_fund = _bybit_funding_daily(sym)
    by_oi_usd = {d: c * price[d] for d, c in by_oi_coins.items() if price.get(d)}

    # Anchor the window on whichever venue gives a CONSISTENT OI floor: Binance's
    # USD OI history (~30d) if the token trades there, else Bybit-only. Mixing
    # "Binance+Bybit" recent days with "Bybit-only" older days would draw a fake
    # step where Binance drops out of its 30d window, so we don't span both.
    days = sorted(bn_oi) if bn_oi else sorted(by_oi_usd)
    out = []
    for d in days:
        oi_bn, oi_by = bn_oi.get(d), by_oi_usd.get(d)
        total = (oi_bn or 0) + (oi_by or 0)
        if total <= 0:
            continue
        # OI-weighted daily funding across the venues that report both that day
        num = den = 0.0
        for oi_v, fund_v in ((oi_bn, bn_fund.get(d)), (oi_by, by_fund.get(d))):
            if oi_v and fund_v is not None:
                num += oi_v * fund_v
                den += oi_v
        funding = (num / den) if den else None
        out.append({"t": d // 1000, "total_oi_usd": round(total, 2),
                    "funding_avg": funding, "src": "backfill"})
    return out


def merge(sym: str, backfill: list[dict]) -> int:
    """Merge backfill points into cache/perp_history/<SYM>.json: drop prior
    backfill points, keep live ones, re-add fresh backfill, sort by t. Returns the
    final point count."""
    HIST.mkdir(parents=True, exist_ok=True)
    p = HIST / f"{sym.upper()}.json"
    existing = []
    if p.exists():
        try:
            existing = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            existing = []
    live = [pt for pt in existing if pt.get("src") != "backfill"]
    # de-dup: if a live point falls on a day we backfilled, keep the live one
    bf_days = {pt["t"] // 86400 for pt in backfill}
    live_days = {pt["t"] // 86400 for pt in live}
    merged = [pt for pt in backfill if pt["t"] // 86400 not in live_days] + live
    merged.sort(key=lambda pt: pt["t"])
    p.write_text(json.dumps(merged), encoding="utf-8")
    return len(merged)


def main(argv) -> int:
    data = json.loads(SCAM.read_text(encoding="utf-8")) if SCAM.exists() else {}
    want = {a.upper() for a in argv} or None
    syms = [s for s in (data.keys() if data else [])] or list(want or [])
    if want:
        syms = [s for s in syms if s.upper() in want] or list(want)
    n = 0
    for sym in syms:
        try:
            series = build_series(sym)
        except Exception as e:
            print(f"{sym:9} backfill error: {e}", flush=True)
            continue
        if not series:
            print(f"{sym:9} no perp history (not on Binance/Bybit?)", flush=True)
            continue
        total = merge(sym, series)
        import datetime as dt
        d0 = dt.datetime.utcfromtimestamp(series[0]["t"]).date()
        d1 = dt.datetime.utcfromtimestamp(series[-1]["t"]).date()
        print(f"{sym:9} {len(series)} daily pts {d0}..{d1}  (file now {total} pts)", flush=True)
        n += 1
        time.sleep(0.3)
    print(f"\nbackfilled {n} token(s) -> {HIST}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
