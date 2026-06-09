"""Fetch each token's project website + X/Twitter handle from GeckoTerminal's
token-info endpoint and cache them for the report.

Keyed by listing slug; cached to `cache/token_socials.json`, resumable.

    python fetch_token_socials.py            # incremental
    python fetch_token_socials.py --force    # refetch all
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import requests

HERE = Path(__file__).resolve().parents[1]   # perps_correlation/ (project root, NOT this subfolder)
LISTINGS = HERE / "listings"
CACHE = HERE.parent / "cache" / "token_socials.json"
UA = {"User-Agent": "Mozilla/5.0 verifysheet/socials"}

# listings `chain` value -> GeckoTerminal network slug.
GT_NET = {
    "ethereum": "eth", "eth": "eth", "bsc": "bsc", "base": "base",
    "solana": "solana", "arbitrum": "arbitrum", "linea": "linea",
    "tron": "tron", "sui": "sui-network",
}


# cmc_slug -> CoinGecko coin id, when they differ. Used by the CG fallback.
CG_ALIAS = {"story-protocol": "story", "katana-network": "katana"}


def fetch_coingecko(cmc_slug: str) -> dict | None:
    """Fallback for tokens GeckoTerminal has no info for (e.g. BSC Alpha-bridged
    contracts): look the project up on CoinGecko by id (≈ the CMC slug)."""
    cid = CG_ALIAS.get(cmc_slug, cmc_slug)
    url = f"https://api.coingecko.com/api/v3/coins/{cid}"
    params = {"localization": "false", "tickers": "false", "market_data": "false",
              "community_data": "false", "developer_data": "false"}
    for attempt in range(4):
        try:
            r = requests.get(url, params=params, headers=UA, timeout=20)
            if r.status_code == 429:
                time.sleep(8 * (attempt + 1))
                continue
            if r.status_code != 200:
                return None
            l = r.json().get("links", {})
            hp = [h for h in (l.get("homepage") or []) if h]
            return {"website": hp[0] if hp else None,
                    "twitter": l.get("twitter_screen_name") or None}
        except Exception:
            time.sleep(2)
    return None


def fetch(net: str, addr: str) -> dict | None:
    url = f"https://api.geckoterminal.com/api/v2/networks/{net}/tokens/{addr}/info"
    for attempt in range(4):                       # retry through 429 / transient errors
        try:
            r = requests.get(url, headers=UA, timeout=20)
            if r.status_code == 429:
                time.sleep(5 * (attempt + 1))
                continue
            if r.status_code != 200:
                return None
            a = r.json().get("data", {}).get("attributes", {})
            sites = a.get("websites") or []
            return {
                "website": sites[0] if sites else None,
                "twitter": a.get("twitter_handle") or None,
            }
        except Exception:
            time.sleep(2)
    return None


def main():
    force = "--force" in sys.argv
    missing_only = "--missing" in sys.argv   # refetch entries that came back empty
    cache = {} if force else (json.loads(CACHE.read_text()) if CACHE.exists() else {})

    for fp in sorted(LISTINGS.glob("*.json")):
        slug = fp.stem
        cached = cache.get(slug)
        if cached and not force:
            empty = not cached.get("website") and not cached.get("twitter")
            if not (missing_only and empty):
                continue
        cfg = json.loads(fp.read_text())
        chain = (cfg.get("chain") or "").lower()
        addr = cfg.get("token_contract")
        net = GT_NET.get(chain)
        if not net or not addr:
            cache[slug] = {"website": None, "twitter": None}
            print(f"{slug:10} no chain/addr ({chain})")
            CACHE.write_text(json.dumps(cache, indent=2))
            continue
        info = fetch(net, addr) or {"website": None, "twitter": None}
        src = "GT"
        if not info.get("website") and not info.get("twitter") and cfg.get("cmc_slug"):
            cg = fetch_coingecko(cfg["cmc_slug"])   # GT had nothing → try CoinGecko
            if cg and (cg.get("website") or cg.get("twitter")):
                info, src = cg, "CG"
        cache[slug] = info
        print(f"{slug:10} [{src}] web={info['website'] or '—'}  x={info['twitter'] or '—'}")
        CACHE.write_text(json.dumps(cache, indent=2))
        time.sleep(2.2)            # GeckoTerminal free tier ~30 req/min

    print(f"\nWrote {CACHE} ({len(cache)} tokens)")


if __name__ == "__main__":
    main()
