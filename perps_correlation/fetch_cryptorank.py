"""Fetch CryptoRank v0 funding data per project.

CryptoRank's free v0 endpoints (no key) give us:
  /v0/search?query=...           -> slug
  /v0/coins/{slug}               -> icoData.raised.USD, hasFundingRounds, fundIds, FDV
  /v0/coins/{slug}/investors     -> tier1..tier5, angel, other  (each has isLead flag)

We cache the raw per-coin response and raw investors response. Aggregation
happens later in build_enriched.py so the analysis stage can re-derive fields.

Output: cache/cryptorank.json   { ticker -> {"slug": str, "coin": {...}, "investors": {...}, "search_hit": {...}|None} }
"""
import json
import sys
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import requests

ROOT = Path(__file__).parent.parent
CACHE = ROOT / "cache"
OUT = CACHE / "cryptorank.json"

UA = {"User-Agent": "Mozilla/5.0 verifysheet/correlation",
      "Accept": "application/json"}
BASE = "https://api.cryptorank.io/v0"


def load(p, default):
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else default


def save(p, obj):
    p.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")


def search(name: str, ticker: str):
    """Return the best slug match from CryptoRank search."""
    try:
        r = requests.get(f"{BASE}/search", params={"query": name}, headers=UA, timeout=20)
        if r.status_code != 200:
            return None
        coins = r.json().get("coins", [])
        ticker_u = ticker.upper()
        # exact symbol+name match preferred
        for c in coins:
            if c.get("symbol", "").upper() == ticker_u and c.get("name", "").lower() == name.lower():
                return c
        for c in coins:
            if c.get("symbol", "").upper() == ticker_u:
                return c
        # fallback to ticker search
        r2 = requests.get(f"{BASE}/search", params={"query": ticker}, headers=UA, timeout=15)
        if r2.status_code == 200:
            for c in r2.json().get("coins", []):
                if c.get("symbol", "").upper() == ticker_u:
                    return c
        return coins[0] if coins else None
    except Exception as e:
        print(f"  search error {name}/{ticker}: {e}")
        return None


def fetch_coin(slug: str):
    for attempt in range(3):
        try:
            r = requests.get(f"{BASE}/coins/{slug}", headers=UA, timeout=20)
            if r.status_code != 200:
                return None
            return r.json().get("data")
        except Exception as e:
            if attempt == 2:
                print(f"  coin fetch failed {slug}: {e}")
                return None
            time.sleep(1.0)


def fetch_investors(slug: str):
    for attempt in range(3):
        try:
            r = requests.get(f"{BASE}/coins/{slug}/investors", headers=UA, timeout=20)
            if r.status_code != 200:
                return None
            return r.json()
        except Exception as e:
            if attempt == 2:
                print(f"  investors fetch failed {slug}: {e}")
                return None
            time.sleep(1.0)


def main():
    rows = json.loads((ROOT / "parsed_rows.json").read_text(encoding="utf-8"))
    out = load(OUT, {})
    n = 0
    for row in rows:
        ticker = row.get("symbol", "")
        name = row.get("name", "")
        if not ticker or not name:
            continue
        if ticker in out and out[ticker].get("coin") is not None:
            continue
        try:
            hit = search(name, ticker)
        except Exception as e:
            print(f"  search crash {name}: {e}")
            hit = None
        time.sleep(0.25)
        if not hit:
            out[ticker] = {"slug": None, "coin": None, "investors": None, "search_hit": None, "name": name}
            continue
        slug = hit["key"]
        coin = fetch_coin(slug)
        time.sleep(0.25)
        investors = fetch_investors(slug) if coin else None
        time.sleep(0.25)
        out[ticker] = {"slug": slug, "coin": coin, "investors": investors, "search_hit": hit, "name": name}
        n += 1
        if n % 25 == 0:
            save(OUT, out)
            try:
                print(f"  ...{n} fetched, last={name}({ticker}) slug={slug}")
            except UnicodeEncodeError:
                print(f"  ...{n} fetched, last=({ticker}) slug={slug}")
    save(OUT, out)
    matched = sum(1 for v in out.values() if v.get("slug"))
    print(f"done. {matched}/{len(out)} tickers matched a CryptoRank slug.")


if __name__ == "__main__":
    main()
