"""Build cache/token_unlocks.json from DefiLlama's FREE open dataset bucket.

Source (all keyless, HTTP 200 — the paid api.llama.fi/emissions endpoints 402, so
we use the open dataset bucket the DefiLlama frontend itself reads):
  - https://defillama-datasets.llama.fi/emissionsProtocolsList  -> [slug, ...]
  - https://defillama-datasets.llama.fi/emissions/<slug>        -> per-protocol JSON
  - https://coins.llama.fi/prices/current/coingecko:<id>,...    -> current prices

Per-protocol unlock schema discovered:
  d["documentedData"]["data"] = [ {"label": <allocation category>,
                                   "data": [ {"timestamp": int,
                                              "unlocked": float (CUMULATIVE tokens),
                                              "rawEmission": float,
                                              "burned": float}, ... ] }, ... ]
  -> diffing consecutive `unlocked` per category gives the per-day token unlock.
  d["metadata"]["total"] / d["supplyMetrics"]["maxSupply"] = total supply (for %).
  d["gecko_id"] = CoinGecko id (best match key).
  unlockUsdChart is PAST-only / often empty, so USD is computed = tokens * price.

We collapse the future per-day deltas into one event PER CALENDAR MONTH (dominant
allocation category as the label), which yields clean discrete rows for both
linear-vesting tokens (e.g. MORPHO) and cliff tokens (e.g. ONDO).

Matching tokens -> protocol:
  1. scam tokens: cg_id == protocol gecko_id (exact, most reliable).
  2. fallback: symbol or lowercased name == protocol symbol / gecko_id / name
     (conservative exact match only, to avoid false positives).

Output cache/token_unlocks.json keyed by UPPER symbol:
  { "AAVE": {"next_unlock": {...}, "events": [...], "source_url": "..."} }
Only tokens that matched AND have >=1 future unlock event (ts > now).
"""
from __future__ import annotations

import glob
import json
import os
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent
REPO = ROOT.parent
CACHE_DIR = REPO / "cache"
PROTO_CACHE_DIR = CACHE_DIR / "_unlocks_protocols"  # per-protocol raw dataset cache
OUT = CACHE_DIR / "token_unlocks.json"

DATASET = "https://defillama-datasets.llama.fi"
PRICES = "https://coins.llama.fi/prices/current/"
UA = {"User-Agent": "Mozilla/5.0"}

NOW = int(time.time())


def _get_json(url, timeout=30):
    r = requests.get(url, headers=UA, timeout=timeout)
    r.raise_for_status()
    return r.json()


def load_protocols_list():
    return _get_json(f"{DATASET}/emissionsProtocolsList")


def fetch_protocol(slug):
    """Fetch one protocol's emissions dataset, caching the raw JSON to disk."""
    PROTO_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    fp = PROTO_CACHE_DIR / f"{slug}.json"
    if fp.exists():
        try:
            return json.loads(fp.read_text(encoding="utf-8"))
        except Exception:
            pass
    d = _get_json(f"{DATASET}/emissions/{slug}")
    try:
        fp.write_text(json.dumps(d), encoding="utf-8")
    except Exception:
        pass
    return d


def future_events(d):
    """Return (events, total_supply) where events are future monthly unlocks.

    events: list of {"ts","tokens","pct_supply","label"} (usd filled later),
    soonest first. ts = first day of that month with an unlock.
    """
    dd = (d.get("documentedData") or {}).get("data") or []
    total = (d.get("metadata") or {}).get("total") or (
        d.get("supplyMetrics") or {}
    ).get("maxSupply")

    # month key -> {category -> tokens}, and month key -> earliest ts in month
    by_month = defaultdict(lambda: defaultdict(float))
    month_ts = {}
    for cat in dd:
        label = cat.get("label")
        series = cat.get("data") or []
        prev = None
        for p in series:
            ts = p.get("timestamp")
            cum = p.get("unlocked")
            if ts is None or cum is None:
                prev = cum
                continue
            if prev is not None:
                delta = cum - prev
                if ts > NOW and delta > 1e-6:
                    dt = datetime.fromtimestamp(ts, timezone.utc)
                    mk = (dt.year, dt.month)
                    by_month[mk][label] += delta
                    if mk not in month_ts or ts < month_ts[mk]:
                        month_ts[mk] = ts
            prev = cum

    events = []
    for mk in sorted(by_month):
        cats = by_month[mk]
        tokens = sum(cats.values())
        if tokens <= 0:
            continue
        dom = max(cats, key=cats.get) if cats else None
        pct = (tokens / total * 100.0) if total else None
        events.append(
            {
                "ts": int(month_ts[mk]),
                "tokens": float(tokens),
                "usd": None,
                "pct_supply": (round(pct, 4) if pct is not None else None),
                "label": dom,
            }
        )
    events.sort(key=lambda e: e["ts"])
    return events, total


def fetch_prices(gecko_ids):
    """Batch current prices keyed by gecko id. Returns {gecko_id: (price, symbol)}."""
    out = {}
    ids = [g for g in gecko_ids if g]
    CHUNK = 80
    for i in range(0, len(ids), CHUNK):
        chunk = ids[i : i + CHUNK]
        keys = ",".join(f"coingecko:{g}" for g in chunk)
        try:
            d = _get_json(PRICES + keys)
        except Exception:
            continue
        for k, v in (d.get("coins") or {}).items():
            gid = k.split(":", 1)[1] if ":" in k else k
            out[gid] = (v.get("price"), v.get("symbol"))
        time.sleep(0.1)
    return out


