"""Backfill Binance main-spot listing times (the filter's "Binance Spot" chip
was empty — only Alpha + Perp had been extracted).

Earliest 1d candle = listing day (floored at the token's earliest known event to
avoid ticker collisions), refined to the first 1m candle for exact-minute time.
Merges "Binance Spot" events into listings/*.json (idempotent).

    python fetch_binance_spot.py            # fetch + merge all
    python fetch_binance_spot.py aero ondo  # subset
    python fetch_binance_spot.py --no-merge

Cache: cache/binance_spot_listings.json  {slug: {"iso","day","exact"} | null}
Then `python build_listing_report.py` repopulates the Binance Spot chip + charts.
"""
from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests

HERE = Path(__file__).parent
LISTINGS = HERE / "listings"
CACHE = HERE.parent / "cache" / "binance_spot_listings.json"
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120"}
KLINES = "https://api.binance.com/api/v3/klines"
FLOOR_BUFFER = timedelta(days=30)
DAY_MS = 86_400_000


def parse_iso(t: str) -> datetime:
    return datetime.fromisoformat(t.replace("Z", "+00:00"))


def token_floor(cfg: dict) -> datetime:
    times = [parse_iso(e["iso_time_utc"]) for e in cfg.get("events", []) if e.get("iso_time_utc")]
    if not times:
        return parse_iso(cfg.get("window_start_utc", "2023-01-01T00:00:00Z"))
    return min(times) - FLOOR_BUFFER


def _klines(sym: str, interval: str, start_ms: int, limit: int) -> list:
    try:
        r = requests.get(KLINES, params={"symbol": f"{sym}USDT", "interval": interval,
                                          "startTime": start_ms, "limit": limit},
                         headers=UA, timeout=15)
        return r.json() if r.status_code == 200 else []
    except Exception:
        return []


def earliest_spot(sym: str, floor: datetime) -> tuple[int | None, bool]:
    """(exact-or-day open time ms, exact?) for the first Binance-spot candle."""
    start = int(floor.timestamp() * 1000)
    day_rows = _klines(sym, "1d", start, 1)        # oldest candle at/after floor
    if not day_rows:
        return None, False
    day_ms = int(day_rows[0][0])
    min_rows = _klines(sym, "1m", day_ms, 1)       # first minute of that day's trading
    if min_rows:
        return int(min_rows[0][0]), True
    return day_ms, False


def iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def merge(cache: dict, only):
    added = skipped = 0
    for fp in sorted(LISTINGS.glob("*.json")):
        slug = fp.stem
        if only and slug not in only:
            continue
        rec = cache.get(slug)
        if not rec or not rec.get("iso"):
            continue
        cfg = json.loads(fp.read_text())
        if any(e["exchange"] == "Binance Spot" for e in cfg.get("events", [])):
            skipped += 1
            continue
        cfg.setdefault("events", []).append({
            "exchange": "Binance Spot",
            "iso_time_utc": rec["iso"],
            "note": "first Binance spot candle" + ("" if rec.get("exact") else " (day resolution)"),
        })
        cfg["events"].sort(key=lambda e: e["iso_time_utc"])
        fp.write_text(json.dumps(cfg, indent=2))
        added += 1
    print(f"merge: +{added} Binance Spot events, {skipped} already had one")


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    only = {a.lower() for a in args} or None
    no_merge = "--no-merge" in sys.argv
    cache = json.loads(CACHE.read_text()) if CACHE.exists() else {}

    for fp in sorted(LISTINGS.glob("*.json")):
        slug = fp.stem
        if only and slug not in only:
            continue
        cfg = json.loads(fp.read_text())
        sym = cfg["token"].upper()
        floor = token_floor(cfg)
        ms, exact = earliest_spot(sym, floor)
        if ms:
            cache[slug] = {"iso": iso(ms), "day": iso(ms)[:10], "exact": exact}
            print(f"{slug:10} {iso(ms)}  {'exact' if exact else 'day'}")
        else:
            cache[slug] = None
            print(f"{slug:10} not on Binance spot")
        CACHE.write_text(json.dumps(cache, indent=2))
        time.sleep(0.1)

    if not no_merge:
        merge(cache, only)
    print(f"\nWrote {CACHE}")


if __name__ == "__main__":
    main()
