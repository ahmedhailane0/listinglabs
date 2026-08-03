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
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

# Run from any cwd: put perps_correlation/ on the path so `from fetch.fetch_screener
# import _fetch_token` (the shared signal pull) resolves, like the other scripts.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

HERE = Path(__file__).resolve().parents[1]          # perps_correlation/
CACHE = HERE.parent / "cache"
WATCHLIST = CACHE / "manip_watchlist.json"
SCREENER = CACHE / "screener" / "screener.json"
OUT = Path(os.environ.get("LIVE_OUT", str(CACHE / "screener" / "live.json")))
SIG_STATE = OUT.parent / ".live_signals.json"       # cached Buy verdicts between runs
FAPI = "https://fapi.binance.com"


def _get(url: str, timeout: int = 12):
    req = urllib.request.Request(url, headers={"User-Agent": "listinglabs-live/1"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def _compute_signals(base_by_sym: dict[str, str]) -> dict:
    """Recompute Buy v1/v2/v4 for every screenable coin, REUSING the hourly
    screener's exact per-coin pull + engine (`_fetch_token`) so a live fire is
    bit-identical to what the hourly screener would report — just sooner. Heavy
    (OI-hist + 1H klines + funding per coin), so callers gate it to once per
    closed hour. Returns {verdicts:{sym:{v1,v2,v4 fired bools}}, counts, as_of}."""
    from fetch.fetch_screener import _fetch_token, _binance_intervals
    try:
        intervals = _binance_intervals()
    except Exception:
        intervals = {}
    verdicts: dict[str, dict] = {}
    as_of = [None]

    def work(item):
        sym, base = item
        try:
            rec = _fetch_token(base, intervals)
            s = rec.get("signals") or {}
            verdicts[sym] = {k: 1 if (s.get(k) or {}).get("fired") else 0
                             for k in ("v1", "v2", "v4")}
            if rec.get("as_of"):
                as_of[0] = max(as_of[0] or 0, rec["as_of"])
        except Exception:
            pass

    with ThreadPoolExecutor(max_workers=24) as ex:
        list(ex.map(work, base_by_sym.items()))
    counts = {k: sum(v[k] for v in verdicts.values()) for k in ("v1", "v2", "v4")}
    return {"verdicts": verdicts, "counts": counts, "as_of": as_of[0]}


def main() -> int:
    wl = json.loads(WATCHLIST.read_text(encoding="utf-8"))
    # perp ticker (BINANCE) -> watchlist key, screenable coins only
    tkr2sym: dict[str, str] = {}
    base_by_sym: dict[str, str] = {}   # sym -> perp base (no USDT), for _fetch_token
    for s, r in wl.items():
        if r.get("has_perp"):
            base = (r.get("perp_sym") or s)
            tkr2sym[(base + "USDT").upper()] = s
            base_by_sym[s] = base

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
        # Universe follows the hourly screener (2026-07-20 audit): fetch_screener's
        # bulk exchangeInfo guard drops non-TRADING perps from screener.json, so a
        # delisted coin (e.g. IP) must drop out of live.json too — not keep serving
        # its final numbers every minute. Fail open: no/empty snapshot changes nothing.
        toks = set(sc.get("tokens") or {})
        if toks:
            base_by_sym = {s: b for s, b in base_by_sym.items() if s in toks}
            tkr2sym = {t: s for t, s in tkr2sym.items() if s in base_by_sym}
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
            # Binance perp 24h quote volume (USDT) — distinct from a token's CMC
            # market volume; surfaced as its own "Binance 24h vol" stat.
            "vol24": float(k["quoteVolume"]) if (k and k.get("quoteVolume")) else None,
        }

    # ── Buy v1/v2/v4 verdicts: cached between runs, recomputed once per closed
    # hour (~90s after the boundary so Binance has posted the new OI + candle).
    # Carried in live.json so the page flips Buy badges + counts live, with no
    # rebuild and no extra git/build churn. ──────────────────────────────────
    state = {}
    try:
        state = json.loads(SIG_STATE.read_text(encoding="utf-8"))
    except Exception:
        pass
    now = int(time.time())
    cur_hour = now // 3600
    need = (state.get("hour") != cur_hour) and ((now - cur_hour * 3600) >= 90)
    if need or not state.get("verdicts"):
        try:
            sig = _compute_signals(base_by_sym)
            if sig["verdicts"]:
                state = {"hour": cur_hour, "verdicts": sig["verdicts"],
                         "counts": sig["counts"], "as_of": sig["as_of"]}
                SIG_STATE.write_text(json.dumps(state, separators=(",", ":")), encoding="utf-8")
        except Exception as e:
            print(f"signals recompute skipped: {type(e).__name__}: {e}")
    verdicts = state.get("verdicts") or {}
    for s, sg in verdicts.items():
        if s in tokens:
            tokens[s]["sig"] = sg

    payload = {
        "generated_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "n": len(tokens),
        "sig_counts": state.get("counts"),
        "sig_as_of": state.get("as_of"),
        "tokens": tokens,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    tmp = OUT.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
    tmp.replace(OUT)
    fired = sum(1 for v in verdicts.values() if any(v.values()))
    print(f"live.json: {len(tokens)} tokens, {fired} with a Buy signal -> {OUT} @ {payload['generated_utc']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
