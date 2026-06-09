"""Top-10 token holders + concentration + Bubblemaps availability — keyless,
multichain, per chain a token is deployed on.

On-chain holder distribution is sourced keyless across chains:
  - **GoPlus token-security API** (primary, keyless): covers every EVM chain we
    track (Ethereum / BNB Chain / Base / Polygon / Arbitrum / Optimism / Avalanche).
    Returns the top-10 holders with share %, a contract/CEX/burn tag, and the
    total holder count. This is what makes BNB-Chain (and other non-ETH) tokens
    work — the user's explicit requirement.
  - **Ethplorer** `freekey` (Ethereum fallback): used only if GoPlus returns
    nothing for an ETH token.
Chains GoPlus doesn't cover (hyperliquid / mantle / stable / TON / …) return
`available=False`, so the UI shows an explicit "unavailable on <chain>" state
instead of a wrong number.

**Multichain.** A watchlist token can live on several chains; the bubble-map
chain picker needs each one. We read the {chain: contract} map from
`cache/scam_platforms.json` (built by fetch_scam_chains.py) and fetch holders for
EVERY chain, writing one file per chain:

    cache/scam_holders/<SYM>__<chain>.json   # one per deployment chain
    cache/scam_holders/<SYM>.json            # the PRIMARY chain (back-compat:
                                             # the existing top-holders table/donut)

Each file also carries `bubblemaps_available` (probed against the keyless
Bubblemaps map-availability endpoint for that chain+contract) so the builder can
decide, offline, whether to embed the official cluster map or show a fallback.

File schema:
    { symbol, chain, contract, available, source, top10_share, retail_share,
      holder_count, bubblemaps_available, holders: [ {rank, address, share,
      is_contract, tag}, … ] }

    python fetch_holders.py DEXE            # one token, all its chains
    python fetch_holders.py                 # all scam-watchlist tokens
"""
from __future__ import annotations

