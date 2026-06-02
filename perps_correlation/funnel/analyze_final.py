"""Corrected analysis: new-launch subset as the PRIMARY result, Coinone-only
reconciliation, and at-listing FDV (perp price at onboard x total supply)."""
from __future__ import annotations
import json, statistics as st
from pathlib import Path

HERE = Path(__file__).parent
M = json.loads((HERE / "funnel_master.json").read_text(encoding="utf-8"))
KL = HERE / "klines"


def summ(xs):
    xs = [x for x in xs if x is not None]
    return f"median {st.median(xs):.0f}d / mean {st.mean(xs):.0f}d (n={len(xs)})" if xs else "n=0"


def at_listing_fdv(m):
    # Use the same supply basis CMC uses for current FDV (max supply, else total)
    # so at-listing vs current FDV is an apples-to-apples comparison.
    tot = m.get("max_supply") or m.get("total")
    if not tot:
        return None
    for iv in ("1d", "1h"):
        p = KL / f"{m['symbol'].lower()}_{iv}.json"
        if p.exists():
            rows = json.loads(p.read_text(encoding="utf-8"))
            if rows:
                return rows[0][4] * tot  # first perp close (~onboard) x total supply
    return None


for m in M:
    m["new_launch"] = m["days_alpha_to_perp"] is not None and abs(m["days_alpha_to_perp"]) <= 7
    m["korea_datable"] = bool(m["upbit_date"] or m["bithumb_date"])
    m["at_listing_fdv"] = at_listing_fdv(m)

new = [m for m in M if m["new_launch"]]
majors = [m for m in M if not m["new_launch"]]
coinone_only = [m["symbol"] for m in M if not m["korea_datable"] and m["on_coinone"]]

print(f"TOTAL funnel: {len(M)}")
print(f"  new launches (|Alpha-Perp|<=7d): {len(new)}")
print(f"  re-featured majors (perp far from alpha): {len(majors)}")
print(f"  Korea-datable (Upbit/Bithumb): {sum(1 for m in M if m['korea_datable'])}")
print(f"  Coinone-only (no datable Korean leg): {len(coinone_only)} -> {coinone_only}")


def block(rows, label):
    cb2p_first = [m["days_coinbase_to_perp"] for m in rows if (m["days_coinbase_to_perp"] or -1) > 0]
    p2k = [m["days_perp_to_korean"] for m in rows if (m["days_perp_to_korean"] or -999) >= 0]
    cb2k = [m["days_coinbase_to_korean"] for m in rows if (m["days_coinbase_to_korean"] or -999) >= 0]
    within3 = sum(1 for x in cb2p_first if x <= 3)
    print(f"\n=== {label} (n={len(rows)}) ===")
    print(f"  Coinbase->Perp (CB first, {len(cb2p_first)}): {summ(cb2p_first)}  within3d={within3}/{len(cb2p_first)}")
    print(f"  Perp->Korea: {summ(p2k)}")
    print(f"  Coinbase->Korea: {summ(cb2k)}")


block(new, "PRIMARY: NEW LAUNCHES")
block(M, "POOLED (incl. re-featured majors) - directional only")


def buckets(vals):
    b = {"<100M": 0, "100-300M": 0, "300M-1B": 0, ">1B": 0, "unknown": 0}
    for f in vals:
        if not f: b["unknown"] += 1
        elif f < 100e6: b["<100M"] += 1
        elif f < 300e6: b["100-300M"] += 1
        elif f < 1e9: b["300M-1B"] += 1
        else: b[">1B"] += 1
    return b


print("\n=== FDV: current vs at-listing (new launches) ===")
print("  current   :", buckets([m["fdv"] for m in new]))
print("  at-listing:", buckets([m["at_listing_fdv"] for m in new]))
print("\n=== FDV at-listing (ALL 74) ===")
print("  at-listing:", buckets([m["at_listing_fdv"] for m in M]))

(HERE / "funnel_master.json").write_text(json.dumps(M, ensure_ascii=False, indent=2), encoding="utf-8")
print("\n(updated funnel_master.json with new_launch / korea_datable / at_listing_fdv)")
