"""Fetch the PRECISE Binance Futures perp-listing announcement time per tracked
token, so the report can annotate the on-chain Alpha price spike that the
announcement triggered (the CTR-style note the user liked:
"BN perp announce — 2026-05-28 08:16 UTC … Alpha price spiked +59% … ~74min
before the 09:30 contract open").

The day-resolution dates in announcements.json aren't enough to measure a 5-minute
spike. Binance's CMS *article detail* endpoint exposes `publishDate` in ms — the
real release moment (verified: CTR = 2026-05-28 08:16:29 UTC). So:

  1. page the Binance Futures announcements catalog (id 48) for
     "… Will Launch … <SYM>USDT Perpetual …" headlines,
  2. extract every USDT perp ticker in each headline (some bundle two),
  3. for tickers we track, fetch the article detail → `publishDate` (ms UTC).

Writes cache/perp_announce.json: { "SYM": {"announce_utc": "…Z", "title", "url"} }.
Static once captured (kept across runs); only missing tokens are fetched.

    python fetch_perp_announce.py            # all tracked tokens still missing
    python fetch_perp_announce.py ctr slx    # (re)fetch just these
"""
from __future__ import annotations

import json
import re
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).parent
LISTINGS = HERE / "listings"
OUT = HERE.parent / "cache" / "perp_announce.json"

CATALOG = ("https://www.binance.com/bapi/composite/v1/public/cms/article/"
           "catalog/list/query?catalogId=48&pageNo={pg}&pageSize=50")
DETAIL = ("https://www.binance.com/bapi/composite/v1/public/cms/article/"
          "detail/query?articleCode={code}")
ART_URL = "https://www.binance.com/en/support/announcement/detail/{code}"
HEADERS = {"User-Agent": "Mozilla/5.0", "Accept": "application/json", "lang": "en"}

# tickers in a futures launch headline, e.g. "Will Launch USDⓈ-Margined CTRUSDT
# Perpetual" or "… ZESTUSDT and BTWUSDT …". Capture the symbol before USDT.
TICKER_RE = re.compile(r"\b([A-Z0-9]{2,15})USDT\b")
MAX_PAGES = 60          # ~3000 articles back; plenty for the tracked set
PACE_S = 0.4


def _get(url: str) -> dict | None:
    for attempt in range(4):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=25) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception:
            time.sleep(2.0 * (attempt + 1))
    return None


def tracked_symbols() -> set[str]:
    out = set()
    for p in LISTINGS.glob("*.json"):
        try:
            out.add(json.loads(p.read_text(encoding="utf-8"))["token"].upper())
        except Exception:
            pass
    return out


def main(argv: list[str]) -> None:
    want = {a.upper() for a in argv}
    tracked = tracked_symbols()
    targets = (want & tracked) if want else tracked

    out: dict = {}
    if OUT.exists():
        try:
            out = json.loads(OUT.read_text(encoding="utf-8"))
        except Exception:
            out = {}
    # only chase tokens we don't already have (unless explicitly requested)
    missing = {s for s in targets if want or s not in out}
    if not missing:
        print("nothing to fetch (all tracked tokens already have a perp-announce time)")
        return

    print(f"seeking perp-announce times for {len(missing)} token(s)…", flush=True)
    found = 0
    for pg in range(1, MAX_PAGES + 1):
        if not missing:
            break
        payload = _get(CATALOG.format(pg=pg))
        arts = (((payload or {}).get("data") or {}).get("articles")) or []
        if not arts:
            break
        for a in arts:
            title = a.get("title") or ""
            if "perpetual" not in title.lower():
                continue
            syms = set(TICKER_RE.findall(title)) & missing
            if not syms:
                continue
            detail = _get(DETAIL.format(code=a["code"]))
            pubd = ((detail or {}).get("data") or {}).get("publishDate")
            time.sleep(PACE_S)
            if not pubd:
                continue
            iso = datetime.fromtimestamp(pubd / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            for s in syms:
                out[s] = {"announce_utc": iso, "title": title,
                          "url": ART_URL.format(code=a["code"])}
                missing.discard(s)
                found += 1
                print(f"  {s}: {iso}", flush=True)
        OUT.write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
        time.sleep(PACE_S)

    print(f"\nDONE. {found} new perp-announce time(s); "
          f"{len(missing)} still missing -> {OUT}", flush=True)


if __name__ == "__main__":
    main(sys.argv[1:])
