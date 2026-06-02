"""Backfill missing CEX listing dates across all reaction-report tokens.

For every `listings/*.json`, probe the venues that the report filter exposes
but barely has data for — OKX, Bybit, Kraken, KuCoin, Bitget, Gate — on both
spot and perp/futures, and record each venue's earliest candle as its listing
time.

Collision guard (short tickers like G / IP / PEPE share symbols with unrelated
assets): the search is floored at the token's *earliest known listing event*
minus a small buffer, so we never pick up an ancient candle from a different
coin that happened to trade under the same ticker years earlier.

Resolution is daily (one cheap call spanning the whole window); good enough for
"did it list and roughly when". Results are cached in
`cache/venue_sweep.json` so reruns are incremental.

    python sweep_venues.py            # sweep all tokens (incremental)
    python sweep_venues.py aero ctr   # only these
    python sweep_venues.py --force    # ignore cache, refetch everything

Then `python merge_sweep.py` folds the cache into listings/*.json, and
`python build_listing_report.py` rebuilds the site.
"""
from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests

UA = {"User-Agent": "Mozilla/5.0 verifysheet/sweep"}
HERE = Path(__file__).parent
LISTINGS = HERE / "listings"
CACHE = HERE.parent / "cache" / "venue_sweep.json"
NOW = datetime.now(timezone.utc)
FLOOR_BUFFER = timedelta(days=30)   # allow listings slightly before known cluster

DAY_MS = 86_400_000


def ms(dt: datetime) -> int: return int(dt.timestamp() * 1000)
def s(dt: datetime) -> int: return int(dt.timestamp())


# Each fetcher returns the earliest candle open time (ms) at or after `start`,
# trying common contract-symbol variants, or None if the symbol is absent.
# Daily resolution. `variants` lets perps cover 1000x-multiplied tickers.

def _spot_variants(sym): return [sym]
def _perp_variants(sym): return [sym, f"1000{sym}", f"10000{sym}"]


def bybit(sym, start, end, category):
    for v in (_perp_variants(sym) if category == "linear" else _spot_variants(sym)):
        try:
            r = requests.get("https://api.bybit.com/v5/market/kline",
                             params={"category": category, "symbol": f"{v}USDT", "interval": "D",
                                     "start": ms(start), "end": ms(end), "limit": 1000},
                             headers=UA, timeout=20)
            if r.status_code != 200: continue
            d = r.json().get("result", {}).get("list") or []
            if d: return min(int(row[0]) for row in d)
        except Exception: continue
    return None


def kucoin_spot(sym, start, end):
    try:
        r = requests.get("https://api.kucoin.com/api/v1/market/candles",
                         params={"symbol": f"{sym}-USDT", "type": "1day",
                                 "startAt": s(start), "endAt": s(end)},
                         headers=UA, timeout=20)
        if r.status_code != 200: return None
        d = r.json().get("data") or []
        if d: return min(int(row[0]) for row in d) * 1000
    except Exception: pass
    return None


def kucoin_futures(sym, start, end):
    for v in _perp_variants(sym):
        try:
            r = requests.get("https://api-futures.kucoin.com/api/v1/kline/query",
                             params={"symbol": f"{v}USDTM", "granularity": 1440,
                                     "from": ms(start), "to": ms(end)},
                             headers=UA, timeout=20)
            if r.status_code != 200: continue
            d = r.json().get("data") or []
            if d: return min(int(row[0]) for row in d)
        except Exception: continue
    return None


def kraken_spot(sym, start, end):
    try:
        r = requests.get("https://api.kraken.com/0/public/OHLC",
                         params={"pair": f"{sym}USD", "interval": 1440, "since": s(start)},
                         headers=UA, timeout=20)
        if r.status_code != 200: return None
        result = r.json().get("result") or {}
        rows = next((v for k, v in result.items() if k != "last"), None)
        if not rows: return None
        in_range = [int(row[0]) * 1000 for row in rows if s(start) <= int(row[0]) <= s(end)]
        return min(in_range) if in_range else None
    except Exception: return None


def kraken_futures(sym, start, end):
    try:
        r = requests.get(f"https://futures.kraken.com/api/charts/v1/trade/PF_{sym}USD/1d",
                         params={"from": s(start), "to": s(end)}, headers=UA, timeout=20)
        if r.status_code != 200: return None
        candles = r.json().get("candles") or []
        if candles: return min(int(c["time"]) for c in candles)
    except Exception: pass
    return None


def bitget_spot(sym, start, end):
    try:
        r = requests.get("https://api.bitget.com/api/v2/spot/market/candles",
                         params={"symbol": f"{sym}USDT", "granularity": "1Dutc",
                                 "startTime": ms(start), "endTime": ms(end), "limit": 1000},
                         headers=UA, timeout=20)
        if r.status_code != 200: return None
        d = r.json().get("data") or []
        if d: return min(int(row[0]) for row in d)
    except Exception: pass
    return None


def bitget_perp(sym, start, end):
    for v in _perp_variants(sym):
        try:
            r = requests.get("https://api.bitget.com/api/v2/mix/market/candles",
                             params={"symbol": f"{v}USDT", "productType": "usdt-futures",
                                     "granularity": "1Dutc", "startTime": ms(start),
                                     "endTime": ms(end), "limit": 1000},
                             headers=UA, timeout=20)
            if r.status_code != 200: continue
            d = r.json().get("data") or []
            if d: return min(int(row[0]) for row in d)
        except Exception: continue
    return None


