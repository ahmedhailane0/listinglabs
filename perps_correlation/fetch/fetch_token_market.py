"""Pull CURRENT FDV / market-cap / supply for every Listing-Reactions token from
CoinMarketCap, so the report's FDV matches CMC instead of drifting to a stale
hand-entered value.

Why this exists: each `listings/<token>.json` stored a one-off `fdv_usd` captured
on the day the token was added. Price moves, so the displayed FDV slowly diverges
from CMC (e.g. QAIT showed $199M while CMC read $290M). CMC's detail endpoint is
keyless and CI-safe, the same source `fetch_oi_cmc.py` already uses — so we refresh
FDV/MC/supply the same way OI is refreshed and let the builder prefer the live
values (falling back to the JSON when a token has no slug / a fetch fails).

Writes cache/token_market.json:
    { "fetched_utc": "...Z",
      "tokens": { "SYM": {price, fdv_usd, mcap_usd, circulating_supply,
                          total_supply, max_supply, date_launched, slug,
                          fetched_utc}, ... } }

Usage:  python fetch_token_market.py            # all reaction tokens
        python fetch_token_market.py qait linea # just these
"""
from __future__ import annotations

import json
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]   # perps_correlation/ (project root, NOT this subfolder)
LISTINGS = HERE / "listings"
OUT = HERE.parent / "cache" / "token_market.json"

DETAIL = "https://api.coinmarketcap.com/data-api/v3/cryptocurrency/detail?slug={slug}"
HEADERS = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}

PACE_S = 1.5          # base delay between tokens
MAX_RETRIES = 5       # per token, on "system busy" / errors
BACKOFF_S = 6.0       # grows each retry


def _get_json(url: str) -> dict | None:
    """One GET with CMC-throttle retry. Returns parsed payload or None."""
    for attempt in range(MAX_RETRIES):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=30) as r:
                payload = json.loads(r.read().decode("utf-8"))
        except Exception:
            time.sleep(BACKOFF_S * (attempt + 1))
            continue
        status = (payload.get("status") or {})
        if status.get("error_code") not in (0, "0", None):
            time.sleep(BACKOFF_S * (attempt + 1))
            continue
        return payload
    return None


def fetch_one(slug: str) -> dict:
    """Current price / FDV / market-cap / supply / launch date for a CMC slug."""
    payload = _get_json(DETAIL.format(slug=slug))
    if not payload:
        return {"error": "fetch failed"}
    data = payload.get("data") or {}
    st = data.get("statistics") or {}
    return {
        "price": st.get("price") or None,
        "fdv_usd": st.get("fullyDilutedMarketCap") or None,
        "mcap_usd": st.get("marketCap") or None,
        "circulating_supply": st.get("circulatingSupply") or None,
        "total_supply": st.get("totalSupply") or None,
        "max_supply": st.get("maxSupply") or None,
        # CMC's real launch date (the token's TGE) when present — distinct from
        # dateAdded (when CMC created the page, often long before launch).
        "date_launched": (data.get("dateLaunched") or None),
    }


def main(argv: list[str]) -> None:
    only = {a.lower() for a in argv}
    cfgs = sorted(LISTINGS.glob("*.json"))
    results: dict = {}
    if OUT.exists():
        try:
            results = json.loads(OUT.read_text(encoding="utf-8")).get("tokens", {})
        except Exception:
            results = {}

    total = len(cfgs)
    for i, p in enumerate(cfgs, 1):
        if only and p.stem.lower() not in only:
            continue
        cfg = json.loads(p.read_text(encoding="utf-8"))
        token = (cfg.get("token") or p.stem).upper()
        slug = cfg.get("cmc_slug")
        if not slug:
            print(f"[{i}/{total}] {token}: no cmc_slug", flush=True)
            continue

        res = fetch_one(slug)
        res["slug"] = slug
        res["fetched_utc"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        results[token] = res

        if "error" in res:
            print(f"[{i}/{total}] {token} ({slug}): ERROR {res['error']}", flush=True)
        else:
            fdv = res.get("fdv_usd")
            print(f"[{i}/{total}] {token} ({slug}): FDV "
                  f"{('${:,.0f}'.format(fdv)) if fdv else '—'}", flush=True)

        OUT.write_text(json.dumps(
            {"fetched_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
             "source": "CoinMarketCap web data-api (cryptocurrency/detail statistics)",
             "note": "CURRENT/live FDV/MC/supply snapshot",
             "tokens": results}, indent=2), encoding="utf-8")
        time.sleep(PACE_S)

    ok = sum(1 for r in results.values() if r.get("fdv_usd"))
    print(f"\nDONE. {ok} tokens with live FDV -> {OUT}", flush=True)


if __name__ == "__main__":
    main(sys.argv[1:])
