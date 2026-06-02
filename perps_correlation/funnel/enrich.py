"""Per-token GeckoTerminal lookup by EXACT contract address (collision-proof):
gets clean FDV/mcap/supply AND the top-liquidity pool (for charts) in one call.
Cached to enrich.json.
"""
from __future__ import annotations
import json, time
from pathlib import Path
import requests

UA = {"User-Agent": "Mozilla/5.0 verifysheet/enrich"}
HERE = Path(__file__).parent
CACHE = HERE / "enrich.json"
out: dict = json.loads(CACHE.read_text(encoding="utf-8")) if CACHE.exists() else {}

NET = {"BSC": "bsc", "Ethereum": "eth", "Base": "base", "Solana": "solana",
       "Sui": "sui-network", "Arbitrum": "arbitrum", "Linea": "linea", "TRON": "tron"}


def fnum(x):
    try: return float(x)
    except (TypeError, ValueError): return None


def lookup(net: str, addr: str) -> dict:
    for attempt in range(4):
        r = requests.get(f"https://api.geckoterminal.com/api/v2/networks/{net}/tokens/{addr}",
                         params={"include": "top_pools"}, headers=UA, timeout=25)
        if r.status_code == 429:
            time.sleep(3 * (attempt + 1)); continue
        if r.status_code != 200:
            return {"error": r.status_code}
        j = r.json()
        a = j.get("data", {}).get("attributes", {})
        inc = j.get("included", [])
        pools = [(fnum(p["attributes"].get("reserve_in_usd")) or 0, p["id"]) for p in inc]
        pool = max(pools)[1].split("_", 1)[-1] if pools else None
        return {"fdv": fnum(a.get("fdv_usd")), "mcap": fnum(a.get("market_cap_usd")),
                "price": fnum(a.get("price_usd")), "total_supply": fnum(a.get("total_supply")),
                "gt_symbol": a.get("symbol"), "gt_name": a.get("name"),
                "pool": pool, "reserve": max(pools)[0] if pools else None}
    return {"error": "429"}


def main():
    rows = json.loads((HERE / "funnel_dated.json").read_text(encoding="utf-8"))
    for i, r in enumerate(rows, 1):
        sym = r["symbol"]
        if sym in out and out[sym].get("pool"):
            continue
        net = NET.get(r.get("chain"))
        addr = r.get("contract")
        if not net or not addr:
            out[sym] = {"error": f"no net/addr (chain={r.get('chain')})"}
        else:
            out[sym] = lookup(net, addr)
            out[sym]["net"] = net
            time.sleep(2.2)
        CACHE.write_text(json.dumps(out, indent=2), encoding="utf-8")
        e = out[sym]
        fdv = e.get("fdv")
        print(f"[{i:2}/{len(rows)}] {sym:10} {(e.get('net') or ''):12} fdv={f'${fdv/1e6:.0f}M' if fdv else '-':>8} pool={'Y' if e.get('pool') else 'N'} {e.get('error','')}")
    ok = sum(1 for v in out.values() if v.get("pool"))
    fdvok = sum(1 for v in out.values() if v.get("fdv"))
    print(f"\npools={ok}/{len(rows)}  fdv={fdvok}/{len(rows)}")


if __name__ == "__main__":
    main()
