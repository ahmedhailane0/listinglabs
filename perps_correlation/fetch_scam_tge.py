"""Fetch a TGE date (token generation / real launch) per Manipulated-watchlist
token, so the report can show + sort by it like the Listing Reactions report's
TGE column.

Source priority (all keyless + CI-safe; any failure is skipped, never raises):
  1. a hand-curated OVERRIDE for tokens whose true TGE no API exposes cleanly;
  2. CoinMarketCap `dateLaunched` — the token's REAL launch date;
  3. CoinGecko `genesis_date`;
  4. CoinMarketCap `dateAdded` — LAST resort only.

Why not `dateAdded` first (the old behaviour): `dateAdded` is when CMC *created
the page*, which is routinely months-to-years before the token launches (e.g. CMC
had LINEA's page in 2023-07 but LINEA launched 2025-09; Monad's in 2024-07 but it
launched 2025-11). That made the TGE column "completely wrong" for many tokens.
`dateLaunched` is the actual launch and fixes the bulk of them.

Writes cache/scam_tge.json: { "SYMBOL": "YYYY-MM-DDTHH:MM:SS.000Z", ... }

The data is static (a token's launch never changes), so existing entries are kept
and only missing symbols are fetched — cheap to re-run when new watchlist tokens
are added. Use `--force` to recompute every token (needed once after changing the
source priority so the old dateAdded values get replaced).

Run: python fetch_scam_tge.py [--force]
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import requests

HERE = Path(__file__).parent
CACHE = HERE.parent / "cache"
DATA = CACHE / "scam_data.json"
OUT = CACHE / "scam_tge.json"

H = {"User-Agent": "Mozilla/5.0 verifysheet/scam-tge", "Accept": "application/json"}

# Hand-curated true TGE dates for tokens where CMC exposes no `dateLaunched` and
# `dateAdded` is wrong (it predates the real launch). Verified from public launch
# records. Keyed by watchlist symbol.
OVERRIDES = {
    "HYPE": "2024-11-29T00:00:00.000Z",   # Hyperliquid TGE / airdrop
    "MON":  "2025-11-24T00:00:00.000Z",   # Monad mainnet + MON token TGE
    "AERO": "2023-08-28T00:00:00.000Z",   # Aerodrome Finance launch on Base
    # No CMC slug in scam_data, or CMC's dateLaunched is the project-founding date
    # (not the token TGE) / a same-symbol collision. Dates verified from the launch
    # announcements + the correct CMC slug.
    "H":      "2025-06-25T00:00:00.000Z",  # Humanity Protocol TGE (Binance Alpha 06-25)
    "NAORIS": "2025-07-31T00:00:00.000Z",  # Naoris Protocol TGE
    "STABLE": "2025-11-06T00:00:00.000Z",  # Stable (CMC slug "stable")
    "GWEI":   "2026-01-21T00:00:00.000Z",  # ETHGas (CMC slug "eth-gas", dateLaunched)
    "SIREN":  "2025-02-07T00:00:00.000Z",  # Siren (CMC slug "siren-bsc", dateLaunched)
    "GENIUS": "2026-04-13T00:00:00.000Z",  # Genius Terminal TGE (Binance Alpha 04-13)
}


def cmc_dates(slug: str) -> dict:
    """Return {'launched': iso|None, 'added': iso|None} from CMC's detail page."""
    try:
        r = requests.get("https://api.coinmarketcap.com/data-api/v3/cryptocurrency/detail",
                         params={"slug": slug}, headers=H, timeout=20)
        if r.status_code == 200:
            d = r.json().get("data") or {}
            return {"launched": d.get("dateLaunched") or None,
                    "added": d.get("dateAdded") or None}
    except Exception:
        pass
    return {"launched": None, "added": None}


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
    force = "--force" in sys.argv
    data = json.loads(DATA.read_text(encoding="utf-8"))
    out: dict = {}
    if OUT.exists() and not force:
        try:
            out = json.loads(OUT.read_text(encoding="utf-8"))
        except Exception:
            out = {}

    for sym, rec in data.items():
        if out.get(sym) and not force:        # static — keep what we already have
            continue
        # 1) curated override wins
        date = OVERRIDES.get(sym)
        src = "override"
        if not date and rec.get("cmc_slug"):
            d = cmc_dates(rec["cmc_slug"])
            # 2) real launch date
            if d["launched"]:
                date, src = d["launched"], "dateLaunched"
            else:
                # 3) CoinGecko genesis, then 4) dateAdded (last resort)
                if rec.get("cg_id"):
                    g = cg_genesis(rec["cg_id"])
                    time.sleep(2.0)           # CoinGecko is rate-limited
                    if g:
                        date, src = g, "cg_genesis"
                if not date and d["added"]:
                    date, src = d["added"], "dateAdded(fallback)"
        elif not date and rec.get("cg_id"):
            g = cg_genesis(rec["cg_id"])
            time.sleep(2.0)
            if g:
                date, src = g, "cg_genesis"
        if date:
            out[sym] = date
            print(f"  {sym}: {date[:10]}  ({src})")
        else:
            print(f"  {sym}: no date")
        time.sleep(1.0)

    OUT.write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"wrote {OUT} ({len(out)} dates)")


if __name__ == "__main__":
    main()
