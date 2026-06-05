"""Resolve EXACT listing-announcement article URLs per token+event, and cache
them so the report links each event timestamp straight to the right article.

Sources, in order of authority:
  * Local archives already built by sheet_verification/fetch_exchanges.py and
    cached under cache/ — Binance (spot+futures, with tickers), OKX listings,
    Upbit notices. These carry pre-extracted tickers + real article URLs.
  * Live feeds for venues not in those archives — Bybit, KuCoin.

Everything else (Coinbase, Kraken, Gate, Bitget, Bithumb, Coinone) falls back
to a site:-scoped web search in venues.announcement_url.

Collision guard: a candidate matches only if the symbol is in its extracted
tickers OR appears as a standalone token in the title (symbols <=2 chars also
require the project name). The chosen article's market type (spot vs perp) must
match the event, and among matches we take the closest publish date. The matched
title is cached for auditability.

    python fetch_announcements.py            # all tokens
    python fetch_announcements.py aero ctr   # subset

Writes cache/announcements.json: {slug: {event_label: {"url", "title", "date"}}}.
"date" is the article's publish timestamp (ISO), used to mark the announcement on
the chart's x-axis (distinct from the listing event time).
"""
from __future__ import annotations

import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

HERE = Path(__file__).parent
LISTINGS = HERE / "listings"
CACHE_DIR = HERE.parent / "cache"
CACHE = CACHE_DIR / "announcements.json"
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120"}

PERP_WORDS = ("perpetual", "perp", "futures", "-margined", "margined", "swap", "x-perp")

# A candidate must express an actual listing (not a wallet/convert/earn mention
# that merely contains the token name).
LISTING_OK = ("will list", "gets listed", "get listed", "listed on", "to list",
              "market support", "perpetual contract", "will launch", "will add",
              "has launched", "now available", "available on", "new listing",
              "listing of", "lists ", "launches ", "will be available")
LISTING_NEG = ("on convert", "web3 wallet", "wallet adds", "trading bot",
               "copy trading", "via ondo global", "tokenized", "trading competition",
               "airdrop", "trading bots",
               # non-listing notices the report must never show as a "listing
               # announcement" (kept conservative — only phrases that do NOT co-occur
               # with a genuine listing headline)
               "delist", "will be removed", "removal of", "seed tag", "monitoring tag",
               "leverage adjust", "margin tier", "maintenance", "will suspend",
               "suspension of", "simple earn", "to be delisted")


def listing_intent(title: str) -> bool:
    t = title.lower()
    return any(w in t for w in LISTING_OK) and not any(w in t for w in LISTING_NEG)


def parse_iso(t: str) -> datetime:
    return datetime.fromisoformat(t.replace("Z", "+00:00"))


def _ts_to_dt(ts) -> datetime | None:
    """Epoch -> UTC datetime, tolerant of seconds vs milliseconds (Bybit feeds
    ms ≈1.7e12, KuCoin feeds seconds ≈1.7e9; ÷1000 on the latter gives 1970)."""
    if not ts:
        return None
    try:
        v = int(ts)
    except (TypeError, ValueError):
        return None
    if v > 1e12:        # milliseconds
        v //= 1000
    return datetime.fromtimestamp(v, tz=timezone.utc)


