"""Scaffold a new Binance Alpha token into the Listing Reactions report.

Adding a token used to be hand-authored JSON. Almost everything is actually
discoverable from keyless endpoints, so this writes a ready-to-build
`listings/<sym>.json` from just the ticker:

  - Binance Alpha listings API -> name, chain, contract, listingTime, fdv, mcap,
    circulating/total supply (same source as funnel/build_funnel.py).
  - GeckoTerminal token->pools -> the `gecko_pool` (top pool by liquidity).
  - CoinMarketCap detail -> best-effort `cmc_slug` (verified by symbol match;
    left null if no confident match — it only feeds the later OI/supply refresh).

It seeds ONE "Binance Alpha" event at the listing time and a default chart
window. Other venues (incl. the Binance perp) are filled afterward by the normal
pipeline: refresh_klines -> listing_chart -> sweep_venues/merge_sweep ->
build_all (apply_signals folds BWEnews perp announcements).

    python add_alpha_token.py ZEST BABYSHARK PHAROS SHARE ACN
    python add_alpha_token.py ZEST --force   # overwrite an existing config
"""
from __future__ import annotations

import json
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from fetch_oi_cmc import DETAIL, HEADERS

HERE = Path(__file__).resolve().parent
LISTINGS = HERE / "listings"

ALPHA_URL = ("https://www.binance.com/bapi/defi/v1/public/wallet-direct/buw/"
             "wallet/cex/alpha/all/token/list")
GT_POOLS = "https://api.geckoterminal.com/api/v2/networks/{net}/tokens/{addr}/pools?page=1"

# Binance `chainName` -> the value we store in `chain` (also the GeckoTerminal
# network id). GeckoTerminal calls Ethereum "eth", not "ethereum".
CHAIN_MAP = {"bsc": "bsc", "base": "base", "ethereum": "eth", "eth": "eth",
             "solana": "solana", "arbitrum": "arbitrum", "linea": "linea",
             "tron": "tron", "sui": "sui"}


def _get(url: str):
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def _iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _num(v):
    """Alpha API returns fdv/mcap/supply as strings; store them as numbers
    (configs elsewhere are numeric)."""
    if v in (None, "", "null"):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def alpha_index() -> dict:
    """symbol(upper) -> Alpha record."""
    data = _get(ALPHA_URL)["data"]
    return {(t.get("symbol") or "").upper(): t for t in data}


def gecko_pool(net: str, addr: str) -> str | None:
    """Top pool address (bare, no net prefix) for a token contract, or None."""
    try:
        pools = _get(GT_POOLS.format(net=net, addr=addr)).get("data") or []
    except Exception as e:
        print(f"    gecko pools error: {e}")
        return None
    if not pools:
        return None
    pid = pools[0].get("id") or ""          # e.g. "bsc_0xabc…"
    return pid.split("_", 1)[1] if "_" in pid else pid


def resolve_cmc_slug(sym: str, name: str) -> str | None:
    """Best-effort cmc_slug: try name/symbol-derived slugs, keep the first whose
    CMC detail symbol matches. None if no confident match. Uses a single direct
    GET per candidate (a 404 means "not this slug" — fail fast, no throttle
    backoff)."""
    cands = []
    for s in (name, sym):
        if s:
            cands.append(s.lower().replace(" ", "-").replace(".", "").replace("'", ""))
    seen, ordered = set(), []
    for c in cands:                          # de-dupe, preserve order
        if c and c not in seen:
            seen.add(c); ordered.append(c)
    for slug in ordered:
        try:
            payload = _get(DETAIL.format(slug=slug))
        except Exception:
            time.sleep(0.5)
            continue
        data = (payload or {}).get("data") or {}
        if data and (data.get("symbol") or "").upper() == sym.upper():
            return slug
        time.sleep(0.5)
    return None


def build_config(sym: str, rec: dict) -> dict:
    chain_raw = (rec.get("chainName") or rec.get("chainId") or "").lower()
    chain = CHAIN_MAP.get(chain_raw, chain_raw)
    contract = rec.get("contractAddress")
    lt = rec.get("listingTime") or rec.get("onlineTime")
    pool = gecko_pool(chain, contract) if contract else None
    slug = resolve_cmc_slug(sym, rec.get("name") or "")
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    cfg = {
        "token": sym,
        "name": rec.get("name") or sym,
        "chain": chain,
        "token_contract": contract,
        "gecko_pool": pool,
        "fdv_usd": _num(rec.get("fdv")),
        "fdv_source": f"Binance Alpha listings API, {today} (current, not at-listing)",
        "mcap_usd": _num(rec.get("marketCap")),
        "circulating_supply": _num(rec.get("circulatingSupply")),
        "total_supply": _num(rec.get("totalSupply")),
        "cmc_slug": slug,
        "category": "",
        "new": True,                          # NEW badge in the report; clear when no longer recent
        "window_start_utc": _iso(lt - 3600_000) if lt else None,
        "window_end_utc": _iso(lt + 48 * 3600_000) if lt else None,
        "events": [],
        "annotations": [],
        "not_listed": [],
    }
    if lt:
        cfg["events"].append({
            "exchange": "Binance Alpha",
            "iso_time_utc": _iso(lt),
            "note": f"Trading start per Binance Alpha listings API "
                    f"(listingTime={lt}, contract on {rec.get('chainName')}).",
        })
    return cfg


def main(argv) -> int:
    force = "--force" in argv
    syms = [a.upper() for a in argv if not a.startswith("-")]
    if not syms:
        print("usage: python add_alpha_token.py SYM [SYM ...] [--force]")
        return 1
    idx = alpha_index()
    LISTINGS.mkdir(exist_ok=True)
    written = 0
    for sym in syms:
        out = LISTINGS / f"{sym.lower()}.json"
        if out.exists() and not force:
            print(f"{sym:10} skip — {out.name} exists (use --force)")
            continue
        rec = idx.get(sym)
        if not rec:
            print(f"{sym:10} NOT FOUND in Binance Alpha universe")
            continue
        cfg = build_config(sym, rec)
        out.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")
        written += 1
        print(f"{sym:10} -> {out.name}  chain={cfg['chain']} pool={'ok' if cfg['gecko_pool'] else 'MISSING'} "
              f"cmc_slug={cfg['cmc_slug']} fdv={cfg['fdv_usd']}", flush=True)
    print(f"\nwrote {written} config(s). Next: refresh_klines.py {' '.join(s.lower() for s in syms)}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
