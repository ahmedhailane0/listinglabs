"""Close the loop: fold detected BWEnews listing signals back into the token
configs so a newly-detected venue listing actually shows up on the token's chart,
events table, and venue filter — autonomously.

Flow (runs in build_all, after fetch_bwenews, before the report build):
    cache/bwenews_signals.json  ->  listings/<token>.json

For each signal on a TRACKED token whose venue isn't already an event:
  • add an event at the announcement time, tagged `source: "bwenews"` with a note
    that quotes the headline and flags it as announcement-time / approximate
    (NOT candle-verified) — so auto data is always distinguishable from curated;
  • drop that venue from `not_listed` (the token IS now listed there);
  • extend `window_end_utc` to cover the new event so the chart's default view and
    candle data include the new listing mark (refresh_klines extends the candles).

Guarantees: idempotent (an existing event for a venue is never duplicated or
overwritten — curated, candle-verified times always win), and it only ever ADDS
information. Safe to run every build.

    python apply_signals.py            # apply to all tracked tokens
    python apply_signals.py --dry-run  # show what would change, write nothing
"""
from __future__ import annotations

import json
import sys
from datetime import timedelta
from pathlib import Path

from listing_chart import parse_iso

HERE = Path(__file__).parent
LISTINGS = HERE / "listings"
SIGNALS = HERE.parent / "cache" / "bwenews_signals.json"

# Venue label (as emitted by fetch_bwenews) -> keyword test for pruning the
# freeform `not_listed` strings. Binance perp/spot entries are phrased variously
# ("Binance USDT-M perp"), so they get a 2-keyword test instead of a prefix.
def _not_listed_match(entry: str, venue: str) -> bool:
    e = entry.lower()
    v = venue.lower()
    if venue == "Binance Perp":
        return "binance" in e and "perp" in e
    if venue == "Binance Spot":
        return "binance" in e and "spot" in e
    if venue == "Binance Alpha":
        return "binance" in e and "alpha" in e
    return v in e or e in v or e.startswith(v.split(".")[0])


def _venue_already_event(venue: str, events: list) -> bool:
    """True if the token already has an event for this venue family."""
    base = venue.split(".")[0]  # "Gate.io" -> "Gate"
    return any((ev.get("exchange", "").startswith(venue)
                or ev.get("exchange", "").startswith(base)) for ev in events)


def apply_to_token(cfg: dict, sigs: list[dict]) -> list[str]:
    """Mutate cfg in place. Return human-readable change lines (empty = no change)."""
    changes: list[str] = []
    events = cfg.setdefault("events", [])
    not_listed = cfg.get("not_listed", [])

    # newest signal per venue (a venue may appear in several headlines)
    by_venue: dict[str, dict] = {}
    for s in sigs:
        v = s.get("venue")
        t = s.get("published_utc")
        if not v or not t:
            continue
        if v not in by_venue or t > (by_venue[v].get("published_utc") or ""):
            by_venue[v] = s

    for venue, s in by_venue.items():
        if _venue_already_event(venue, events):
            continue  # curated / candle-verified event wins — never overwrite
        # normalize the announcement time to the curated "...Z" convention
        t_iso = parse_iso(s["published_utc"]).strftime("%Y-%m-%dT%H:%M:%SZ")
        link = s.get("link") or ""
        title = (s.get("title") or "").strip()
        events.append({
            "exchange": venue,
            "iso_time_utc": t_iso,
            "note": (f"Auto-added from BWEnews RSS: \"{title[:140]}\" "
                     f"(announcement time, approximate — not candle-verified). {link}").strip(),
            "source": "bwenews",
        })
        changes.append(f"+event {venue} @ {t_iso}")
        # prune not_listed
        kept = [e for e in not_listed if not _not_listed_match(e, venue)]
        if len(kept) != len(not_listed):
            changes.append(f"-not_listed {venue}")
            not_listed = kept

    if not changes:
        return []

    cfg["not_listed"] = not_listed
    events.sort(key=lambda ev: ev.get("iso_time_utc") or "")

    # extend the default chart window to cover the latest event (+6h headroom)
    latest = max(parse_iso(ev["iso_time_utc"]) for ev in events if ev.get("iso_time_utc"))
    cur_end = parse_iso(cfg["window_end_utc"])
    if latest > cur_end:
        new_end = latest + timedelta(hours=6)
        cfg["window_end_utc"] = new_end.strftime("%Y-%m-%dT%H:%M:%SZ")
        changes.append(f"window_end -> {cfg['window_end_utc']}")
    return changes


def main(argv: list[str]) -> int:
    dry = "--dry-run" in argv
    if not SIGNALS.exists():
        print("apply_signals: no bwenews_signals.json yet — nothing to apply.")
        return 0
    data = json.loads(SIGNALS.read_text(encoding="utf-8"))
    sigs = [s for s in data.get("signals", []) if s.get("tracked") and s.get("symbol")]

    # group tracked signals by token symbol
    by_token: dict[str, list] = {}
    for s in sigs:
        by_token.setdefault(s["symbol"].upper(), []).append(s)

    touched = 0
    for sym, toksigs in sorted(by_token.items()):
        p = LISTINGS / f"{sym.lower()}.json"
        if not p.exists():
            continue
        cfg = json.loads(p.read_text(encoding="utf-8"))
        changes = apply_to_token(cfg, toksigs)
        if changes:
            touched += 1
            tag = "[dry-run] " if dry else ""
            print(f"{tag}{sym}: " + "; ".join(changes))
            if not dry:
                p.write_text(json.dumps(cfg, ensure_ascii=False, indent=2),
                             encoding="utf-8")
    print(f"apply_signals: {touched} token(s) updated{' (dry-run)' if dry else ''}.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
