"""Top-10 token holders + concentration — keyless, best-effort.

On-chain holder distribution has no reliable no-key source across all chains, so
this is keyless/best-effort by design: ERC-20 tokens on **Ethereum** are read
from Ethplorer's public `freekey`; other chains (BSC / Base / Solana / …) have no
solid keyless holder endpoint and return `available=False` so the UI can show an
explicit "holder data unavailable on <chain>" state instead of a wrong number.

Per the agreed definition, **retail = 100% − (sum of the top-10 holders' share)**.
Burn/dead and known CEX/contract wallets are NOT excluded (simple top-10), so a
large burn address inflates "top-10 concentration" — that's by design for now.

Writes cache/scam_holders/<SYM>.json:
    { symbol, chain, available, source, top10_share, retail_share,
      holders: [ {rank, address, share}, ... ] }

    python fetch_holders.py DEXE            # one token (reads chain/contract from scam_data)
    python fetch_holders.py                 # all scam-watchlist tokens
"""
from __future__ import annotations

import json
import sys
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).parent
CACHE = HERE.parent / "cache"
OUT = CACHE / "scam_holders"
SCAM = CACHE / "scam_data.json"
UA = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}

# CoinGecko platform key -> whether we have a keyless holder source for it.
ETHPLORER_CHAINS = {"ethereum"}


def _get(url, tries=3):
    for i in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=20) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception:
            if i == tries - 1:
                return None
            time.sleep(1.0 * (i + 1))
    return None


def _ethplorer_top(contract: str, limit: int = 10):
    d = _get(f"https://api.ethplorer.io/getTopTokenHolders/{contract}?apiKey=freekey&limit={limit}")
    holders = (d or {}).get("holders")
    if not holders:
        return None
    return [{"rank": i + 1, "address": h.get("address"), "share": h.get("share")}
            for i, h in enumerate(holders) if h.get("share") is not None]


def fetch_holders(symbol: str, chain: str | None, contract: str | None) -> dict:
    sym = symbol.upper()
    base = {"symbol": sym, "chain": chain, "available": False, "source": None,
            "holders": [], "top10_share": None, "retail_share": None,
            "fetched_at": int(time.time())}
    if chain in ETHPLORER_CHAINS and contract:
        top = _ethplorer_top(contract, 10)
        if top:
            top10 = round(sum(h["share"] for h in top), 2)
            base.update(available=True, source="ethplorer", holders=top,
                        top10_share=top10, retail_share=round(100.0 - top10, 2))
    return base


def main(argv):
    OUT.mkdir(parents=True, exist_ok=True)
    data = json.loads(SCAM.read_text(encoding="utf-8"))
    want = {a.upper() for a in argv} or None
    done = 0
    for rec in data.values():
        sym = rec["symbol"].upper()
        if want and sym not in want:
            continue
        res = fetch_holders(sym, rec.get("chain"), rec.get("contract"))
        (OUT / f"{sym}.json").write_text(json.dumps(res, ensure_ascii=False), encoding="utf-8")
        tag = (f"top10={res['top10_share']}% retail={res['retail_share']}%"
               if res["available"] else f"unavailable ({rec.get('chain') or 'no chain'})")
        print(f"{sym:9} {tag}")
        done += 1
        if res["available"]:
            time.sleep(0.5)   # be gentle on the shared freekey
    print(f"\nfetch_holders: {done} token(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
