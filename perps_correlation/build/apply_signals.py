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

NEW-TOKEN AUTO-LISTING (the inclusion rule): a signal for an UNTRACKED symbol
creates a brand-new `listings/<sym>.json` IFF the token has at least one BINANCE
signal (Binance Alpha / Perp / Spot — `BINANCE_VENUES`). That is the universe
rule: a token belongs on the site once Binance lists it. Signals that only ever
hit other venues (Upbit/Bithumb/Bitget/…) for a token we've never seen on Binance
are "listed elsewhere" noise and create nothing. The auto-created config is built
purely from the BWEnews data (symbol, name parsed from the "Name (SYM)" headline,
every signal as a `source:"bwenews"` event → chart markers + events rows); it has
no price/pool data yet (a later /add-token or data refresh enriches it). Short
tickers (≤2 chars) must have their NAME appear in a headline before we create,
to avoid incidental-substring false positives.

Guarantees: idempotent (an existing event for a venue is never duplicated or
overwritten — curated, candle-verified times always win; an existing token is
never re-created), and it only ever ADDS information. Safe to run every build.

    python apply_signals.py            # apply to all tracked tokens + auto-list new Binance ones
    python apply_signals.py --dry-run  # show what would change, write nothing
"""
from __future__ import annotations

import json
import re
import sys
from datetime import timedelta
from pathlib import Path

# A signal qualifies an UNTRACKED token for auto-listing only if it lists on one
# of these Binance venues (the site's inclusion rule).
BINANCE_VENUES = {"Binance Alpha", "Binance Perp", "Binance Spot"}

import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))  # make lib./fetch./build. importable from anywhere
from lib.listing_chart import parse_iso

HERE = Path(__file__).resolve().parents[1]   # perps_correlation/ (project root, NOT this subfolder)
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


def _confident_match(cfg: dict, sig: dict) -> bool:
    """Guard against false positives for short tickers (audit M-4). A 1-2 char symbol
    (G, IP, LA...) can appear incidentally in an unrelated headline, so for those we
    require the token's NAME to also appear in the title before trusting the match.
    Longer symbols are distinctive enough on their own."""
    sym = (cfg.get("token") or "").upper()
    if len(sym) > 2:
        return True
    name = (cfg.get("name") or "").lower()
    title = (sig.get("title") or "").lower()
    return bool(name) and name in title


def apply_to_token(cfg: dict, sigs: list[dict]) -> list[str]:
    """Mutate cfg in place. Return human-readable change lines (empty = no change)."""
    changes: list[str] = []
    events = cfg.setdefault("events", [])
    not_listed = cfg.get("not_listed", [])

    # newest signal per venue (a venue may appear in several headlines); drop
    # low-confidence matches for short tickers.
    by_venue: dict[str, dict] = {}
    for s in sigs:
        v = s.get("venue")
        t = s.get("published_utc")
        if not v or not t or not _confident_match(cfg, s):
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


# Verbs/qualifiers that precede the name in a headline ("Binance Will List Re (RE)"
# → strip down to "Re", not "List Re").
_NAME_STOP = {"will", "list", "listing", "launch", "launches", "add", "added", "support",
              "supports", "the", "on", "to", "trading", "spot", "futures", "futures'",
              "perpetual", "contract", "contracts", "margined", "with", "seed", "tag",
              "applied", "binance", "new", "usdt", "premiere", "world"}


def _name_from_titles(sym: str, sigs: list[dict]) -> str:
    """Parse a human name out of a "Name (SYM)" pattern in any headline (e.g.
    "Archium (ARX)" -> "Archium", "ReProtocol (RE)" -> "ReProtocol"), stripping any
    leading listing-verb words. Prefer a Binance-sourced headline; ignore a name
    that's just the ticker again."""
    rx = re.compile(r"([A-Za-z][A-Za-z0-9.'\-]{1,29}(?:\s[A-Za-z0-9.'\-]{1,20})?)\s*\(" +
                    re.escape(sym) + r"\)")
    ordered = sorted(sigs, key=lambda s: 0 if (s.get("venue") or "").startswith("Binance") else 1)
    for s in ordered:
        m = rx.search(s.get("title") or "")
        if not m:
            continue
        words = m.group(1).strip().split()
        while words and words[0].lower() in _NAME_STOP:
            words.pop(0)
        nm = " ".join(words).strip()
        if nm and nm.upper() != sym:
            return nm
    return ""