import json
import sys
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]   # perps_correlation/ (project root, NOT this subfolder)
CACHE = HERE.parent / "cache"
OUT = CACHE / "scam_holders"
SCAM = CACHE / "scam_data.json"
PLATFORMS = CACHE / "scam_platforms.json"
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
# CoinGecko platform key -> Bubblemaps legacy chain code (the chains their free
# map covers). Others (optimism / mantle / hyperliquid / stable) have no map.
CG_PLATFORM_TO_BMAPS = {
    "ethereum": "eth",
    "binance-smart-chain": "bsc",
    "base": "base",
    "polygon-pos": "poly",
    "arbitrum-one": "arbi",
    "avalanche": "avax",
    "fantom": "ftm",
    "cronos": "cro",
    "solana": "sol",
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
    # GoPlus silently returns an empty holder list under burst load, so retry
    # once after a pause before concluding the token genuinely has no data.
    res, raw = {}, []
    for attempt in range(2):
        d = _get(f"https://api.gopluslabs.io/api/v1/token_security/{chain_id}"
                 f"?contract_addresses={addr}")
        res = ((d or {}).get("result") or {}).get(addr) or {}
        raw = res.get("holders") or []
        if raw:
            break
        if attempt == 0:
            time.sleep(3.0)
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


def _bubblemaps_available(chain: str | None, contract: str | None) -> bool:
    """Does Bubblemaps' free legacy map exist for this chain+contract? Keyless
    GET; True only when their service reports the map is computed/available."""
    code = CG_PLATFORM_TO_BMAPS.get(chain or "")
    if not code or not contract:
        return False
    d = _get(f"https://api-legacy.bubblemaps.io/map-availability?chain={code}&token={contract}")
    if not isinstance(d, dict):
        return False
    return d.get("status") == "OK" and bool(d.get("availability"))


def _bubblemaps_meta(chain: str | None, contract: str | None) -> dict | None:
    """Bubblemaps-computed signal we CAN show (their map itself can't be embedded
    — their CSP frame-ancestors blocks external iframes, and the raw graph API is
    gated). map-metadata is keyless: a decentralisation score (0–100, higher =
    more spread out) plus the % of supply held in contracts / on CEXs, with the
    timestamp it was last computed. Returns None when no map exists."""
    code = CG_PLATFORM_TO_BMAPS.get(chain or "")
    if not code or not contract:
        return None
    d = _get(f"https://api-legacy.bubblemaps.io/map-metadata?chain={code}&token={contract}")
    if not isinstance(d, dict) or d.get("status") != "OK":
        return None
    ids = d.get("identified_supply") or {}
    return {"score": d.get("decentralisation_score"),
            "pct_cex": ids.get("percent_in_cexs"),
            "pct_contract": ids.get("percent_in_contracts"),
            "ts_update": d.get("ts_update"), "dt_update": d.get("dt_update")}


def _finalize(base, holders, source, holder_count=None):
    top10 = round(sum(h["share"] for h in holders), 2)
    base.update(available=True, source=source, holders=holders,
                top10_share=top10, retail_share=round(100.0 - top10, 2),
                holder_count=holder_count)
    return base


def fetch_holders(symbol: str, chain: str | None, contract: str | None) -> dict:
    sym = symbol.upper()
    base = {"symbol": sym, "chain": chain, "contract": contract, "available": False,
            "source": None, "holders": [], "top10_share": None, "retail_share": None,
            "holder_count": None, "bubblemaps_available": False, "bubblemaps_meta": None,
            "fetched_at": int(time.time())}
    if not contract:
        return base
    base["bubblemaps_available"] = _bubblemaps_available(chain, contract)
    if base["bubblemaps_available"]:
        base["bubblemaps_meta"] = _bubblemaps_meta(chain, contract)
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


def _chains_for(rec: dict, platforms: dict) -> dict:
    """{chain: contract} for a token: the platforms cache, with the scam_data
    chain+contract merged in so we never drop a contract we already have."""
    sym = rec["symbol"].upper()
    chains = dict(platforms.get(sym) or {})
    ch, c = rec.get("chain"), rec.get("contract")
    if ch and c and ch not in chains:
        chains[ch] = c
    return chains


def _primary_chain(rec: dict, chains: dict) -> str | None:
    """The chain shown by the back-compat <SYM>.json (top-holders table/donut):
    the scam_data chain if it's a real deployment, else the first chain."""
    ch = rec.get("chain")
    if ch and ch in chains:
        return ch
    return next(iter(chains), None)


def main(argv):
    OUT.mkdir(parents=True, exist_ok=True)
    data = json.loads(SCAM.read_text(encoding="utf-8"))
    platforms = json.loads(PLATFORMS.read_text(encoding="utf-8")) if PLATFORMS.exists() else {}
    want = {a.upper() for a in argv} or None
    done = 0
    for rec in data.values():
        sym = rec["symbol"].upper()
        if want and sym not in want:
            continue
        chains = _chains_for(rec, platforms)
        if not chains:  # no contract anywhere — still write an unavailable primary
            res = fetch_holders(sym, rec.get("chain"), rec.get("contract"))
            (OUT / f"{sym}.json").write_text(json.dumps(res, ensure_ascii=False), encoding="utf-8")
            print(f"{sym:9} no contract on any chain")
            done += 1
            continue
        primary = _primary_chain(rec, chains)
        for chain, contract in chains.items():
            res = fetch_holders(sym, chain, contract)
            # never clobber a previously-good holder list with a throttled blank:
            # if this run came back empty but the cached file had real data for
            # the same contract, keep that data (only refresh bubblemaps + stamp).
            cf = OUT / f"{sym}__{chain}.json"
            if not res["available"] and cf.exists():
                old = json.loads(cf.read_text(encoding="utf-8"))
                if old.get("available") and old.get("contract") == contract:
                    for k in ("available", "source", "holders", "top10_share",
                              "retail_share", "holder_count"):
                        res[k] = old[k]
                    res["stale_holders"] = True
            cf.write_text(json.dumps(res, ensure_ascii=False), encoding="utf-8")
            if chain == primary:  # back-compat copy for the existing holders block
                (OUT / f"{sym}.json").write_text(json.dumps(res, ensure_ascii=False),
                                                 encoding="utf-8")
            bm = " +bmap" if res["bubblemaps_available"] else ""
            tag = (f"top10={res['top10_share']}% retail={res['retail_share']}%"
                   if res["available"] else "unavailable")
            star = "*" if chain == primary else " "
            print(f"{sym:9}{star}{chain:22} {tag}{bm}")
            # GoPlus throttles bursts to empty results, so pace EVERY chain call
            # (not just hits) — otherwise valid holder lists come back blank.
            time.sleep(1.5)
        done += 1
    print(f"\nfetch_holders: {done} token(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
