"""Per-token chain deployments for the Scam Watchlist bubble-map chain picker.

A watchlist token is often deployed on several chains (e.g. BILL on ETH / BSC /
Base / Mantle / Solana). The bubble-map section lets the user pick a chain and
see that chain's holder map, so we need the full {chain: contract} list per
token. CoinGecko's `detail_platforms` is the keyless source; we MERGE in the
single chain+contract already stored in scam_data.json so we never lose a
contract CoinGecko happens to omit (it returned empty platforms for LIT / SKYAI /
PIEVERSE even though we have their contracts).

Writes cache/scam_platforms.json:
    { "BILL": { "ethereum": "0x…", "binance-smart-chain": "0x…", … }, … }

This is LOCAL-ONLY (like backfill_perp_history): CoinGecko's free tier rate-limits
the shared CI IP, and a token's chain set changes rarely. Run locally and commit
the cache; CI's fetch_holders.py reads it. Re-run when you add tokens.

    python fetch_scam_chains.py
"""
from __future__ import annotations

import json
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]   # perps_correlation/ (project root, NOT this subfolder)
CACHE = HERE.parent / "cache"
SCAM = CACHE / "scam_data.json"
OUT = CACHE / "scam_platforms.json"
CG = "https://api.coingecko.com/api/v3"
UA = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
PACE = 2.6  # CoinGecko free tier ~30/min


def _get(url, tries=4):
    for i in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=25) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception as e:  # noqa: BLE001
            if "429" in str(e) and i < tries - 1:
                time.sleep(8)
            elif i < tries - 1:
                time.sleep(2)
            else:
                return None
    return None


def _platforms(cg_id: str) -> dict:
    """CoinGecko chain -> contract for a coin. Prefers detail_platforms (carries
    the address) and falls back to platforms; drops empty entries."""
    d = _get(f"{CG}/coins/{cg_id}?localization=false&tickers=false&market_data=false"
             f"&community_data=false&developer_data=false&sparkline=false")
    if not d:
        return {}
    src = d.get("detail_platforms") or {}
    raw = {k: (v.get("contract_address") if isinstance(v, dict) else v) for k, v in src.items()}
    if not any(raw.values()):
        raw = d.get("platforms") or {}
    return {k.strip(): v.strip() for k, v in raw.items() if k and isinstance(v, str) and v.strip()}


def main():
    data = json.loads(SCAM.read_text(encoding="utf-8"))
    out: dict[str, dict] = {}
    for rec in data.values():
        sym = rec["symbol"].upper()
        plats: dict[str, str] = {}
        cg = rec.get("cg_id")
        if cg:
            plats.update(_platforms(cg))
            time.sleep(PACE)
        # never lose the contract we already have on file
        chain, contract = rec.get("chain"), rec.get("contract")
        if chain and contract and chain not in plats:
            plats[chain] = contract
        out[sym] = plats
        print(f"{sym:9} {len(plats):2} chain(s)  {list(plats.keys())}")
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    multi = sum(1 for p in out.values() if len(p) > 1)
    print(f"\nwrote {OUT}  ({len(out)} tokens, {multi} multichain)")


if __name__ == "__main__":
    main()