def gate_spot(sym, start, end):
    out = []
    cur = start
    while cur < end:                       # gate caps ~1000 points per call
        chunk_end = min(cur + timedelta(days=900), end)
        try:
            r = requests.get("https://api.gateio.ws/api/v4/spot/candlesticks",
                             params={"currency_pair": f"{sym}_USDT", "interval": "1d",
                                     "from": s(cur), "to": s(chunk_end)},
                             headers=UA, timeout=20)
            if r.status_code == 200:
                d = r.json() or []
                out += [int(row[0]) * 1000 for row in d if isinstance(row, list)]
        except Exception: pass
        cur = chunk_end
    return min(out) if out else None


def gate_perp(sym, start, end):
    for v in _perp_variants(sym):
        out = []
        cur = start
        while cur < end:
            chunk_end = min(cur + timedelta(days=1900), end)
            try:
                r = requests.get("https://api.gateio.ws/api/v4/futures/usdt/candlesticks",
                                 params={"contract": f"{v}_USDT", "interval": "1d",
                                         "from": s(cur), "to": s(chunk_end)},
                                 headers=UA, timeout=20)
                if r.status_code == 200:
                    d = r.json() or []
                    out += [int(x["t"]) * 1000 for x in d if isinstance(x, dict) and "t" in x]
            except Exception: pass
            cur = chunk_end
        if out: return min(out)
    return None


def okx(sym, start, end, kind):
    # OKX history-candles caps at 100 rows and returns newest-first; paginate
    # backward (older) via `after` until we pass `start`. Daily bars.
    insts = ([f"{sym}-USDT-SWAP", f"1000{sym}-USDT-SWAP"] if kind == "swap"
             else [f"{sym}-USDT"])
    for inst in insts:
        earliest = None
        after = ms(end)
        for _ in range(40):                # 40*100 days ≈ 11 yr ceiling
            try:
                r = requests.get("https://www.okx.com/api/v5/market/history-candles",
                                 params={"instId": inst, "bar": "1Dutc",
                                         "after": after, "limit": 100},
                                 headers=UA, timeout=20)
                if r.status_code != 200: break
                d = r.json().get("data") or []
                if not d: break
                times = [int(row[0]) for row in d]
                batch_min = min(times)
                earliest = batch_min if earliest is None else min(earliest, batch_min)
                if batch_min <= ms(start): break
                after = batch_min          # page older
            except Exception: break
        if earliest is not None:
            in_range = earliest if earliest >= ms(start) else None
            # earliest may be < floor only if listing predates floor; clamp out
            if earliest >= ms(start):
                return earliest
            # otherwise the genuine first candle is before floor: still its listing
            return earliest
    return None


# venue-key -> (callable, report-event-label)
VENUES = {
    "okx_spot":       (lambda s_, a, b: okx(s_, a, b, "spot"),  "OKX Spot"),
    "okx_perp":       (lambda s_, a, b: okx(s_, a, b, "swap"),  "OKX Perp"),
    "bybit_spot":     (lambda s_, a, b: bybit(s_, a, b, "spot"),   "Bybit Spot"),
    "bybit_perp":     (lambda s_, a, b: bybit(s_, a, b, "linear"), "Bybit Perp"),
    "kraken_spot":    (kraken_spot,    "Kraken Spot"),
    "kraken_futures": (kraken_futures, "Kraken Futures"),
    "kucoin_spot":    (kucoin_spot,    "KuCoin Spot"),
    "kucoin_futures": (kucoin_futures, "KuCoin Futures"),
    "bitget_spot":    (bitget_spot,    "Bitget Spot"),
    "bitget_perp":    (bitget_perp,    "Bitget Perp"),
    "gate_spot":      (gate_spot,      "Gate.io Spot"),
    "gate_perp":      (gate_perp,      "Gate.io Perp"),
}


def parse_iso(t: str) -> datetime:
    return datetime.fromisoformat(t.replace("Z", "+00:00"))


def token_floor(cfg: dict) -> datetime:
    """Earliest known listing event (any venue) minus a buffer."""
    times = [parse_iso(e["iso_time_utc"]) for e in cfg.get("events", []) if e.get("iso_time_utc")]
    if not times:
        return parse_iso(cfg.get("window_start_utc", "2023-01-01T00:00:00Z"))
    return min(times) - FLOOR_BUFFER


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    force = "--force" in sys.argv
    only = {a.lower() for a in args} or None

    cache = {} if force else (json.loads(CACHE.read_text()) if CACHE.exists() else {})

    files = sorted(LISTINGS.glob("*.json"))
    for fp in files:
        cfg = json.loads(fp.read_text())
        slug = fp.stem
        if only and slug not in only:
            continue
        sym = cfg["token"].upper()
        floor = token_floor(cfg)
        rec = cache.setdefault(slug, {"symbol": sym, "floor": floor.isoformat(), "venues": {}})
        rec["floor"] = floor.isoformat()
        print(f"\n{slug} ({sym})  floor>={floor.date()}")
        for vkey, (fn, label) in VENUES.items():
            if vkey in rec["venues"] and not force:
                cached = rec["venues"][vkey]
                print(f"  {label:16} cached: {cached['iso'] if cached else '—'}")
                continue
            try:
                t_ms = fn(sym, floor, NOW)
            except Exception as e:
                t_ms = None
                print(f"  {label:16} ERROR {e}")
            if t_ms:
                t = datetime.fromtimestamp(t_ms / 1000, tz=timezone.utc)
                rec["venues"][vkey] = {"label": label, "iso": t.isoformat().replace("+00:00", "Z")}
                flag = "  <-- before floor?" if t < floor else ""
                print(f"  {label:16} {t.date()}{flag}")
            else:
                rec["venues"][vkey] = None
                print(f"  {label:16} —")
            time.sleep(0.15)
        CACHE.write_text(json.dumps(cache, indent=2))
    print(f"\nWrote {CACHE}")


if __name__ == "__main__":
    main()
