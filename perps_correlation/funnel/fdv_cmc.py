"""Pull clean FDV from CoinMarketCap for the 74 funnel tokens.

Disambiguate ticker collisions by matching our Alpha contract address against
cmc_map.json's platform.token_address. Unmapped tokens -> None (backfilled from
GeckoTerminal later). Cached to fdv_cmc.json.
"""
from __future__ import annotations
import json, time
from pathlib import Path
import requests

UA = {"User-Agent": "Mozilla/5.0 verifysheet/fdv"}
ROOT = Path(__file__).parent.parent.parent
HERE = Path(__file__).parent
CACHE = HERE / "fdv_cmc.json"
out: dict = json.loads(CACHE.read_text(encoding="utf-8")) if CACHE.exists() else {}

cmc_map = json.loads((ROOT / "cmc_map.json").read_text(encoding="utf-8"))
by_addr: dict[str, dict] = {}
by_sym: dict[str, list] = {}
for e in cmc_map:
    by_sym.setdefault(e["symbol"].upper(), []).append(e)
    plat = e.get("platform")
    if plat and plat.get("token_address"):
        by_addr[str(plat["token_address"]).lower()] = e


def resolve_slug(sym: str, contract: str | None) -> str | None:
    addr = (contract or "").lower()
    if addr and addr in by_addr:
        return by_addr[addr]["slug"]
    cands = by_sym.get(sym.upper(), [])
    if len(cands) == 1:
        return cands[0]["slug"]
    # multiple: try contract match within candidates, else prefer active+highest rank
    for c in cands:
        plat = c.get("platform") or {}
        if addr and str(plat.get("token_address", "")).lower() == addr:
            return c["slug"]
    active = [c for c in cands if c.get("is_active")]
    ranked = sorted(active or cands, key=lambda c: (c.get("rank") or 1e9))
    return ranked[0]["slug"] if ranked else None


def cmc_fdv(slug: str):
    r = requests.get("https://api.coinmarketcap.com/data-api/v3/cryptocurrency/detail",
                     params={"slug": slug}, headers=UA, timeout=25)
    st = (r.json().get("data") or {}).get("statistics") or {}
    return {"fdv": st.get("fullyDilutedMarketCap"), "mcap": st.get("marketCap"),
            "price": st.get("price"), "circ": st.get("circulatingSupply"),
            "total": st.get("totalSupply")}


def main():
    rows = json.loads((HERE / "funnel_dated.json").read_text(encoding="utf-8"))
    for i, r in enumerate(rows, 1):
        sym = r["symbol"]
        if sym in out:
            continue
        slug = resolve_slug(sym, r.get("contract"))
        rec = {"slug": slug, "fdv": None, "source": None}
        if slug:
            try:
                q = cmc_fdv(slug)
                rec.update(q); rec["source"] = "cmc"
            except Exception as e:
                rec["source"] = f"err:{e}"
            time.sleep(0.25)
        out[sym] = rec
        CACHE.write_text(json.dumps(out, indent=2), encoding="utf-8")
        fdv = rec.get("fdv")
        print(f"[{i:2}/{len(rows)}] {sym:10} slug={slug} fdv={f'${fdv/1e6:.0f}M' if fdv else '-'}")
    got = sum(1 for v in out.values() if v.get("fdv"))
    print(f"\nFDV resolved for {got}/{len(rows)} tokens")


if __name__ == "__main__":
    main()
