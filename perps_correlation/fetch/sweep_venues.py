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
    python sweep_venues.py --ci --limit 12   # CI mode: skip geo-blocked venues,
                                             # newest tokens first, cap count

Then `python merge_sweep.py` folds the cache into listings/*.json, and
`python build_listing_report.py` rebuilds the site.

## Cache contract (so it is self-healing — fixes the "frozen null" bug)

Each venue can be in one of three states, and we keep a per-venue `checked`
timestamp so a `null` is never frozen forever:

  • a HIT  -> {"label","iso"}      : the exchange confirmed a listing. This is an
                                     immutable fact; cached forever.
  • ABSENT -> null                 : the exchange answered (HTTP 200) but has no
                                     such market. Re-probed when it goes stale —
                                     ACTIVE tokens (a recent/`new` listing) re-
                                     check daily because they keep gaining venues;
                                     old tokens re-check rarely.
  • UNKNOWN-> key left absent      : we could NOT reach the exchange (non-200 /
                                     network / geo-block 451). We deliberately do
                                     NOT cache this as `null`, so a CI run that is
                                     451'd by Bybit/OKX can never poison the cache
                                     into hiding a listing that really exists.
                                     It just gets retried (locally, or next run).
"""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests

UA = {"User-Agent": "Mozilla/5.0 verifysheet/sweep"}
HERE = Path(__file__).resolve().parents[1]   # perps_correlation/ (project root, NOT this subfolder)
LISTINGS = HERE / "listings"
CACHE = HERE.parent / "cache" / "venue_sweep.json"
NOW = datetime.now(timezone.utc)
FLOOR_BUFFER = timedelta(days=30)   # allow listings slightly before known cluster

# A token is "active" (likely to still gain new venues) if its newest known
# event is within this window, or it is flagged `new`. Active tokens re-probe
# their ABSENT venues daily; stale tokens only every STALE_NULL_TTL.
ACTIVE_DAYS = timedelta(days=120)
ACTIVE_NULL_TTL = timedelta(days=1)
STALE_NULL_TTL = timedelta(days=60)

DAY_MS = 86_400_000


def ms(dt: datetime) -> int: return int(dt.timestamp() * 1000)
def s(dt: datetime) -> int: return int(dt.timestamp())


class FetchError(Exception):
    """The exchange could not be reached / refused (non-200, network, 451).

    Distinct from a clean "no such market" (a fetcher returns None for that).
    The caller treats this as UNKNOWN and never caches it as a negative.
    """


def _get(url: str, params: dict) -> requests.Response:
    """GET that raises FetchError on any non-200 so the caller can tell a real
    'not listed' (200 + empty) apart from 'couldn't reach the venue'."""
    r = requests.get(url, params=params, headers=UA, timeout=20)
    if r.status_code != 200:
        raise FetchError(f"{url} -> HTTP {r.status_code}")
    return r


# Each fetcher returns the earliest candle open time (ms) at or after `start`,
# trying common contract-symbol variants. Contract (see module docstring):
#   int  -> listed (earliest candle ms)
#   None -> the venue answered but has no such market (confirmed absent)
#   raise FetchError / any Exception -> couldn't determine (do not cache)
# Daily resolution. `variants` lets perps cover 1000x-multiplied tickers.

def _spot_variants(sym): return [sym]
def _perp_variants(sym): return [sym, f"1000{sym}", f"10000{sym}"]


def bybit(sym, start, end, category):
    saw_ok = False
    for v in (_perp_variants(sym) if category == "linear" else _spot_variants(sym)):
        try:
            r = _get("https://api.bybit.com/v5/market/kline",
                     {"category": category, "symbol": f"{v}USDT", "interval": "D",
                      "start": ms(start), "end": ms(end), "limit": 1000})
        except Exception:
            continue
        saw_ok = True
        d = r.json().get("result", {}).get("list") or []
        if d:
            return min(int(row[0]) for row in d)
    if saw_ok:
        return None
    raise FetchError("bybit: all variants unreachable")


def kucoin_spot(sym, start, end):
    r = _get("https://api.kucoin.com/api/v1/market/candles",
             {"symbol": f"{sym}-USDT", "type": "1day",
              "startAt": s(start), "endAt": s(end)})
    d = r.json().get("data") or []
    return min(int(row[0]) for row in d) * 1000 if d else None


def kucoin_futures(sym, start, end):
    saw_ok = False
    for v in _perp_variants(sym):
        try:
            r = _get("https://api-futures.kucoin.com/api/v1/kline/query",
                     {"symbol": f"{v}USDTM", "granularity": 1440,
                      "from": ms(start), "to": ms(end)})
        except Exception:
            continue
        saw_ok = True
        d = r.json().get("data") or []
        if d:
            return min(int(row[0]) for row in d)
    if saw_ok:
        return None
    raise FetchError("kucoin_futures: all variants unreachable")


def kraken_spot(sym, start, end):
    r = _get("https://api.kraken.com/0/public/OHLC",
             {"pair": f"{sym}USD", "interval": 1440, "since": s(start)})
    result = r.json().get("result") or {}
    rows = next((v for k, v in result.items() if k != "last"), None)
    if not rows:
        return None
    in_range = [int(row[0]) * 1000 for row in rows if s(start) <= int(row[0]) <= s(end)]
    return min(in_range) if in_range else None


def kraken_futures(sym, start, end):
    r = _get(f"https://futures.kraken.com/api/charts/v1/trade/PF_{sym}USD/1d",
             {"from": s(start), "to": s(end)})
    candles = r.json().get("candles") or []
    return min(int(c["time"]) for c in candles) if candles else None


def bitget_spot(sym, start, end):
    r = _get("https://api.bitget.com/api/v2/spot/market/candles",
             {"symbol": f"{sym}USDT", "granularity": "1Dutc",
              "startTime": ms(start), "endTime": ms(end), "limit": 1000})
    d = r.json().get("data") or []
    return min(int(row[0]) for row in d) if d else None


def bitget_perp(sym, start, end):
    saw_ok = False
    for v in _perp_variants(sym):
        try:
            r = _get("https://api.bitget.com/api/v2/mix/market/candles",
                     {"symbol": f"{v}USDT", "productType": "usdt-futures",
                      "granularity": "1Dutc", "startTime": ms(start),
                      "endTime": ms(end), "limit": 1000})
        except Exception:
            continue
        saw_ok = True
        d = r.json().get("data") or []
        if d:
            return min(int(row[0]) for row in d)
    if saw_ok:
        return None
    raise FetchError("bitget_perp: all variants unreachable")


def gate_spot(sym, start, end):
    cur = start                            # oldest first; first non-empty chunk
    while cur < end:                       # holds the earliest candle. gate caps
        chunk_end = min(cur + timedelta(days=900), end)   # ~1000 points per call
        r = _get("https://api.gateio.ws/api/v4/spot/candlesticks",
                 {"currency_pair": f"{sym}_USDT", "interval": "1d",
                  "from": s(cur), "to": s(chunk_end)})
        d = r.json() or []
        rows = [int(row[0]) * 1000 for row in d if isinstance(row, list)]
        if rows:
            return min(rows)
        cur = chunk_end
    return None


def gate_perp(sym, start, end):
    saw_ok = False
    for v in _perp_variants(sym):
        found = None
        cur = start
        ok = True
        while cur < end:
            chunk_end = min(cur + timedelta(days=1900), end)
            try:
                r = _get("https://api.gateio.ws/api/v4/futures/usdt/candlesticks",
                         {"contract": f"{v}_USDT", "interval": "1d",
                          "from": s(cur), "to": s(chunk_end)})
            except Exception:
                ok = False
                break
            d = r.json() or []
            rows = [int(x["t"]) * 1000 for x in d if isinstance(x, dict) and "t" in x]
            if rows:
                found = min(rows)
                break
            cur = chunk_end
        if ok:
            saw_ok = True
            if found is not None:
                return found
    if saw_ok:
        return None
    raise FetchError("gate_perp: all variants unreachable")


def okx(sym, start, end, kind):
    # OKX history-candles caps at 100 rows and returns newest-first; paginate
    # backward (older) via `after` until we pass `start`. Daily bars.
    insts = ([f"{sym}-USDT-SWAP", f"1000{sym}-USDT-SWAP"] if kind == "swap"
             else [f"{sym}-USDT"])
    for inst in insts:
        earliest = None
        after = ms(end)
        for _ in range(40):                # 40*100 days ≈ 11 yr ceiling
            r = _get("https://www.okx.com/api/v5/market/history-candles",
                     {"instId": inst, "bar": "1Dutc", "after": after, "limit": 100})
            d = r.json().get("data") or []
            if not d:
                break
            times = [int(row[0]) for row in d]
            batch_min = min(times)
            earliest = batch_min if earliest is None else min(earliest, batch_min)
            if batch_min <= ms(start):
                break
            after = batch_min              # page older
        if earliest is not None:
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

# Venues whose APIs geo-block GitHub's datacenter IP (HTTP 451), per the klines
# gap-fill lesson in CLAUDE.md. Skipped in --ci mode so a cloud run doesn't waste
# its budget hammering endpoints it can never reach. They stay LOCAL-only: run a
# plain `python sweep_venues.py` locally to fill OKX/Bybit. (Even without this,
# the FetchError contract means a 451 never poisons the cache — this is purely an
# efficiency skip.)
CI_BLOCKED = {"okx_spot", "okx_perp", "bybit_spot", "bybit_perp"}


def parse_iso(t: str) -> datetime:
    return datetime.fromisoformat(t.replace("Z", "+00:00"))


def token_floor(cfg: dict) -> datetime:
    """Earliest known listing event (any venue) minus a buffer."""
    times = [parse_iso(e["iso_time_utc"]) for e in cfg.get("events", []) if e.get("iso_time_utc")]
    if not times:
        return parse_iso(cfg.get("window_start_utc", "2023-01-01T00:00:00Z"))
    return min(times) - FLOOR_BUFFER


def token_active(cfg: dict) -> bool:
    """A token still likely to gain new venue listings: flagged `new`, or its
    newest known event is recent."""
    if cfg.get("new"):
        return True
    times = [parse_iso(e["iso_time_utc"]) for e in cfg.get("events", []) if e.get("iso_time_utc")]
    if not times:
        return True
    return (NOW - max(times)) <= ACTIVE_DAYS


def should_reprobe_null(cfg: dict, checked_iso: str | None) -> bool:
    """Re-probe a venue cached as ABSENT (null) when its check has gone stale.
    This is the fix for the frozen-null bug: a venue that listed a token AFTER
    the first sweep used to stay null forever; now active tokens re-check daily."""
    if not checked_iso:
        return True                        # legacy null with no timestamp -> recheck
    try:
        age = NOW - parse_iso(checked_iso)
    except Exception:
        return True
    ttl = ACTIVE_NULL_TTL if token_active(cfg) else STALE_NULL_TTL
    return age > ttl


def main():
    argv = sys.argv[1:]
    force = "--force" in argv
    ci = "--ci" in argv or os.environ.get("SWEEP_CI") == "1"
    limit = None
    if "--limit" in argv:
        i = argv.index("--limit")
        limit = int(argv[i + 1])
        del argv[i:i + 2]               # drop the flag AND its value
    limit = limit or (int(os.environ["SWEEP_LIMIT"]) if os.environ.get("SWEEP_LIMIT") else None)
    args = [a for a in argv if not a.startswith("-")]
    only = {a.lower() for a in args} or None

    # Always load the existing cache so a filtered run never wipes other tokens;
    # --force just means "re-probe the selected tokens regardless of cached state".
    cache = json.loads(CACHE.read_text(encoding="utf-8")) if CACHE.exists() else {}

    files = sorted(LISTINGS.glob("*.json"))
    if only:
        files = [f for f in files if f.stem in only]
    elif ci:
        # newest listings first — they're the ones still gaining venues.
        def newest_event(fp):
            cfg = json.loads(fp.read_text(encoding="utf-8"))
            times = [e["iso_time_utc"] for e in cfg.get("events", []) if e.get("iso_time_utc")]
            return max(times) if times else ""
        files = sorted(files, key=newest_event, reverse=True)
    if limit:
        files = files[:limit]

    venue_items = [(k, v) for k, v in VENUES.items() if not (ci and k in CI_BLOCKED)]

    for fp in files:
        cfg = json.loads(fp.read_text(encoding="utf-8"))
        slug = fp.stem
        sym = cfg["token"].upper()
        floor = token_floor(cfg)
        rec = cache.setdefault(slug, {"symbol": sym, "floor": floor.isoformat(),
                                      "venues": {}, "checked": {}})
        rec["floor"] = floor.isoformat()
        rec.setdefault("checked", {})
        print(f"\n{slug} ({sym})  floor>={floor.date()}{'  [active]' if token_active(cfg) else ''}")
        for vkey, (fn, label) in venue_items:
            cached = rec["venues"].get(vkey, "__absent__")
            if not force:
                if isinstance(cached, dict):          # immutable HIT — keep
                    print(f"  {label:16} cached: {cached['iso']}")
                    continue
                if cached is None and not should_reprobe_null(cfg, rec["checked"].get(vkey)):
                    print(f"  {label:16} —  (null, still fresh)")
                    continue
            try:
                t_ms = fn(sym, floor, NOW)
            except Exception as e:
                # UNKNOWN: do NOT cache a negative — leave it to retry next time.
                print(f"  {label:16} ? unreachable ({type(e).__name__}) — left for retry")
                time.sleep(0.15)
                continue
            rec["checked"][vkey] = NOW.isoformat().replace("+00:00", "Z")
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