def create_token_from_signals(sym: str, sigs: list[dict]) -> dict | None:
    """Build a brand-new minimal listing config from BWEnews signals, or None if
    the token doesn't qualify (no Binance signal, or a short ticker with no name)."""
    if not any((s.get("venue") in BINANCE_VENUES) for s in sigs):
        return None                                  # listed only elsewhere — not ours
    name = _name_from_titles(sym, sigs)
    if len(sym) <= 2 and not name:
        return None                                  # too risky to invent a 1-2 char token

    # newest signal per venue (a venue may appear in several near-identical headlines)
    by_venue: dict[str, dict] = {}
    for s in sigs:
        v, t = s.get("venue"), s.get("published_utc")
        if not v or not t:
            continue
        if v not in by_venue or t > (by_venue[v].get("published_utc") or ""):
            by_venue[v] = s

    events = []
    for v, s in by_venue.items():
        t_iso = parse_iso(s["published_utc"]).strftime("%Y-%m-%dT%H:%M:%SZ")
        title = (s.get("title") or "").strip()
        link = s.get("link") or ""
        events.append({
            "exchange": v,
            "iso_time_utc": t_iso,
            "note": (f"Auto-listed from BWEnews RSS: \"{title[:140]}\" "
                     f"(announcement time, approximate — not candle-verified). {link}").strip(),
            "source": "bwenews",
        })
    events.sort(key=lambda e: e["iso_time_utc"])
    first = parse_iso(events[0]["iso_time_utc"])
    last = parse_iso(events[-1]["iso_time_utc"])
    return {
        "token": sym,
        "name": name or sym,
        "new": True,
        "source": "bwenews-auto",                    # marks an auto-listed (data-light) token
        "window_start_utc": (first - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "window_end_utc": (last + timedelta(hours=6)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "events": events,
        "not_listed": [],
        "annotations": [],
    }


def main(argv: list[str]) -> int:
    dry = "--dry-run" in argv
    if not SIGNALS.exists():
        print("apply_signals: no bwenews_signals.json yet — nothing to apply.")
        return 0
    data = json.loads(SIGNALS.read_text(encoding="utf-8"))
    # all symbol-bearing signals (tracked OR not) — untracked Binance ones can now
    # create a new token; updates to existing tokens still only ever ADD.
    sigs = [s for s in data.get("signals", []) if s.get("symbol")]

    by_token: dict[str, list] = {}
    for s in sigs:
        by_token.setdefault(s["symbol"].upper(), []).append(s)

    touched = created = 0
    for sym, toksigs in sorted(by_token.items()):
        p = LISTINGS / f"{sym.lower()}.json"
        tag = "[dry-run] " if dry else ""
        if p.exists():
            cfg = json.loads(p.read_text(encoding="utf-8"))
            changes = apply_to_token(cfg, toksigs)
            if changes:
                touched += 1
                print(f"{tag}{sym}: " + "; ".join(changes))
                if not dry:
                    p.write_text(json.dumps(cfg, ensure_ascii=False, indent=2),
                                 encoding="utf-8")
        else:
            cfg = create_token_from_signals(sym, toksigs)
            if cfg:
                created += 1
                venues = ", ".join(e["exchange"] for e in cfg["events"])
                print(f"{tag}{sym}: NEW token auto-listed (Binance signal) — events: {venues}")
                if not dry:
                    p.write_text(json.dumps(cfg, ensure_ascii=False, indent=2),
                                 encoding="utf-8")
    print(f"apply_signals: {touched} updated, {created} created"
          f"{' (dry-run)' if dry else ''}.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
