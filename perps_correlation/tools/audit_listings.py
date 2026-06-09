"""Audit every tracked token for MISSING real major-venue listings.

Motivation: the candle-sweep (`sweep_venues.py`) only runs by hand, and its
cache is incremental — once a venue is recorded as `null` it is never re-probed,
so a listing that lands *after* a token's first sweep stays invisible forever.
This audit ignores that cache entirely: it force-probes the live exchange candle
APIs (the authoritative source — immune to CoinMarketCap-style phantom markets)
for OKX / Bybit / Kraken / KuCoin / Bitget / Gate on both spot and perp, then
diffs what the exchanges actually have against the events already in
`listings/<token>.json`.

It is **read-only**: it writes a report, never touches `listings/*.json` or the
sweep cache. Apply real changes with `sweep_venues.py --force` + `merge_sweep.py`.

    python audit_listings.py                 # audit every token
    python audit_listings.py btw ctr aero    # only these
    python audit_listings.py --limit 10      # first 10 (alphabetical)

Outputs:
  • console summary (per-token gaps),
  • cache/listing_audit.json  (machine-readable),
  • AUDIT_LISTINGS.md         (human report).
"""
from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))  # make lib./fetch./build. importable from anywhere
from fetch.sweep_venues import VENUES, token_floor, NOW
from build.merge_sweep import classify

HERE = Path(__file__).resolve().parents[1]   # perps_correlation/ (project root, NOT this subfolder)
LISTINGS = HERE / "listings"
OUT_JSON = HERE.parent / "cache" / "listing_audit.json"
OUT_MD = HERE / "AUDIT_LISTINGS.md"


def existing_keys(cfg: dict) -> set:
    keys = {classify(e.get("exchange", "")) for e in cfg.get("events", [])}
    keys.discard(None)
    return keys


def audit_token(cfg: dict) -> dict:
    """Return {'token','floor','missing':[...],'present':[...],'suspect':[...]}.

    missing  = venue the exchange API confirms but the token has no event for
    suspect  = a hit whose first candle predates the collision floor (likely a
               different coin that traded under the same ticker — needs eyes)
    """
    sym = cfg["token"].upper()
    floor = token_floor(cfg)
    floor_ms = int(floor.timestamp() * 1000)
    have = existing_keys(cfg)

    missing, present, suspect = [], [], []
    for vkey, (fn, label) in VENUES.items():
        try:
            t_ms = fn(sym, floor, NOW)
        except Exception:
            t_ms = None
        time.sleep(0.12)
        if not t_ms:
            continue
        key = classify(label)            # (family, market)
        iso = datetime.fromtimestamp(t_ms / 1000, tz=timezone.utc).isoformat().replace("+00:00", "Z")
        entry = {"venue": label, "iso": iso, "key": list(key) if key else None}
        if t_ms < floor_ms:
            entry["note"] = "first candle predates floor — possible ticker collision"
            suspect.append(entry)
            continue
        if key in have:
            present.append(entry)
        else:
            missing.append(entry)
    return {
        "token": sym,
        "floor": floor.isoformat().replace("+00:00", "Z"),
        "missing": missing,
        "present": present,
        "suspect": suspect,
    }


def main(argv: list[str]) -> int:
    limit = None
    if "--limit" in argv:
        i = argv.index("--limit")
        limit = int(argv[i + 1])
        del argv[i:i + 2]
    only = {a.lower() for a in argv if not a.startswith("-")} or None

    files = sorted(LISTINGS.glob("*.json"))
    if only:
        files = [f for f in files if f.stem in only]
    if limit:
        files = files[:limit]

    results = []
    for fp in files:
        cfg = json.loads(fp.read_text(encoding="utf-8"))
        r = audit_token(cfg)
        results.append(r)
        miss = ", ".join(f"{m['venue']} {m['iso'][:10]}" for m in r["missing"]) or "—"
        susp = f"  ⚠{len(r['suspect'])} suspect" if r["suspect"] else ""
        print(f"{fp.stem:10} missing: {miss}{susp}")
        # write progressively so a long run is resumable to inspect
        OUT_JSON.write_text(json.dumps(results, indent=2), encoding="utf-8")

    write_report(results)
    n_gap = sum(1 for r in results if r["missing"])
    n_evt = sum(len(r["missing"]) for r in results)
    n_sus = sum(len(r["suspect"]) for r in results)
    print(f"\n{len(results)} tokens audited — {n_gap} have gaps "
          f"({n_evt} missing events), {n_sus} suspect hits.")
    print(f"Report: {OUT_MD}")
    return 0


def write_report(results: list[dict]) -> None:
    lines = ["# Listing audit — missing real major-venue listings", ""]
    lines.append(f"_Generated {NOW.isoformat().replace('+00:00','Z')}. "
                 "Source: live exchange candle APIs (OKX/Bybit/Kraken/KuCoin/"
                 "Bitget/Gate, spot+perp). Read-only._")
    lines.append("")
    gaps = [r for r in results if r["missing"]]
    if not gaps:
        lines.append("**No missing listings found.** Every token's events already "
                     "cover every venue its exchange API confirms.")
    else:
        lines.append(f"## {len(gaps)} token(s) missing listings")
        lines.append("")
        lines.append("| Token | Missing venue | Earliest candle (≈listing) |")
        lines.append("|---|---|---|")
        for r in gaps:
            for m in r["missing"]:
                lines.append(f"| {r['token']} | {m['venue']} | {m['iso'][:10]} |")
    susp = [r for r in results if r["suspect"]]
    if susp:
        lines.append("")
        lines.append("## ⚠ Suspect hits (pre-floor — likely ticker collision, verify by hand)")
        lines.append("")
        lines.append("| Token | Venue | First candle |")
        lines.append("|---|---|---|")
        for r in susp:
            for s in r["suspect"]:
                lines.append(f"| {r['token']} | {s['venue']} | {s['iso'][:10]} |")
    lines.append("")
    lines.append("---")
    lines.append("To apply the confirmed gaps: `python sweep_venues.py --force` "
                 "then `python merge_sweep.py` (curated/verified events always win).")
    OUT_MD.write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
