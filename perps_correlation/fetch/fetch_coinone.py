"""Extract Coinone (KR) listing times for the reaction-report tokens and merge
them into listings/*.json as "Coinone" events.

Coinone exposes no listing-date field, so we use the same earliest-candle method
as sweep_venues.py: page daily candles back to the first one (= listing day),
floored at the token's earliest known event minus a buffer to avoid ticker
collisions, then refine to the exact first 1-minute candle for minute-precision
timing.

    python fetch_coinone.py            # fetch + merge all
    python fetch_coinone.py aero ctr   # subset
    python fetch_coinone.py --no-merge # fetch/cache only

Cache: cache/coinone_listings.json  {slug: {"iso","day","exact"} | null}
Then `python build_listing_report.py` repopulates the Coinone chip + charts.
"""
from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests

HERE = Path(__file__).resolve().parents[1]   # perps_correlation/ (project root, NOT this subfolder)
LISTINGS = HERE / "listings"
CACHE = HERE.parent / "cache" / "coinone_listings.json"
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120"}
BASE = "https://api.coinone.co.kr/public/v2"
FLOOR_BUFFER = timedelta(days=30)
DAY_MS = 86_400_000


def parse_iso(t: str) -> datetime:
    return datetime.fromisoformat(t.replace("Z", "+00:00"))


def token_floor(cfg: dict) -> datetime:
    times = [parse_iso(e["iso_time_utc"]) for e in cfg.get("events", []) if e.get("iso_time_utc")]
    if not times:
        return parse_iso(cfg.get("window_start_utc", "2023-01-01T00:00:00Z"))
    return min(times) - FLOOR_BUFFER


def _chart(sym: str, interval: str, timestamp: int | None = None) -> tuple[list[int], bool]:
    params = {"interval": interval, "size": 500}
    if timestamp:
        params["timestamp"] = timestamp
    try:
        r = requests.get(f"{BASE}/chart/KRW/{sym}", params=params, headers=UA, timeout=20)
        if r.status_code != 200:
            return [], True
        j = r.json()
        ts = [int(c["timestamp"]) for c in (j.get("chart") or [])]
        return ts, bool(j.get("is_last"))
    except Exception:
        return [], True


def markets() -> set[str]:
    try:
        r = requests.get(f"{BASE}/markets/KRW", headers=UA, timeout=20)
        return {m.get("target_currency", "").upper() for m in (r.json().get("markets") or [])}
    except Exception:
        return set()


def earliest_day(sym: str) -> int | None:
    """Smallest daily-candle timestamp (ms), paging back to start of history."""
    best, cursor = None, None
    for _ in range(20):
        ts, last = _chart(sym, "1d", cursor)
        if not ts:
            break
        lo = min(ts)
        best = lo if best is None else min(best, lo)
        if last:
            break
        cursor = lo
        time.sleep(0.15)
    return best


def earliest_minute(sym: str, day_ms: int) -> int | None:
    """First 1m candle on/around the listing day = exact first-trade minute."""
    best, cursor = None, day_ms + 2 * DAY_MS
    for _ in range(8):
        ts, last = _chart(sym, "1m", cursor)
        if not ts:
            break
        lo = min(ts)
        best = lo if best is None else min(best, lo)
        if last or lo < day_ms - 2 * DAY_MS:
            break
        cursor = lo
        time.sleep(0.15)
    return best


def iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def merge(cache: dict, only: set | None):
    added = skipped = 0
    for fp in sorted(LISTINGS.glob("*.json")):
        slug = fp.stem
        if only and slug not in only:
            continue
        rec = cache.get(slug)
        if not rec or not rec.get("iso"):
            continue
        cfg = json.loads(fp.read_text())
        if any(e["exchange"] == "Coinone" for e in cfg.get("events", [])):
            skipped += 1
            continue
        cfg.setdefault("events", []).append({
            "exchange": "Coinone",
            "iso_time_utc": rec["iso"],
            "note": "first Coinone KRW candle" + ("" if rec.get("exact") else " (day resolution)"),
        })
        cfg["events"].sort(key=lambda e: e["iso_time_utc"])
        fp.write_text(json.dumps(cfg, indent=2))
        added += 1
    print(f"merge: +{added} Coinone events, {skipped} already had one")


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    only = {a.lower() for a in args} or None
    no_merge = "--no-merge" in sys.argv

    cache = json.loads(CACHE.read_text()) if CACHE.exists() else {}
    listed = markets()
    print(f"Coinone KRW markets: {len(listed)}")

    for fp in sorted(LISTINGS.glob("*.json")):
        slug = fp.stem
        if only and slug not in only:
            continue
        cfg = json.loads(fp.read_text())
        sym = cfg["token"].upper()
        if sym not in listed:
            cache[slug] = None
            print(f"{slug:10} not on Coinone")
            CACHE.write_text(json.dumps(cache, indent=2))
            continue
        floor = token_floor(cfg)
        day = earliest_day(sym)
        if not day:
            cache[slug] = None
            print(f"{slug:10} no candles")
        elif datetime.fromtimestamp(day / 1000, tz=timezone.utc) < floor:
            cache[slug] = None      # earliest candle predates known cluster -> likely collision
            print(f"{slug:10} SKIP earliest {iso(day)[:10]} < floor {floor.date()} (collision?)")
        else:
            minute = earliest_minute(sym, day)
            exact = bool(minute and abs(minute - day) <= 2 * DAY_MS)
            ms = minute if exact else day
            cache[slug] = {"iso": iso(ms), "day": iso(day)[:10], "exact": exact}
            print(f"{slug:10} {iso(ms)}  {'exact' if exact else 'day'}")
        CACHE.write_text(json.dumps(cache, indent=2))
        time.sleep(0.2)

    if not no_merge:
        merge(cache, only)
    print(f"\nWrote {CACHE}")


if __name__ == "__main__":
    main()
