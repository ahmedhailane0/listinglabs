"""Enrich BWEnews-auto-listed tokens with the on-chain data their price chart
needs.

`build/apply_signals.py` creates a bare `listings/<sym>.json` from a BWEnews
headline (symbol / name / venue / time only — no contract or pool), tagged
`source:"bwenews-auto"`. Without a pool, `refresh_klines` can't fetch candles, so
the detail page has no chart. This step resolves each such token against the
keyless **Binance Alpha listings API** (real name, chain, contract, FDV, supply)
and its **GeckoTerminal top pool**, then MERGES that into the config without
touching the BWEnews venue events. `refresh_klines` populates candles on its
normal cycle afterwards and the chart appears.

Only tokens tagged `source:"bwenews-auto"` that still lack a `gecko_pool` are
touched; the marker is kept (provenance + keeps the neutral "awaiting data"
placeholder until candles arrive). Idempotent. Network best-effort: any failure
leaves the token unchanged for a later run and never raises — safe inside
build_all / CI (Binance's web `bapi` is reachable from CI; GeckoTerminal is one
call per token, retried next run if throttled).

    python fetch/enrich_autolisted.py            # enrich all pending
    python fetch/enrich_autolisted.py ARX CAP    # only these
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))  # lib./fetch./build./tools. importable
from tools.add_alpha_token import (alpha_index, gecko_pool, resolve_cmc_slug,
                                   CHAIN_MAP, _num, _iso)

HERE = Path(__file__).resolve().parents[1]   # perps_correlation/
LISTINGS = HERE / "listings"


def _pending(only: set[str]):
    """Yield (path, cfg) for auto-listed tokens that still need a pool."""
    for p in sorted(LISTINGS.glob("*.json")):
        try:
            cfg = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        if cfg.get("source") != "bwenews-auto" or cfg.get("gecko_pool"):
            continue
        if only and (cfg.get("token") or "").upper() not in only:
            continue
        yield p, cfg


def enrich(cfg: dict, rec: dict) -> bool:
    """Merge Alpha-API + pool data into cfg in place. False = couldn't (no
    contract/pool yet) so the caller leaves it for a later run."""
    chain_raw = (rec.get("chainName") or rec.get("chainId") or "").lower()
    chain = CHAIN_MAP.get(chain_raw, chain_raw)
    contract = rec.get("contractAddress")
    if not contract:
        return False
    pool = gecko_pool(chain, contract)
    if not pool:
        return False
    sym = cfg["token"]
    lt = rec.get("listingTime") or rec.get("onlineTime")
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    cfg["name"] = rec.get("name") or cfg.get("name") or sym      # Alpha name is authoritative
    cfg["chain"] = chain
    cfg["token_contract"] = contract
    cfg["gecko_pool"] = pool
    cfg["fdv_usd"] = _num(rec.get("fdv"))
    cfg["fdv_source"] = f"Binance Alpha listings API, {today} (current, not at-listing)"
    cfg["mcap_usd"] = _num(rec.get("marketCap"))
    cfg["circulating_supply"] = _num(rec.get("circulatingSupply"))
    cfg["total_supply"] = _num(rec.get("totalSupply"))
    cfg["cmc_slug"] = resolve_cmc_slug(sym, rec.get("name") or "")
    # add the precise Binance Alpha listing event if we don't already have one
    events = cfg.setdefault("events", [])
    if lt and not any((e.get("exchange") or "").startswith("Binance Alpha") for e in events):
        events.append({
            "exchange": "Binance Alpha",
            "iso_time_utc": _iso(lt),
            "note": (f"Trading start per Binance Alpha listings API "
                     f"(listingTime={lt}, contract on {rec.get('chainName')})."),
        })
        events.sort(key=lambda e: e.get("iso_time_utc") or "")
    # make sure the chart window covers the listing
    if lt:
        start = _iso(lt - 3600_000)
        if not cfg.get("window_start_utc") or start < cfg["window_start_utc"]:
            cfg["window_start_utc"] = start
    return True


def main(argv: list[str]) -> int:
    only = {a.upper() for a in argv if not a.startswith("-")}
    pend = list(_pending(only))
    if not pend:
        print("enrich_autolisted: nothing pending.")
        return 0
    try:
        idx = alpha_index()
    except Exception as e:
        print(f"enrich_autolisted: Alpha API unreachable ({e}); skipping (will retry next run).")
        return 0
    done = 0
    for p, cfg in pend:
        sym = (cfg.get("token") or "").upper()
        rec = idx.get(sym)
        if not rec:
            print(f"  {sym}: not in Binance Alpha universe — left as-is")
            continue
        try:
            if enrich(cfg, rec):
                p.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")
                done += 1
                print(f"  {sym}: enriched — name={cfg['name']!r} chain={cfg['chain']} "
                      f"pool=ok fdv={cfg['fdv_usd']}")
            else:
                print(f"  {sym}: no contract/pool yet — retry next run")
        except Exception as e:
            print(f"  {sym}: enrich error ({e}); left as-is")
    print(f"enrich_autolisted: {done} enriched.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
