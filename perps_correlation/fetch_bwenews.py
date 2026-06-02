"""Poll the BWEnews public RSS feed and turn listing headlines into structured
signals the ListingLabs build can use.

Free, keyless, no auth: https://rss-public.bwe-ws.com/  (exchange announcements).
The websocket (wss://bwenews-api.bwe-ws.com/ws) is intentionally NOT used here —
a GitHub Actions cron can't hold a socket open. This polls instead, which is the
$0 cloud-friendly path (best-effort, ~per-cron-run freshness).

Outputs (into ../cache/):
  bwenews_feed.json     rolling de-duplicated list of recent feed items
  bwenews_signals.json  the subset classified as venue listing events, with the
                        token symbol + venue extracted, and whether that token is
                        already tracked in listings/*.json

Run:
    python fetch_bwenews.py
"""
from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.request import Request, urlopen

HERE = Path(__file__).parent
CACHE = HERE.parent / "cache"
CACHE.mkdir(exist_ok=True)
LISTINGS = HERE / "listings"

RSS_URL = "https://rss-public.bwe-ws.com/"
FEED_PATH = CACHE / "bwenews_feed.json"
SIGNALS_PATH = CACHE / "bwenews_signals.json"
UA = {"User-Agent": "Mozilla/5.0 verifysheet/bwenews"}

MAX_FEED = 400  # cap the rolling feed so the cache file can't grow unbounded

# Map a headline to one of OUR allowlisted venues. Order matters: more specific
# patterns first. Each entry: (compiled regex on the lowercased title, venue label
# matching the report's CHIP_SPECS / events[].exchange convention).
_VENUE_RULES: list[tuple[re.Pattern, str]] = [
    (re.compile(r"binance.*futures.*will launch"), "Binance Perp"),
    (re.compile(r"binance.*will list.*alpha|binance alpha"), "Binance Alpha"),
    (re.compile(r"binance.*will list"), "Binance Spot"),
    (re.compile(r"upbit listing|upbit\b"), "Upbit"),
    (re.compile(r"bithumb listing|bithumb\b"), "Bithumb"),
    (re.compile(r"coinone\b"), "Coinone"),
    (re.compile(r"coinbase"), "Coinbase"),
    (re.compile(r"\bokx\b"), "OKX"),
    (re.compile(r"\bbybit\b"), "Bybit"),
    (re.compile(r"\bkraken\b"), "Kraken"),
    (re.compile(r"\bkucoin\b"), "KuCoin"),
    (re.compile(r"\bbitget\b"), "Bitget"),
    (re.compile(r"gate\.io|\bgate\b"), "Gate.io"),
]

# Words that look like a ticker in "...XXXUSDT Perpetual..." but are quote assets
# or noise, so we never emit them as the listed token.
_QUOTE_SUFFIXES = ("USDT", "USDC", "USD", "USDⓈ", "KRW", "BTC", "ETH")
_NOISE_SYMBOLS = {"USDT", "USDC", "USD", "KRW", "BTC", "ETH", "ETF", "IPO", "CEO",
                  "SEC", "US", "AI", "DEX", "NFT"}


def _clean(title: str) -> str:
    """Strip the HTML the feed embeds inside <title> (e.g. <br/>, entities)."""
    t = re.sub(r"&lt;.*?&gt;", " ", title)      # escaped tags
    t = re.sub(r"<.*?>", " ", t)                # real tags
    t = (t.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
          .replace("&#39;", "'").replace("&quot;", '"'))
    return re.sub(r"\s+", " ", t).strip()


def _is_listing(title_l: str) -> bool:
    return any(k in title_l for k in (
        "will launch", "will list", "listing", "market support", "added",
        "perpetual", "spot trading", "world premiere"))


def _venue(title_l: str) -> str | None:
    for rx, label in _VENUE_RULES:
        if rx.search(title_l):
            return label
    return None


def _extract_symbol(title: str) -> str | None:
    """Best-effort ticker extraction from a listing headline.

    Two signals: a "(SYMBOL)" group right after a name, or a "<SYMBOL><QUOTE>"
    perpetual/spot pair. Returns the bare token symbol (uppercase) or None.
    """
    # 1) "Solstice(SLX)" / "Solstice (SLX)"
    for m in re.finditer(r"\(([A-Z0-9]{2,15})\)", title):
        cand = m.group(1)
        if cand not in _NOISE_SYMBOLS:
            return cand
    # 2) "SLXUSDT", "ANTHROPICUSDT" -> strip a known quote suffix
    for m in re.finditer(r"\b([A-Z0-9]{2,20})\b", title):
        word = m.group(1)
        for q in _QUOTE_SUFFIXES:
            if word.endswith(q) and len(word) > len(q):
                base = word[: -len(q)]
                if base and base not in _NOISE_SYMBOLS:
                    return base
    return None


def _tracked_symbols() -> set[str]:
    out = set()
    for p in LISTINGS.glob("*.json"):
        try:
            out.add(json.loads(p.read_text(encoding="utf-8"))["token"].upper())
        except Exception:
            pass
    return out


def _fetch_rss() -> str:
    req = Request(RSS_URL, headers=UA)
    with urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", "replace")


def _parse_items(xml: str) -> list[dict]:
    items = []
    for block in re.findall(r"<item>(.*?)</item>", xml, re.S):
        t = re.search(r"<title>(.*?)</title>", block, re.S)
        link = re.search(r"<link>(.*?)</link>", block, re.S)
        pub = re.search(r"<pubDate>(.*?)</pubDate>", block, re.S)
        if not t:
            continue
        title = _clean(t.group(1))
        ts = None
        if pub:
            try:
                ts = parsedate_to_datetime(pub.group(1).strip()).astimezone(
                    timezone.utc).isoformat()
            except Exception:
                ts = None
        items.append({
            "title": title,
            "link": link.group(1).strip() if link else None,
            "published_utc": ts,
        })
    return items


def main() -> int:
    try:
        xml = _fetch_rss()
    except Exception as e:
        print(f"bwenews: RSS fetch failed ({e}); leaving caches untouched.")
        return 0  # never fail the build over a flaky news feed

    fresh = _parse_items(xml)
    tracked = _tracked_symbols()

    # merge into the rolling feed, de-duping on (title, published)
    existing = []
    if FEED_PATH.exists():
        try:
            existing = json.loads(FEED_PATH.read_text(encoding="utf-8"))
        except Exception:
            existing = []
    seen = {(i.get("title"), i.get("published_utc")) for i in existing}
    added = 0
    for it in fresh:
        key = (it["title"], it["published_utc"])
        if key not in seen:
            existing.insert(0, it)
            seen.add(key)
            added += 1
    existing = existing[:MAX_FEED]

    # classify the listing signals
    signals = []
    for it in existing:
        title_l = it["title"].lower()
        if not _is_listing(title_l):
            continue
        venue = _venue(title_l)
        sym = _extract_symbol(it["title"])
        if not venue:
            continue
        signals.append({
            **it,
            "venue": venue,
            "symbol": sym,
            "tracked": bool(sym and sym in tracked),
        })

    FEED_PATH.write_text(json.dumps(existing, ensure_ascii=False, indent=2),
                         encoding="utf-8")
    SIGNALS_PATH.write_text(json.dumps({
        "fetched_utc": datetime.now(timezone.utc).isoformat(),
        "signals": signals,
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    new_tokens = sorted({s["symbol"] for s in signals
                         if s["symbol"] and not s["tracked"]})
    print(f"bwenews: {len(fresh)} items fetched, {added} new; "
          f"{len(signals)} listing signals; "
          f"untracked symbols seen: {', '.join(new_tokens) or '—'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
