"""Fetch a TGE date (token first-listed / generation) per Manipulated-watchlist
token, so the report can show + sort by it like the Listing Reactions report's
TGE column.

Source: CoinMarketCap's keyless web data-api `dateAdded` (when CMC first listed
the token ≈ its public debut), with a CoinGecko `genesis_date` fallback. Both are
keyless and CI-safe — any failure is skipped, never raises.

Writes cache/scam_tge.json: { "SYMBOL": "YYYY-MM-DDTHH:MM:SS.000Z", ... }

The data is static (a token's listing date never changes), so existing entries are
kept and only missing symbols are fetched — cheap to re-run when new watchlist
tokens are added.

Run: python fetch_scam_tge.py
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import requests

HERE = Path(__file__).parent
CACHE = HERE.parent / "cache"
DATA = CACHE / "scam_data.json"
OUT = CACHE / "scam_tge.json"

H = {"User-Agent": "Mozilla/5.0 verifysheet/scam-tge", "Accept": "application/json"}


def cmc_date_added(slug: str) -> str | None:
    try:
        r = requests.get("https://api.coinmarketcap.com/data-api/v3/cryptocurrency/detail",
                         params={"slug": slug}, headers=H, timeout=20)
        if r.status_code == 200:
            return ((r.json().get("data") or {}).get("dateAdded")) or None
    except Exception:
        pass
    return None


def cg_genesis(cg_id: str) -> str | None:
    try:
        r = requests.get(f"https://api.coingecko.com/api/v3/coins/{cg_id}",
                         params={"localization": "false", "tickers": "false",
                                 "market_data": "false", "community_data": "false",
                                 "developer_data": "false", "sparkline": "false"},
                         headers=H, timeout=20)
        if r.status_code == 200:
            g = r.json().get("genesis_date")
            return (g + "T00:00:00.000Z") if g else None
    except Exception:
        pass
    return None


def main() -> None:
    data = json.loads(DATA.read_text(encoding="utf-8"))
    out: dict = {}
    if OUT.exists():
        try:
            out = json.loads(OUT.read_text(encoding="utf-8"))
        except Exception:
            out = {}

    for sym, rec in data.items():
        if out.get(sym):                      # static — keep what we already have
            continue
        date = cmc_date_added(rec["cmc_slug"]) if rec.get("cmc_slug") else None
        if not date and rec.get("cg_id"):
            date = cg_genesis(rec["cg_id"])
            time.sleep(2.0)                   # CoinGecko is rate-limited
        if date:
            out[sym] = date
            print(f"  {sym}: {date}")
        else:
            print(f"  {sym}: no date")
        time.sleep(1.0)

    OUT.write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"wrote {OUT} ({len(out)} dates)")


if __name__ == "__main__":
    main()
