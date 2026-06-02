"""Test the reference listing-timing findings against our 74-token funnel set.

Korean = Upbit/Bithumb only (Coinone history is API-capped, unreliable).
Lags in days; signed so positive = second venue is later.
"""
from __future__ import annotations
import json, statistics as st
from datetime import date
from pathlib import Path

HERE = Path(__file__).parent
rows = json.loads((HERE / "funnel_dated.json").read_text(encoding="utf-8"))


def d(s): return date.fromisoformat(s) if s else None
def lag(a, b):  # b - a
    a, b = d(a), d(b)
    return (b - a).days if a and b else None
def summ(xs):
    xs = [x for x in xs if x is not None]
    if not xs: return "n=0"
    return f"n={len(xs)} median={st.median(xs):.0f}d mean={st.mean(xs):.1f}d min={min(xs)} max={max(xs)}"


for r in rows:
    kr = [x for x in (r["upbit_date"], r["bithumb_date"]) if x]
    r["first_kr2"] = min(kr) if kr else None
    r["alpha_d"] = r["alpha_iso"][:10]
    r["perp_d"] = r["perp_onboard_iso"][:10]

def fdvf(r):
    try: return float(r["fdv"])
    except: return None

print("="*70)
print(f"FUNNEL SET: {len(rows)} tokens (Alpha 2025+ & Perp & Coinbase & Upbit/Bithumb-or-Coinone)")
print("Korean = Upbit/Bithumb only below.\n")

# ---- Coinbase <-> Binance Perp ----
cbperp = [(r["symbol"], lag(r["coinbase_date"], r["perp_d"])) for r in rows if r["coinbase_date"]]
cbperp = [(s, l) for s, l in cbperp if l is not None]
perp_first = [l for s, l in cbperp if l < 0]
cb_first   = [l for s, l in cbperp if l > 0]
same       = [l for s, l in cbperp if l == 0]
print("1) COINBASE -> BINANCE PERP  (signed = perp_date - coinbase_date)")
print(f"   overlaps={len(cbperp)}  perp-first={len(perp_first)}  same-day={len(same)}  coinbase-first={len(cb_first)}")
print(f"   when Coinbase first, wait to perp: {summ(cb_first)}")
print(f"   abs gap all: {summ([abs(l) for s,l in cbperp])}")
within3 = [l for l in cb_first if l <= 3]
print(f"   of {len(cb_first)} coinbase-first: {len(within3)} got perp within 3d; {len(cb_first)-len(within3)} took >3d\n")

# ---- Coinbase -> Korea ----
cbkr = [lag(r["coinbase_date"], r["first_kr2"]) for r in rows if r["coinbase_date"] and r["first_kr2"]]
cbkr_after = [l for l in cbkr if l is not None and l >= 0]
print("2) COINBASE -> FIRST KOREAN (Upbit/Bithumb)")
print(f"   overlaps={len([l for l in cbkr if l is not None])}  korean-after-or-same: {summ(cbkr_after)}")
print(f"   signed all: {summ(cbkr)}\n")

# ---- Perp -> Korea ----
pkr = [lag(r["perp_d"], r["first_kr2"]) for r in rows if r["first_kr2"]]
pkr_after = [l for l in pkr if l is not None and l >= 0]
print("3) BINANCE PERP -> FIRST KOREAN")
print(f"   korean-after-or-same: {summ(pkr_after)}")
print(f"   signed all: {summ(pkr)}\n")

# ---- Alpha relative (our extra angle) ----
ap = [lag(r["alpha_d"], r["perp_d"]) for r in rows]
print("4) OUR ANGLE  (Binance Alpha as anchor)")
print(f"   Alpha -> Perp signed: {summ(ap)}   (negative = perp existed before Alpha listing)")
print(f"   perp-before-alpha: {len([l for l in ap if l is not None and l<0])}  /  perp-after-alpha: {len([l for l in ap if l is not None and l>0])}")
ak = [lag(r["alpha_d"], r["first_kr2"]) for r in rows if r["first_kr2"]]
print(f"   Alpha -> first Korean: {summ(ak)}\n")

# ---- FDV buckets ----
print("5) FDV DISTRIBUTION (alpha-reported FDV)")
b = {"<100M":0,"100-300M":0,"300M-1B":0,">1B":0,"unknown":0}
for r in rows:
    f = fdvf(r)
    if f is None: b["unknown"]+=1
    elif f<100e6: b["<100M"]+=1
    elif f<300e6: b["100-300M"]+=1
    elif f<1e9: b["300M-1B"]+=1
    else: b[">1B"]+=1
for k,v in b.items(): print(f"   {k:10} {v}")