def load_token_universe():
    """Return list of ident dicts {symbol, name, cmc_slug, cg_id} (cg_id may be '')."""
    tokens = []
    seen = set()

    # Reactions tokens (no cg_id)
    for f in glob.glob(str(ROOT / "listings" / "*.json")):
        try:
            d = json.loads(Path(f).read_text(encoding="utf-8"))
        except Exception:
            continue
        sym = (d.get("token") or "").strip()
        if not sym:
            continue
        key = sym.upper()
        if key in seen:
            continue
        seen.add(key)
        tokens.append(
            {
                "symbol": sym,
                "name": d.get("name") or "",
                "cmc_slug": d.get("cmc_slug") or "",
                "cg_id": "",
            }
        )

    # Scam tokens (have cg_id)
    try:
        sd = json.loads((CACHE_DIR / "scam_data.json").read_text(encoding="utf-8"))
    except Exception:
        sd = {}
    for v in sd.values():
        sym = (v.get("symbol") or "").strip()
        if not sym:
            continue
        key = sym.upper()
        ident = {
            "symbol": sym,
            "name": v.get("name") or "",
            "cmc_slug": v.get("cmc_slug") or "",
            "cg_id": v.get("cg_id") or "",
        }
        if key in seen:
            # enrich an existing reactions entry with cg_id if it lacks one
            for t in tokens:
                if t["symbol"].upper() == key and not t["cg_id"] and ident["cg_id"]:
                    t["cg_id"] = ident["cg_id"]
            continue
        seen.add(key)
        tokens.append(ident)
    return tokens


def main():
    limit = os.environ.get("UNLOCKS_LIMIT")
    slugs = load_protocols_list()
    if limit:
        slugs = slugs[: int(limit)]
    print(f"token_unlocks: {len(slugs)} protocols to scan")

    # Build the protocol index: gecko_id -> entry, lowercased name -> entry,
    # symbol(upper) -> entry. Each entry has slug, events, total, gecko_id, name.
    by_gecko = {}
    by_name = {}
    by_symbol = {}
    fetched = 0
    for i, slug in enumerate(slugs):
        try:
            d = fetch_protocol(slug)
        except Exception as e:
            print(f"  skip {slug}: {e}")
            time.sleep(0.15)
            continue
        fetched += 1
        events, total = future_events(d)
        if not events:
            continue
        gecko = (d.get("gecko_id") or "").strip().lower()
        name = (d.get("name") or "").strip().lower()
        entry = {
            "slug": slug,
            "events": events,
            "total": total,
            "gecko_id": gecko,
            "name": name,
        }
        if gecko:
            by_gecko[gecko] = entry
        if name:
            by_name[name] = entry
        # symbol from metadata.token "coingecko:xxx" isn't a symbol; symbol comes
        # from the price api later. We index by name/gecko here; symbol fallback
        # is resolved via the price-api symbol below.
        # only sleep on real network fetches (cache hits are instant)
        if not (PROTO_CACHE_DIR / f"{slug}.json").exists():
            time.sleep(0.15)

    # Resolve symbols for all indexed protocols via the price api (also gives price).
    all_geckos = sorted({e["gecko_id"] for e in by_gecko.values() if e["gecko_id"]})
    prices = fetch_prices(all_geckos)  # {gecko: (price, symbol)}
    for gid, (price, symbol) in prices.items():
        if gid in by_gecko:
            by_gecko[gid]["price"] = price
            if symbol:
                by_symbol[symbol.strip().upper()] = by_gecko[gid]

    print(
        f"token_unlocks: indexed {len(by_gecko)} protocols with future unlocks "
        f"(scanned {fetched})"
    )

    # Join the token universe against the index.
    tokens = load_token_universe()
    out = {}
    matched = 0
    for t in tokens:
        sym_u = t["symbol"].strip().upper()
        cg = (t["cg_id"] or "").strip().lower()
        name_l = (t["name"] or "").strip().lower()

        entry = None
        # 1. exact gecko_id (most reliable)
        if cg and cg in by_gecko:
            entry = by_gecko[cg]
        # 2. exact symbol from price api
        elif sym_u in by_symbol:
            entry = by_symbol[sym_u]
        # 3. exact gecko_id == cmc_slug-ish / name match (conservative)
        elif name_l and name_l in by_name:
            entry = by_name[name_l]
        elif cg and cg in by_name:  # cg sometimes equals lowercased name
            entry = by_name[cg]

        if not entry:
            continue
        matched += 1

        price = entry.get("price")
        events = []
        for e in entry["events"]:
            usd = None
            if price and e.get("tokens"):
                usd = float(e["tokens"]) * float(price)
            events.append(
                {
                    "ts": e["ts"],
                    "tokens": round(e["tokens"], 2) if e.get("tokens") else None,
                    "usd": round(usd, 2) if usd is not None else None,
                    "pct_supply": e.get("pct_supply"),
                    "label": e.get("label"),
                }
            )
        if not events:
            continue
        out[sym_u] = {
            "next_unlock": events[0],
            "events": events,
            "source_url": f"https://defillama.com/unlocks/{entry['slug']}",
        }

    with_future = len(out)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    OUT.write_text(
        json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(
        f"token_unlocks: matched {matched} tokens, {with_future} with future "
        f"unlocks -> {OUT}"
    )
    if out:
        print("  e.g.", ", ".join(sorted(out)[:12]))


if __name__ == "__main__":
    main()
