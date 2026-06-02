"""Pull current open interest from CoinMarketCap for every report token.

CMC's web data-api exposes per-exchange perpetual market pairs with an
`openInterestUsd` field. There's no free aggregate endpoint, so total OI =
sum of `openInterestUsd` across all perp pairs (pagination is complete when
`returned == numMarketPairs`).

This is a CURRENT/LIVE snapshot, not OI-at-listing — each record is stamped
with the fetch time. Results are written incrementally to oi_cmc.json so a
mid-run throttle doesn't cost the whole run.

Usage:  python fetch_oi_cmc.py
"""
from __future__ import annotations

import json
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
LISTINGS = HERE / "listings"
OUT = HERE / "oi_cmc.json"

API = ("https://api.coinmarketcap.com/data-api/v3/cryptocurrency/"
       "market-pairs/latest?slug={slug}&category=perpetual&limit=500")
DETAIL = "https://api.coinmarketcap.com/data-api/v3/cryptocurrency/detail?slug={slug}"
HEADERS = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}

PACE_S = 2.0          # base delay between tokens
MAX_RETRIES = 5       # per token, on "system busy" / errors
BACKOFF_S = 8.0       # grows each retry


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


def fetch_mcap(slug: str) -> dict:
    """Current market cap + FDV from CMC's detail endpoint (same-dated as OI)."""
    payload = _get_json(DETAIL.format(slug=slug))
    if not payload:
        return {}
    st = (payload.get("data") or {}).get("statistics") or {}
    return {"mcap_now_usd": st.get("marketCap") or None,
            "fdv_now_usd": st.get("fullyDilutedMarketCap") or None}


def fetch_one(slug: str) -> dict:
    """Return {oi_usd, n_pairs, num_market_pairs, top, complete} for a slug,
    or {error: ...}.  Retries on CMC throttling."""
    url = API.format(slug=slug)
    last_err = ""
    for attempt in range(MAX_RETRIES):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=30) as r:
                payload = json.loads(r.read().decode("utf-8"))
        except Exception as e:  # network / json
            last_err = f"{type(e).__name__}: {e}"
            time.sleep(BACKOFF_S * (attempt + 1))
            continue

        status = (payload.get("status") or {})
        if status.get("error_code") not in (0, "0", None):
            last_err = status.get("error_message") or "unknown status error"
            time.sleep(BACKOFF_S * (attempt + 1))
            continue

        data = payload.get("data") or {}
        pairs = data.get("marketPairs") or []
        num = data.get("numMarketPairs")
        if not pairs:
            # genuinely no perp markets (vs. throttle, which returns empty data
            # with a busy message handled above)
            return {"oi_usd": None, "n_pairs": 0, "num_market_pairs": num,
                    "complete": True, "top": []}

        oi = sum(p.get("openInterestUsd") or 0 for p in pairs)
        top = sorted(
            ({"exchange": p.get("exchangeName"), "pair": p.get("marketPair"),
              "oi_usd": p.get("openInterestUsd") or 0} for p in pairs),
            key=lambda x: x["oi_usd"], reverse=True)[:5]
        return {"oi_usd": oi, "n_pairs": len(pairs), "num_market_pairs": num,
                "complete": (num is None or len(pairs) >= num), "top": top}

    return {"error": last_err}


def main() -> None:
    cfgs = sorted(LISTINGS.glob("*.json"))
    results: dict = {}
    if OUT.exists():
        results = json.loads(OUT.read_text(encoding="utf-8")).get("tokens", {})

    total = len(cfgs)
    for i, p in enumerate(cfgs, 1):
        cfg = json.loads(p.read_text(encoding="utf-8"))
        token = (cfg.get("token") or p.stem).upper()
        slug = cfg.get("cmc_slug")
        if not slug:
            results[token] = {"slug": None, "error": "no cmc_slug"}
            print(f"[{i}/{total}] {token}: no cmc_slug", flush=True)
            continue

        res = fetch_one(slug)
        res["slug"] = slug
        res["fetched_utc"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        # current mcap/FDV from CMC, same-dated as OI -> honest OI% of mcap
        res.update(fetch_mcap(slug))
        oi, mc = res.get("oi_usd"), res.get("mcap_now_usd")
        res["oi_pct_mcap"] = (oi / mc * 100) if (oi and mc) else None
        results[token] = res

        if "error" in res:
            print(f"[{i}/{total}] {token} ({slug}): ERROR {res['error']}", flush=True)
        elif res["oi_usd"] is None:
            print(f"[{i}/{total}] {token} ({slug}): no perp markets", flush=True)
        else:
            flag = "" if res["complete"] else "  !!INCOMPLETE"
            pct = res["oi_pct_mcap"]
            pcts = f" = {pct:.0f}% mcap" if pct is not None else ""
            print(f"[{i}/{total}] {token} ({slug}): OI ${res['oi_usd']:,.0f}{pcts} "
                  f"across {res['n_pairs']} pairs{flag}", flush=True)

        # write incrementally
        OUT.write_text(json.dumps(
            {"fetched_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
             "source": "CoinMarketCap web data-api (perpetual market pairs, summed openInterestUsd)",
             "note": "CURRENT/live OI snapshot, not OI-at-listing",
             "tokens": results}, indent=2), encoding="utf-8")
        time.sleep(PACE_S)

    ok = sum(1 for r in results.values() if r.get("oi_usd"))
    none_ = sum(1 for r in results.values() if r.get("oi_usd") is None and "error" not in r and r.get("slug"))
    err = sum(1 for r in results.values() if "error" in r or not r.get("slug"))
    print(f"\nDONE. {ok} with OI, {none_} no-perp, {err} error/no-slug. -> {OUT}",
          flush=True)


if __name__ == "__main__":
    main()
