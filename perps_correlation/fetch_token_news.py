"""Fetch recent per-token NEWS from CoinMarketCap's KEYLESS content API.

Builds `cache/token_news.json`, consumed by `news_panel.render()` on both
reports' detail pages (server-side rendered — the static site makes no client
API calls; CSP blocks those anyway).

SOURCE (keyless; send User-Agent: Mozilla/5.0):
  1. Resolve a CMC numeric id from a cmc_slug:
       GET https://api.coinmarketcap.com/data-api/v3/cryptocurrency/detail/lite?slug=<slug>
       -> .data.id (int)
  2. News for that id:
       GET https://api.coinmarketcap.com/content/v3/news?coins=<id>&page=1&size=20
       -> .data = [ {slug, cover, assets, createdAt, meta{title,sourceName,sourceUrl,releasedAt}}, ... ]

TOKEN UNIVERSE (union, dedup by UPPER symbol):
  • Reactions tokens: perps_correlation/listings/*.json  (token + cmc_slug)
  • Scam tokens:      cache/scam_data.json values         (symbol + cmc_slug)

OUTPUT SCHEMA — cache/token_news.json (UTF-8, ensure_ascii=False, indent=2):
  {
    "_ids":  { "<SLUG>": <cmcId int>, ... },     # slug->id resolution cache
    "<SYMBOL>": [
       {"title": str, "url": str, "source": str, "ts": int},   # newest first
       ...
    ],
    ...
  }
  ts = unix seconds (UTC). ~15-20 newest items per token.

Re-runnable: refetches news each run but reuses cached slug->id resolutions so
reruns don't re-resolve. Each token is wrapped in try/except so one failure never
aborts the whole run. Env NEWS_LIMIT caps how many tokens are processed per run.
"""
from __future__ import annotations

import glob
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

HERE = Path(__file__).parent
REPO = HERE.parent
CACHE_DIR = REPO / "cache"
OUT = CACHE_DIR / "token_news.json"
LISTINGS = HERE / "listings"
SCAM_DATA = CACHE_DIR / "scam_data.json"

UA = {"User-Agent": "Mozilla/5.0"}
DETAIL_URL = "https://api.coinmarketcap.com/data-api/v3/cryptocurrency/detail/lite"
NEWS_URL = "https://api.coinmarketcap.com/content/v3/news"
SLEEP = 0.3          # polite pause between network calls
ITEMS_PER_TOKEN = 18
TIMEOUT = 20


def _collect_tokens() -> dict:
    """Return {SYMBOL: cmc_slug-or-None}, union of both universes, dedup by symbol."""
    tokens: dict[str, str | None] = {}

    for f in sorted(glob.glob(str(LISTINGS / "*.json"))):
        try:
            d = json.load(open(f, encoding="utf-8"))
        except Exception:
            continue
        sym = (d.get("token") or "").strip().upper()
        if not sym:
            continue
        slug = d.get("cmc_slug")
        # don't overwrite an existing slug with None
        if sym not in tokens or (tokens[sym] is None and slug):
            tokens[sym] = slug

    if SCAM_DATA.exists():
        try:
            scams = json.load(open(SCAM_DATA, encoding="utf-8"))
        except Exception:
            scams = {}
        for v in scams.values():
            if not isinstance(v, dict):
                continue
            sym = (v.get("symbol") or "").strip().upper()
            if not sym:
                continue
            slug = v.get("cmc_slug")
            if sym not in tokens or (tokens[sym] is None and slug):
                tokens[sym] = slug

    return tokens


def _parse_ts(item: dict) -> int:
    """ts (unix seconds, UTC) from meta.releasedAt, falling back to createdAt.

    Both fields can be either an ISO-8601 string or epoch-ms — handle both.
    """
    meta = item.get("meta") or {}
    for raw in (meta.get("releasedAt"), item.get("createdAt")):
        if raw is None:
            continue
        # epoch milliseconds
        if isinstance(raw, (int, float)):
            return int(raw / 1000)
        if isinstance(raw, str):
            s = raw.strip()
            if not s:
                continue
            if s.isdigit():
                return int(int(s) / 1000)
            try:
                dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return int(dt.timestamp())
            except Exception:
                continue
    return 0


def _resolve_id(slug: str, ids: dict) -> int | None:
    """Resolve (and cache) a CMC numeric id from a cmc_slug. None on failure."""
    if not slug:
        return None
    if slug in ids and ids[slug]:
        return ids[slug]
    try:
        r = requests.get(DETAIL_URL, params={"slug": slug}, headers=UA, timeout=TIMEOUT)
        time.sleep(SLEEP)
        if r.status_code != 200:
            return None
        cid = (r.json().get("data") or {}).get("id")
        if isinstance(cid, int):
            ids[slug] = cid
            return cid
    except Exception:
        return None
    return None


def _fetch_news(cid: int) -> list[dict]:
    """Fetch + normalize news items for a CMC id, newest-first."""
    try:
        r = requests.get(
            NEWS_URL,
            params={"coins": cid, "page": 1, "size": 20},
            headers=UA,
            timeout=TIMEOUT,
        )
        time.sleep(SLEEP)
        if r.status_code != 200:
            return []
        data = r.json().get("data") or []
    except Exception:
        return []

    out: list[dict] = []
    for it in data:
        meta = it.get("meta") or {}
        title = (meta.get("title") or "").strip()
        url = (meta.get("sourceUrl") or "").strip()
        source = (meta.get("sourceName") or "").strip()
        if not title or not url:
            continue
        out.append({
            "title": title,
            "url": url,
            "source": source,
            "ts": _parse_ts(it),
        })
    out.sort(key=lambda x: x["ts"], reverse=True)
    return out[:ITEMS_PER_TOKEN]


def main() -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)

    # carry forward previously-resolved slug->id pairs
    ids: dict = {}
    if OUT.exists():
        try:
            prev = json.load(open(OUT, encoding="utf-8"))
            if isinstance(prev.get("_ids"), dict):
                ids = {k: v for k, v in prev["_ids"].items() if isinstance(v, int)}
        except Exception:
            ids = {}

    tokens = _collect_tokens()
    items = sorted(tokens.items())  # deterministic order

    limit = os.environ.get("NEWS_LIMIT")
    if limit:
        try:
            items = items[: int(limit)]
        except ValueError:
            pass

    result: dict = {}
    n_news = 0
    n_empty = 0

    for sym, slug in items:
        try:
            cid = _resolve_id(slug, ids) if slug else None
            news = _fetch_news(cid) if cid else []
            if news:
                result[sym] = news
                n_news += 1
            else:
                n_empty += 1
        except Exception:
            n_empty += 1
            continue

    result["_ids"] = ids
    with open(OUT, "w", encoding="utf-8") as fh:
        json.dump(result, fh, ensure_ascii=False, indent=2)

    print(f"token_news: {n_news} tokens with news, {n_empty} empty -> {OUT}")


if __name__ == "__main__":
    main()
