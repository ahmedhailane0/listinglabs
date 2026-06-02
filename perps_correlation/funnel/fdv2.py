"""High-precision FDV from CMC: slugify the project NAME, try a few candidate
slugs, and ACCEPT only when the returned symbol matches ours (rejects dead
namesakes like Plasma/PLASMA vs XPL). Leftovers -> None (reported, fill later).
Cached to fdv_final.json.
"""
from __future__ import annotations
import json, re, time
from pathlib import Path
import requests

UA = {"User-Agent": "Mozilla/5.0 verifysheet/fdv2"}
HERE = Path(__file__).parent
CACHE = HERE / "fdv_final.json"
out: dict = json.loads(CACHE.read_text(encoding="utf-8")) if CACHE.exists() else {}


def slugify(n: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (n or "").lower()).strip("-")


def detail(slug: str):
    r = requests.get("https://api.coinmarketcap.com/data-api/v3/cryptocurrency/detail",
                     params={"slug": slug}, headers=UA, timeout=20)
    if r.status_code != 200:
        return None
    d = r.json().get("data") or {}
    st = d.get("statistics") or {}
    return {"symbol": d.get("symbol"), "name": d.get("name"), "slug": slug,
            "fdv": st.get("fullyDilutedMarketCap"), "mcap": st.get("marketCap"),
            "circ": st.get("circulatingSupply"), "total": st.get("totalSupply")}


def resolve(sym: str, name: str):
    base = slugify(name)
    cands = [base, f"{base}-protocol", f"{base}-{sym.lower()}", f"{base}-network",
             f"{base}-token", f"{base}-finance", f"{base}-ai", sym.lower(), f"{sym.lower()}-token"]
    seen = set()
    for c in cands:
        if not c or c in seen:
            continue
        seen.add(c)
        try:
            d = detail(c)
        except Exception:
            d = None
        time.sleep(0.2)
        if d and d["symbol"] and d["symbol"].upper() == sym.upper():
            return d
    return None


def main():
    rows = json.loads((HERE / "funnel_dated.json").read_text(encoding="utf-8"))
    for i, r in enumerate(rows, 1):
        sym = r["symbol"]
        if sym in out:
            continue
        d = resolve(sym, r.get("name") or sym)
        out[sym] = d or {"fdv": None, "slug": None}
        CACHE.write_text(json.dumps(out, indent=2), encoding="utf-8")
        fdv = (d or {}).get("fdv")
        print(f"[{i:2}/{len(rows)}] {sym:10} {('slug='+d['slug']) if d else 'UNRESOLVED':28} fdv={f'${fdv/1e6:.0f}M' if fdv else '-'}")
    got = sum(1 for v in out.values() if v.get("fdv"))
    print(f"\nCMC FDV (symbol-verified): {got}/{len(rows)}")
    print("unresolved:", [s for s, v in out.items() if not v.get("fdv")])


if __name__ == "__main__":
    main()