def _day(s: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat(s).replace(tzinfo=timezone.utc) if s else None
    except Exception:
        return None


def is_perp(title: str) -> bool:
    t = title.lower()
    return any(w in t for w in PERP_WORDS)


def symbol_in(title: str, sym: str, name: str) -> bool:
    up = title.upper()
    pat = rf"(?<![A-Z0-9])(?:1000|10000)?{re.escape(sym)}(?=USDT|USDC|USD|KRW|/|\)|\(|\s|-|$)"
    if not re.search(pat, up):
        return False
    if len(sym) <= 2 and name and name.upper() not in up:
        return False
    return True


def matches(art: dict, sym: str, name: str) -> bool:
    if sym in [t.upper() for t in (art.get("tickers") or [])]:
        return True
    return symbol_in(art["title"], sym, name)


# ---- load local archives into a common shape {title,url,ts,tickers} ----

def _load(fname: str, date_key: str = "date", ticker_key: str = "tickers") -> list[dict]:
    p = CACHE_DIR / fname
    if not p.exists():
        return []
    raw = json.loads(p.read_text(encoding="utf-8"))
    out = []
    for a in raw:
        if not a.get("url"):
            continue
        tk = a.get(ticker_key)
        tickers = tk if isinstance(tk, list) else ([tk] if tk else [])
        out.append({"title": a.get("title", ""), "url": a["url"],
                    "ts": _day(a.get(date_key)), "tickers": tickers})
    return out


# ---- live feeds for venues not in the local archives ----

def bybit_feed() -> list[dict]:
    out, page = [], 1
    while page <= 15:
        try:
            r = requests.get("https://api.bybit.com/v5/announcements/index",
                             params={"locale": "en-US", "type": "new_crypto", "limit": 100, "page": page},
                             headers=UA, timeout=20)
            rows = r.json().get("result", {}).get("list") or []
            if not rows:
                break
            for a in rows:
                dtv = _ts_to_dt(a.get("dateTimestamp") or a.get("publishTime"))
                out.append({"title": a["title"], "url": a["url"], "ts": dtv, "tickers": []})
            page += 1
            time.sleep(0.2)
        except Exception:
            break
    return out


def kucoin_search(sym: str, name: str) -> list[dict]:
    out = []
    for kw in {name, sym}:
        if not kw:
            continue
        try:
            r = requests.get("https://www.kucoin.com/_api/cms/articles",
                             params={"page": 1, "pageSize": 20, "keyword": kw, "lang": "en_US"},
                             headers=UA, timeout=20)
            for a in (r.json().get("items") or []):
                dtv = _ts_to_dt(a.get("publish_ts") or a.get("publish_at"))
                out.append({"title": a["title"], "url": f"https://www.kucoin.com/announcement{a['path']}",
                            "ts": dtv, "tickers": []})
            time.sleep(0.2)
        except Exception:
            pass
    return out


def best_match(feed, sym, name, market, ev_dt):
    """market: 'spot' | 'perp' | 'alpha' | 'any'."""
    cands = []
    for a in feed:
        if not matches(a, sym, name):
            continue
        if not listing_intent(a["title"]):
            continue
        if market == "alpha" and "alpha" not in a["title"].lower():
            continue
        if market == "spot" and is_perp(a["title"]):
            continue
        if market == "perp" and not is_perp(a["title"]):
            continue
        cands.append(a)
    if not cands:
        return None
    cands.sort(key=lambda a: abs((a["ts"] - ev_dt).total_seconds()) if a["ts"] else 9e18)
    return cands[0]


# event label -> (feed key, market)
LABEL_SPEC = {
    "Binance Alpha": ("binance", "alpha"),
    "Binance Spot":  ("binance", "spot"),
    "Binance Perp":  ("binance", "perp"),
    "OKX Spot":      ("okx", "spot"),
    "OKX Perp":      ("okx", "perp"),
    "Upbit":         ("upbit", "any"),
    "Bybit Spot":    ("bybit", "spot"),
    "Bybit Perp":    ("bybit", "perp"),
    "Bybit Futures": ("bybit", "perp"),
    "KuCoin Spot":   ("kucoin", "spot"),
    "KuCoin Futures":("kucoin", "perp"),
}


def main():
    only = {a.lower() for a in sys.argv[1:] if not a.startswith("-")} or None
    cache = json.loads(CACHE.read_text(encoding="utf-8")) if CACHE.exists() and CACHE.stat().st_size else {}

    feeds = {
        "binance": _load("binance_all.json"),
        "okx": _load("okx_listings.json"),
        "upbit": _load("upbit_listings.json", ticker_key="ticker"),
        "bybit": bybit_feed(),
    }
    print(f"archives: binance={len(feeds['binance'])} okx={len(feeds['okx'])} "
          f"upbit={len(feeds['upbit'])} bybit={len(feeds['bybit'])}")

    n_links = 0
    for fp in sorted(LISTINGS.glob("*.json")):
        slug = fp.stem
        if only and slug not in only:
            continue
        cfg = json.loads(fp.read_text())
        sym, name = cfg["token"].upper(), cfg.get("name", "")
        feeds["kucoin"] = kucoin_search(sym, name)
        rec = {}
        for ev in cfg.get("events", []):
            spec = LABEL_SPEC.get(ev["exchange"])
            if not spec:
                continue
            fam, market = spec
            m = best_match(feeds[fam], sym, name, market, parse_iso(ev["iso_time_utc"]))
            if m:
                ts = m.get("ts")
                rec[ev["exchange"]] = {
                    "url": m["url"], "title": m["title"],
                    "date": ts.strftime("%Y-%m-%dT%H:%M:%SZ") if ts else None,
                }
        if rec:
            cache[slug] = rec
            n_links += len(rec)
            print(f"{slug:10} {len(rec):2} exact: {', '.join(rec)}")
        CACHE.write_text(json.dumps(cache, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"\nWrote {CACHE} — {n_links} exact links")


if __name__ == "__main__":
    main()
