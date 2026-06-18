#!/usr/bin/env python3
"""BOX-ONLY: the ~60s "live cells" fetcher for the Manipulated tab.

`fetch_screener.py` is the heavy hourly pass (FDV/market/holders/Buy signals).
This is its fast little sibling: in ONE quick pass it pulls only the numbers that
move minute-to-minute — Binance mark price, 24h %, current funding, current OI —
for every screenable watchlist coin, and writes a tiny `live.json`. Caddy serves
that file over HTTPS (sslip.io host) and the page polls it every ~60s, updating
those cells in place without a site rebuild.

Consistency with the hourly screener (data-correctness rule):
  * perp ticker resolves the SAME way: (watchlist `perp_sym` or key) + "USDT".
  * Bybit OI + funding interval are carried forward from the last screener.json
    (they move slowly); `oi_combined` = live Binance OI + carried Bybit OI, so the
    combined-OI cell stays correct without a second venue round-trip every minute.

Output path is env-overridable (LIVE_OUT) so it runs locally (writes into cache/)
and on the box (writes into Caddy's web root). Network only — no keys.
"""
from __future__ import annotations
import json
import os
import sys
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]          # perps_correlation/
CACHE = HERE.parent / "cache"
WATCHLIST = CACHE / "manip_watchlist.json"
SCREENER = CACHE / "screener" / "screener.json"
OUT = Path(os.environ.get("LIVE_OUT", str(CACHE / "screener" / "live.json")))
FAPI = "https://fapi.binance.com"


def _get(url: str, timeout: int = 12):
    req = urllib.request.Request(url, headers={"User-Agent": "listinglabs-live/1"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def main() -> int:
    wl = json.loads(WATCHLIST.read_text(encoding="utf-8"))
    # perp ticker (BINANCE) -> watchlist key, screenable coins only
    tkr2sym: dict[str, str] = {}
    for s, r in wl.items():
        if r.get("has_perp"):
            tkr2sym[((r.get("perp_sym") or s) + "USDT").upper()] = s

    # Carry slow-moving context from the last hourly screener snapshot.
    fint: dict[str, float] = {}
    oibyb: dict[str, float] = {}
    try:
        sc = json.loads(SCREENER.read_text(encoding="utf-8"))
        for s, rec in sc.get("tokens", {}).items():
            if rec.get("funding_interval_h"):
                fint[s] = rec["funding_interval_h"]
            if rec.get("oi_byb") is not None:
                oibyb[s] = rec["oi_byb"]
    except Exception:
        pass

    # Two cheap batch calls cover price + funding for every symbol at once.
    prem = {x["symbol"]: x for x in _get(f"{FAPI}/fapi/v1/premiumIndex")}
    tick = {x["symbol"]: x for x in _get(f"{FAPI}/fapi/v1/ticker/24hr")}

    # Current OI has no batch endpoint -> one tiny call per symbol, concurrently.
    def _oi(t: str):
        try:
            d = _get(f"{FAPI}/fapi/v1/openInterest?symbol={t}")
            return t, float(d["openInterest"])
        except Exception:
            return t, None

    oi_ct: dict[str, float | None] = {}
    with ThreadPoolExecutor(max_workers=16) as ex:
        for t, v in ex.map(_oi, list(tkr2sym)):
            oi_ct[t] = v

    tokens: dict[str, dict] = {}
    for t, s in tkr2sym.items():
        p = prem.get(t)
        if not p:
            continue
        k = tick.get(t)
        mark = float(p["markPrice"]) if p.get("markPrice") else None
        ct = oi_ct.get(t)
        oi_bn = ct * mark if (ct and mark) else None
        oi_comb = ((oi_bn or 0) + (oibyb.get(s) or 0)) if (oi_bn or oibyb.get(s)) else None
        tokens[s] = {
            "price": float(k["lastPrice"]) if (k and k.get("lastPrice")) else mark,
            "p24": float(k["priceChangePercent"]) if (k and k.get("priceChangePercent")) else None,
            "funding": float(p["lastFundingRate"]) if p.get("lastFundingRate") else None,
            "funding_interval_h": fint.get(s),
            "oi_bn": oi_bn,
            "oi_combined": oi_comb,
        }

    payload = {
        "generated_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "n": len(tokens),
        "tokens": tokens,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    tmp = OUT.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
    tmp.replace(OUT)
    print(f"live.json: {len(tokens)} tokens -> {OUT} @ {payload['generated_utc']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
