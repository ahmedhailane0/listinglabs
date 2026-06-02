"""Merge dates + gecko-enrich + CMC-FDV into funnel_master.json, write
funnel_table.csv (with day-lag columns), and print the findings analysis with
clean FDV buckets.

FDV + supply come from fdv_audit.json — the authoritative re-audit that resolves
each token's CMC slug by contract address (collision-proof) and verifies the
returned symbol. This supersedes the old fdv_final.json / fdv_cmc.json, whose
ticker/name heuristics mis-resolved ~50 tokens (e.g. ETHGas $28M→$1.09B). GT is
kept only as a last-resort fallback if the audit somehow lacks a token.
"""
from __future__ import annotations
import json, csv, statistics as st
from datetime import date
from pathlib import Path

HERE = Path(__file__).parent
dated = json.loads((HERE / "funnel_dated.json").read_text(encoding="utf-8"))
enrich = json.loads((HERE / "enrich.json").read_text(encoding="utf-8"))
cmc = json.loads((HERE / "fdv_audit.json").read_text(encoding="utf-8"))


def d(s): return date.fromisoformat(s) if s else None
def lag(a, b):
    a, b = d(a), d(b)
    return (b - a).days if a and b else None
def fnum(x):
    try: return float(x)
    except (TypeError, ValueError): return None


master = []
for r in dated:
    sym = r["symbol"]
    e = enrich.get(sym, {})
    c = cmc.get(sym, {})
    gt_fdv = fnum(e.get("fdv"))
    cmc_fdv = fnum(c.get("fdv")) if c.get("ok") else None
    # CMC (audit) is authoritative; GT only fills a genuinely-missing token.
    if cmc_fdv and cmc_fdv > 0:
        final_fdv, fdv_src = cmc_fdv, "cmc"
    elif gt_fdv and gt_fdv > 0:
        final_fdv, fdv_src = gt_fdv, "gt"
    else:
        final_fdv, fdv_src = None, None
    kr = [x for x in (r["upbit_date"], r["bithumb_date"]) if x]
    first_kr = min(kr) if kr else None
    alpha, perp, cb = r["alpha_iso"][:10], r["perp_onboard_iso"][:10], r["coinbase_date"]
    m = {
        "symbol": sym, "name": r.get("name"), "chain": r.get("chain"),
        "perp_symbol": r.get("perp_symbol"),
        "fdv": final_fdv, "fdv_src": fdv_src,
        "cmc_slug": c.get("slug"), "gecko_pool": e.get("pool"), "net": e.get("net"),
        "contract": r.get("contract"), "mcap": fnum(c.get("mcap")) or fnum(e.get("mcap")),
        "circ": fnum(c.get("circ")),
        "total": fnum(c.get("total")) or fnum(e.get("total_supply")),
        "max_supply": fnum(c.get("max")),
        "alpha_date": alpha, "perp_date": perp, "coinbase_date": cb,
        "upbit_date": r["upbit_date"], "bithumb_date": r["bithumb_date"],
        "coinone_date": r["coinone_date"], "first_korean": first_kr,
        "on_upbit": r["on_upbit"], "on_bithumb": r["on_bithumb"], "on_coinone": r["on_coinone"],
        "days_alpha_to_perp": lag(alpha, perp),
        "days_alpha_to_coinbase": lag(alpha, cb),
        "days_alpha_to_korean": lag(alpha, first_kr),
        "days_coinbase_to_perp": lag(cb, perp),
        "days_coinbase_to_korean": lag(cb, first_kr),
        "days_perp_to_korean": lag(perp, first_kr),
    }
    master.append(m)

master.sort(key=lambda x: x["alpha_date"])
(HERE / "funnel_master.json").write_text(json.dumps(master, ensure_ascii=False, indent=2), encoding="utf-8")

cols = ["symbol", "name", "chain", "fdv", "fdv_src", "alpha_date", "perp_date", "coinbase_date",
        "upbit_date", "bithumb_date", "first_korean",
        "days_alpha_to_perp", "days_alpha_to_coinbase", "days_alpha_to_korean",
        "days_coinbase_to_perp", "days_coinbase_to_korean", "days_perp_to_korean",
        "on_upbit", "on_bithumb", "on_coinone"]
with (HERE / "funnel_table.csv").open("w", newline="", encoding="utf-8") as f:
    w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
    w.writeheader()
    for m in master:
        w.writerow(m)


def summ(xs):
    xs = [x for x in xs if x is not None]
    return f"n={len(xs)} median={st.median(xs):.0f}d mean={st.mean(xs):.1f}d" if xs else "n=0"


print(f"\nmaster: {len(master)} tokens | CSV + JSON written")
fdvok = sum(1 for m in master if m["fdv"])
print(f"clean FDV: {fdvok}/{len(master)}")
print("\n--- FDV buckets (clean) ---")
b = {"<100M": 0, "100-300M": 0, "300M-1B": 0, ">1B": 0, "unknown": 0}
for m in master:
    f = m["fdv"]
    if not f: b["unknown"] += 1
    elif f < 100e6: b["<100M"] += 1
    elif f < 300e6: b["100-300M"] += 1
    elif f < 1e9: b["300M-1B"] += 1
    else: b[">1B"] += 1
for k, v in b.items(): print(f"  {k:10} {v}")

print("\n--- lag summaries ---")
print("Alpha->Perp     ", summ([m["days_alpha_to_perp"] for m in master]),
      f'(perp-before-alpha: {sum(1 for m in master if (m["days_alpha_to_perp"] or 0) < 0)})')
print("Coinbase->Perp  ", summ([m["days_coinbase_to_perp"] for m in master]))
cbfirst = [m["days_coinbase_to_perp"] for m in master if (m["days_coinbase_to_perp"] or -1) > 0]
print("  Coinbase-first only:", summ(cbfirst), f'within3d={sum(1 for x in cbfirst if x<=3)}/{len(cbfirst)}')
print("Coinbase->Korean", summ([m["days_coinbase_to_korean"] for m in master if (m["days_coinbase_to_korean"] or -999) >= 0]))
print("Perp->Korean    ", summ([m["days_perp_to_korean"] for m in master if (m["days_perp_to_korean"] or -999) >= 0]))
