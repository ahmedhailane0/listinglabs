"""Fold the venue sweep (cache/venue_sweep.json) into listings/*.json events.

Adds an event per (venue, market) the sweep found, UNLESS the token already
has an event for that same venue-family + market class — existing events are
treated as authoritative (often hand-verified / minute-precision) and left
untouched. Swept events are tagged so they're distinguishable in the table.

    python merge_sweep.py            # apply
    python merge_sweep.py --dry      # show what would change, write nothing
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).parent
LISTINGS = HERE / "listings"
CACHE = HERE.parent / "cache" / "venue_sweep.json"
SWEEP_NOTE = "earliest-candle sweep (daily resolution)"

# venue-family + market class, used to detect "already covered".
FAMILY = {
    "OKX": "OKX", "Bybit": "Bybit", "Kraken": "Kraken",
    "KuCoin": "KuCoin", "Bitget": "Bitget", "Gate": "Gate",
}
PERP_WORDS = ("Perp", "Futures", "Swap")


def classify(exchange: str):
    """(family, market) for an event name, or None if not a target venue."""
    fam = next((f for k, f in FAMILY.items() if exchange.startswith(k)), None)
    if not fam:
        return None
    market = "perp" if any(w in exchange for w in PERP_WORDS) else "spot"
    return fam, market


def main():
    dry = "--dry" in sys.argv
    cache = json.loads(CACHE.read_text(encoding="utf-8"))
    added = skipped = 0

    for fp in sorted(LISTINGS.glob("*.json")):
        slug = fp.stem
        rec = cache.get(slug)
        if not rec:
            continue
        cfg = json.loads(fp.read_text(encoding="utf-8"))
        existing = {classify(e["exchange"]) for e in cfg.get("events", [])}
        existing.discard(None)

        new_events = []
        for vkey, hit in rec.get("venues", {}).items():
            if not hit:
                continue
            key = classify(hit["label"])
            if key in existing:
                skipped += 1
                continue
            existing.add(key)        # avoid adding two swept hits for same slot
            new_events.append({
                "exchange": hit["label"],
                "iso_time_utc": hit["iso"],
                "note": SWEEP_NOTE,
            })

        if new_events:
            cfg.setdefault("events", []).extend(new_events)
            cfg["events"].sort(key=lambda e: e["iso_time_utc"])
            added += len(new_events)
            labels = ", ".join(e["exchange"] for e in new_events)
            print(f"{slug:10} +{len(new_events):2}  {labels}")
            if not dry:
                fp.write_text(json.dumps(cfg, indent=2), encoding="utf-8")

    print(f"\n{'DRY RUN — ' if dry else ''}added {added} events, "
          f"skipped {skipped} already-covered")


if __name__ == "__main__":
    main()
