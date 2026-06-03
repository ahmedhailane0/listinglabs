"""Top-10 token holders + concentration — keyless, multichain.

On-chain holder distribution is sourced keyless across chains:
  - **GoPlus token-security API** (primary, keyless): covers every EVM chain we
    track (Ethereum / BNB Chain / Base / Polygon / Arbitrum / Optimism / Avalanche).
    Returns the top-10 holders with share %, a contract/CEX/burn tag, and the
    total holder count. This is what makes BNB-Chain (and other non-ETH) tokens
    work — the user's explicit requirement.
  - **Ethplorer** `freekey` (Ethereum fallback): used only if GoPlus returns
    nothing for an ETH token.
Chains GoPlus doesn't cover (hyperliquid / canton / monad / TON / …) return
`available=False`, so the UI shows an explicit "unavailable on <chain>" state
instead of a wrong number.

Per the agreed definition, **retail = 100% − (sum of the top-10 holders' share)**.
Burn/dead and known CEX/contract wallets are NOT excluded from the top-10 (simple
definition), but each holder carries a tag (e.g. "Null Address", a CEX name, or
"contract") so the report can label them.

Writes cache/scam_holders/<SYM>.json:
    { symbol, chain, available, source, top10_share, retail_share, holder_count,
      holders: [ {rank, address, share, is_contract, tag}, ... ] }

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

# CoinGecko platform key -> GoPlus chain id (EVM chains GoPlus supports).
CG_PLATFORM_TO_GOPLUS = {
    "ethereum": "1",
    "binance-smart-chain": "56",
    "base": "8453",
    "polygon-pos": "137",
    "arbitrum-one": "42161",
    "optimistic-ethereum": "10",
    "avalanche": "43114",
}
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


def _goplus_top(chain_id: str, contract: str, limit: int = 10):
    """Top holders via GoPlus token-security (keyless, multichain). Returns
    (holders, holder_count) or (None, None). GoPlus `percent` is a fraction
    (0.2 == 20%), so share = percent * 100."""
    addr = contract.lower()
    d = _get(f"https://api.gopluslabs.io/api/v1/token_security/{chain_id}"
             f"?contract_addresses={addr}")
    res = ((d or {}).get("result") or {}).get(addr) or {}
    raw = res.get("holders") or []
    if not raw:
        return None, None
    holders = []
    for i, h in enumerate(raw[:limit]):
        try:
            share = float(h.get("percent")) * 100.0
        except (TypeError, ValueError):
            continue
        holders.append({"rank": i + 1, "address": h.get("address"),
                        "share": round(share, 4),
                        "is_contract": bool(h.get("is_contract")),
                        "tag": (h.get("tag") or "").strip() or None})
    if not holders:
        return None, None
    hc = res.get("holder_count")
    try:
        hc = int(hc) if hc is not None else None
    except (TypeError, ValueError):
        hc = None
    return holders, hc


def _ethplorer_top(contract: str, limit: int = 10):
    d = _get(f"https://api.ethplorer.io/getTopTokenHolders/{contract}?apiKey=freekey&limit={limit}")
    holders = (d or {}).get("holders")
    if not holders:
        return None
    return [{"rank": i + 1, "address": h.get("address"), "share": h.get("share"),
             "is_contract": False, "tag": None}
            for i, h in enumerate(holders) if h.get("share") is not None]


def _finalize(base, holders, source, holder_count=None):
    top10 = round(sum(h["share"] for h in holders), 2)
    base.update(available=True, source=source, holders=holders,
                top10_share=top10, retail_share=round(100.0 - top10, 2),
                holder_count=holder_count)
    return base


def fetch_holders(symbol: str, chain: str | None, contract: str | None) -> dict:
    sym = symbol.upper()
    base = {"symbol": sym, "chain": chain, "available": False, "source": None,
            "holders": [], "top10_share": None, "retail_share": None,
            "holder_count": None, "fetched_at": int(time.time())}
    if not contract:
        return base
    # primary: GoPlus (covers BNB Chain + every EVM chain we track)
    gp_chain = CG_PLATFORM_TO_GOPLUS.get(chain or "")
    if gp_chain:
        top, hc = _goplus_top(gp_chain, contract, 10)
        if top:
            return _finalize(base, top, "goplus", hc)
    # fallback: Ethplorer for Ethereum if GoPlus came back empty
    if chain in ETHPLORER_CHAINS:
        top = _ethplorer_top(contract, 10)
        if top:
            return _finalize(base, top, "ethplorer")
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
