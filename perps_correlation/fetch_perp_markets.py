"""Per-exchange perp OI + funding for a token — keyless, Coinglass-style.

For a given token symbol, query each exchange's PUBLIC perp API for its
USDT-margined perpetual and return, per venue:
    { venue, oi_usd, oi_coins, funding, funding_interval_h, funding_annualized,
      mark, vol24h_usd }
plus an aggregate (total OI across venues + each venue's share of it).

Everything here is keyless (each exchange exposes OI + funding without an API
key), so it is safe in the $0 CI path. Wrong-ticker guard: a symbol like "H" or
"LAB" can collide with an unrelated project's perp on some venue, so each venue's
mark price is sanity-checked against the token's known spot price (0.5x–2x);
venues that fail are dropped rather than reported with a confidently-wrong OI.

    python fetch_perp_markets.py LAB 10.65      # print the table for one token
    python fetch_perp_markets.py                # refresh all scam-watchlist tokens
"""
from __future__ import annotations

import json
import sys
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).parent
CACHE = HERE.parent / "cache"
OUT = CACHE / "perp_markets"
UA = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
HOURS_PER_YEAR = 24 * 365  # 8760

# Binance fundingInfo lists only symbols whose interval differs from the 8h
# default; cache it once per process (it's one call for ALL symbols) so the
# per-symbol Binance adapter doesn't re-pull it 26 times in an all-token run.
_BINANCE_FI = None


def _binance_intervals():
    global _BINANCE_FI
    if _BINANCE_FI is None:
        fi = _get("https://fapi.binance.com/fapi/v1/fundingInfo") or []
        _BINANCE_FI = {x.get("symbol"): x.get("fundingIntervalHours") for x in fi}
    return _BINANCE_FI


def _get(url: str, tries: int = 2):
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=15) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception:
            if i == tries - 1:
                return None
            time.sleep(0.6)
    return None


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


# ── per-venue adapters ───────────────────────────────────────────────────────
# Each returns a normalized dict (oi_coins/oi_usd/funding/interval_h/mark/vol24h)
# or None when the token has no perp there / the call failed. All keyless.

def _binance(sym):
    pi = _get(f"https://fapi.binance.com/fapi/v1/premiumIndex?symbol={sym}USDT")
    oi = _get(f"https://fapi.binance.com/fapi/v1/openInterest?symbol={sym}USDT")
    if not pi or not oi or oi.get("openInterest") is None:
        return None
    mark = _f(pi.get("markPrice"))
    coins = _f(oi.get("openInterest"))
    if not mark or coins is None:
        return None
    ih = _binance_intervals().get(f"{sym}USDT", 8) or 8
    t = _get(f"https://fapi.binance.com/fapi/v1/ticker/24hr?symbol={sym}USDT") or {}
    return {"oi_coins": coins, "oi_usd": coins * mark, "mark": mark,
            "funding": _f(pi.get("lastFundingRate")), "interval_h": float(ih),
            "vol24h_usd": _f(t.get("quoteVolume"))}


def _bybit(sym):
    d = _get(f"https://api.bybit.com/v5/market/tickers?category=linear&symbol={sym}USDT")
    row = ((d or {}).get("result") or {}).get("list") or []
    if not row:
        return None
    r = row[0]
    mark = _f(r.get("markPrice"))
    oi_usd = _f(r.get("openInterestValue"))
    if not mark or oi_usd is None:
        return None
    info = _get(f"https://api.bybit.com/v5/market/instruments-info?category=linear&symbol={sym}USDT")
    il = ((info or {}).get("result") or {}).get("list") or [{}]
    mins = _f(il[0].get("fundingInterval")) or 480.0  # minutes
    return {"oi_coins": _f(r.get("openInterest")), "oi_usd": oi_usd, "mark": mark,
            "funding": _f(r.get("fundingRate")), "interval_h": mins / 60.0,
            "vol24h_usd": _f(r.get("turnover24h"))}


def _okx(sym):
    oi = _get(f"https://www.okx.com/api/v5/public/open-interest?instType=SWAP&instId={sym}-USDT-SWAP")
    od = (oi or {}).get("data") or []
    if not od:
        return None
    oi_usd = _f(od[0].get("oiUsd"))
    if oi_usd is None:
        return None
    fr = _get(f"https://www.okx.com/api/v5/public/funding-rate?instId={sym}-USDT-SWAP")
    fd = ((fr or {}).get("data") or [{}])[0]
    nxt, cur = _f(fd.get("nextFundingTime")), _f(fd.get("fundingTime"))
    ih = round((nxt - cur) / 3600000) if (nxt and cur and nxt > cur) else 8
    tk = _get(f"https://www.okx.com/api/v5/market/ticker?instId={sym}-USDT-SWAP")
    td = ((tk or {}).get("data") or [{}])[0]
    mark = _f(td.get("last")) or _f(od[0].get("oiUsd")) and (oi_usd / (_f(od[0].get("oiCcy")) or 1))
    vol = _f(td.get("volCcy24h"))
    return {"oi_coins": _f(od[0].get("oiCcy")), "oi_usd": oi_usd, "mark": mark,
            "funding": _f(fd.get("fundingRate")), "interval_h": float(ih or 8),
            "vol24h_usd": (vol * mark) if (vol and mark) else None}


def _kucoin(sym):
    d = _get(f"https://api-futures.kucoin.com/api/v1/contracts/{sym}USDTM")
    c = (d or {}).get("data") or {}
    mark, oi, mult = _f(c.get("markPrice")), _f(c.get("openInterest")), _f(c.get("multiplier"))
    if not mark or oi is None or not mult:
        return None
    coins = oi * mult
    gran = _f(c.get("fundingRateGranularity")) or 28800000.0  # ms
    return {"oi_coins": coins, "oi_usd": coins * mark, "mark": mark,
            "funding": _f(c.get("fundingFeeRate")), "interval_h": gran / 3600000.0,
            "vol24h_usd": _f(c.get("turnoverOf24h"))}


def _gate(sym):
    t = _get(f"https://api.gateio.ws/api/v4/futures/usdt/tickers?contract={sym}_USDT")
    t = (t[0] if isinstance(t, list) and t else None)
    c = _get(f"https://api.gateio.ws/api/v4/futures/usdt/contracts/{sym}_USDT") or {}
    if not t or not c:
        return None
    mark = _f(t.get("mark_price"))
    qmult = _f(c.get("quanto_multiplier")) or 1.0
    size = _f(t.get("total_size"))
    if not mark or size is None:
        return None
    coins = size * qmult
    interval_s = _f(c.get("funding_interval")) or 28800.0
    vol = _f(t.get("volume_24h_settle")) or _f(t.get("volume_24h_quote"))
    return {"oi_coins": coins, "oi_usd": coins * mark, "mark": mark,
            "funding": _f(t.get("funding_rate")), "interval_h": interval_s / 3600.0,
            "vol24h_usd": vol}


def _bitget(sym):
    d = _get(f"https://api.bitget.com/api/v2/mix/market/ticker?symbol={sym}USDT&productType=usdt-futures")
    row = (d or {}).get("data") or []
    row = row[0] if isinstance(row, list) and row else (row if isinstance(row, dict) else None)
    if not row:
        return None
    mark = _f(row.get("markPrice")) or _f(row.get("indexPrice")) or _f(row.get("lastPr"))
    coins = _f(row.get("holdingAmount"))
    if not mark or coins is None:
        return None
    cfg = _get(f"https://api.bitget.com/api/v2/mix/market/contracts?symbol={sym}USDT&productType=usdt-futures")
    crow = ((cfg or {}).get("data") or [{}])[0]
    ih = _f(crow.get("fundInterval")) or 8.0   # contract config, in hours
    return {"oi_coins": coins, "oi_usd": coins * mark, "mark": mark,
            "funding": _f(row.get("fundingRate")), "interval_h": ih,
            "vol24h_usd": _f(row.get("usdtVolume")) or _f(row.get("quoteVolume"))}


def _bingx(sym):
    oi = _get(f"https://open-api.bingx.com/openApi/swap/v2/quote/openInterest?symbol={sym}-USDT")
    pi = _get(f"https://open-api.bingx.com/openApi/swap/v2/quote/premiumIndex?symbol={sym}-USDT")
    od = (oi or {}).get("data") or {}
    pd = (pi or {}).get("data") or {}
    oi_usd = _f(od.get("openInterest"))   # BingX returns OI already in USD
    mark = _f(pd.get("markPrice"))
    if oi_usd is None or not mark:
        return None
    hist = _get(f"https://open-api.bingx.com/openApi/swap/v2/quote/fundingRate?symbol={sym}-USDT&limit=3")
    ts = [int(x["fundingTime"]) for x in ((hist or {}).get("data") or []) if x.get("fundingTime")]
    ih = round((ts[0] - ts[1]) / 3600000) if len(ts) > 1 and ts[0] > ts[1] else 8.0
    return {"oi_coins": oi_usd / mark, "oi_usd": oi_usd, "mark": mark,
            "funding": _f(pd.get("lastFundingRate")), "interval_h": float(ih or 8),
            "vol24h_usd": None}


def _mexc(sym):
    d = _get(f"https://contract.mexc.com/api/v1/contract/ticker?symbol={sym}_USDT")
    r = (d or {}).get("data") or {}
    det = _get(f"https://contract.mexc.com/api/v1/contract/detail?symbol={sym}_USDT")
    dd = (det or {}).get("data") or {}
    mark = _f(r.get("fairPrice")) or _f(r.get("lastPrice"))
    holdvol = _f(r.get("holdVol"))
    size = _f(dd.get("contractSize"))
    if not mark or holdvol is None or not size:
        return None
    coins = holdvol * size
    fr = _get(f"https://contract.mexc.com/api/v1/contract/funding_rate/{sym}_USDT")
    frd = (fr or {}).get("data") or {}
    ih = _f(frd.get("collectCycle")) or 8.0   # MEXC funding interval, in hours
    return {"oi_coins": coins, "oi_usd": coins * mark, "mark": mark,
            "funding": _f(frd.get("fundingRate")) if frd.get("fundingRate") is not None
                       else _f(r.get("fundingRate")),
            "interval_h": ih, "vol24h_usd": _f(r.get("amount24"))}


# Single-symbol adapters used by the CLI single-token path (and as the per-symbol
# venues in the all-token path, which have no bulk endpoint).
SINGLE = [("Binance", _binance), ("OKX", _okx), ("Bybit", _bybit), ("KuCoin", _kucoin),
          ("Gate", _gate), ("Bitget", _bitget), ("BingX", _bingx), ("MEXC", _mexc)]
# Venues without a bulk endpoint — always queried per symbol.
PER_SYMBOL = [("Binance", _binance), ("OKX", _okx), ("BingX", _bingx)]
BULK_VENUES = ("Bybit", "KuCoin", "Gate", "Bitget", "MEXC")


# ── bulk loaders: one (or few) calls return ALL symbols; key by base symbol ───
# Used by the all-token path to avoid ~600 per-symbol calls on the shared CI IP.

def _bulk_bybit():
    d = _get("https://api.bybit.com/v5/market/tickers?category=linear")
    info = _get("https://api.bybit.com/v5/market/instruments-info?category=linear&limit=1000")
    intervals = {r.get("symbol"): _f(r.get("fundingInterval"))
                 for r in ((info or {}).get("result") or {}).get("list") or []}
    out = {}
    for r in ((d or {}).get("result") or {}).get("list") or []:
        s = r.get("symbol", "")
        if not s.endswith("USDT"):
            continue
        mark, oi_usd = _f(r.get("markPrice")), _f(r.get("openInterestValue"))
        if not mark or oi_usd is None:
            continue
        out[s[:-4]] = {"oi_coins": _f(r.get("openInterest")), "oi_usd": oi_usd, "mark": mark,
                       "funding": _f(r.get("fundingRate")),
                       "interval_h": (intervals.get(s) or 480.0) / 60.0,
                       "vol24h_usd": _f(r.get("turnover24h"))}
    return out


def _bulk_kucoin():
    d = _get("https://api-futures.kucoin.com/api/v1/contracts/active")
    out = {}
    for c in (d or {}).get("data") or []:
        s = c.get("symbol", "")
        if not s.endswith("USDTM"):
            continue
        mark, oi, mult = _f(c.get("markPrice")), _f(c.get("openInterest")), _f(c.get("multiplier"))
        if not mark or oi is None or not mult:
            continue
        coins = oi * mult
        out[s[:-5]] = {"oi_coins": coins, "oi_usd": coins * mark, "mark": mark,
                       "funding": _f(c.get("fundingFeeRate")),
                       "interval_h": (_f(c.get("fundingRateGranularity")) or 28800000.0) / 3600000.0,
                       "vol24h_usd": _f(c.get("turnoverOf24h"))}
    return out


def _bulk_gate():
    tk = _get("https://api.gateio.ws/api/v4/futures/usdt/tickers")
    cs = _get("https://api.gateio.ws/api/v4/futures/usdt/contracts")
    cmap = {c.get("name"): c for c in cs} if isinstance(cs, list) else {}
    out = {}
    for t in (tk or []) if isinstance(tk, list) else []:
        name = t.get("contract", "")
        if not name.endswith("_USDT"):
            continue
        c = cmap.get(name) or {}
        mark, size = _f(t.get("mark_price")), _f(t.get("total_size"))
        if not mark or size is None:
            continue
        coins = size * (_f(c.get("quanto_multiplier")) or 1.0)
        out[name[:-5]] = {"oi_coins": coins, "oi_usd": coins * mark, "mark": mark,
                          "funding": _f(t.get("funding_rate")),
                          "interval_h": (_f(c.get("funding_interval")) or 28800.0) / 3600.0,
                          "vol24h_usd": _f(t.get("volume_24h_settle")) or _f(t.get("volume_24h_quote"))}
    return out


def _bulk_bitget():
    tk = _get("https://api.bitget.com/api/v2/mix/market/tickers?productType=usdt-futures")
    cs = _get("https://api.bitget.com/api/v2/mix/market/contracts?productType=usdt-futures")
    cmap = {c.get("symbol"): c for c in (cs or {}).get("data") or []}
    out = {}
    for t in (tk or {}).get("data") or []:
        s = t.get("symbol", "")
        if not s.endswith("USDT"):
            continue
        mark = _f(t.get("markPrice")) or _f(t.get("indexPrice")) or _f(t.get("lastPr"))
        coins = _f(t.get("holdingAmount"))
        if not mark or coins is None:
            continue
        out[s[:-4]] = {"oi_coins": coins, "oi_usd": coins * mark, "mark": mark,
                       "funding": _f(t.get("fundingRate")),
                       "interval_h": _f((cmap.get(s) or {}).get("fundInterval")) or 8.0,
                       "vol24h_usd": _f(t.get("usdtVolume")) or _f(t.get("quoteVolume"))}
    return out


def _bulk_mexc(wanted=None):
    """MEXC has no bulk funding-interval, so for the few WANTED symbols we make a
    per-symbol funding_rate call to get the accurate interval (collectCycle)."""
    tk = _get("https://contract.mexc.com/api/v1/contract/ticker")
    dt = _get("https://contract.mexc.com/api/v1/contract/detail")
    size = {c.get("symbol"): _f(c.get("contractSize")) for c in (dt or {}).get("data") or []}
    out = {}
    for r in (tk or {}).get("data") or []:
        s = r.get("symbol", "")
        if not s.endswith("_USDT"):
            continue
        base = s[:-5]
        if wanted is not None and base not in wanted:
            continue
        mark = _f(r.get("fairPrice")) or _f(r.get("lastPrice"))
        holdvol, sz = _f(r.get("holdVol")), size.get(s)
        if not mark or holdvol is None or not sz:
            continue
        ih, fund = 8.0, _f(r.get("fundingRate"))
        fr = ((_get(f"https://contract.mexc.com/api/v1/contract/funding_rate/{s}") or {}).get("data") or {})
        if fr:
            ih = _f(fr.get("collectCycle")) or 8.0
            if fr.get("fundingRate") is not None:
                fund = _f(fr.get("fundingRate"))
        out[base] = {"oi_coins": holdvol * sz, "oi_usd": holdvol * sz * mark, "mark": mark,
                     "funding": fund, "interval_h": ih, "vol24h_usd": _f(r.get("amount24"))}
    return out


def _assemble(sym: str, spot_price, raw) -> dict:
    """raw: list of (venue_name, norm_dict). Drop venues whose mark disagrees with
    spot (collision guard), annualize funding, compute OI shares, sort by OI."""
    venues = []
    for name, d in raw:
        if not d or not d.get("oi_usd"):
            continue
        mark = d.get("mark")
        if spot_price and mark and not (0.5 <= mark / spot_price <= 2.0):
            continue  # wrong-ticker collision — different project on this venue
        v = dict(d)
        f, ih = v.get("funding"), v.get("interval_h") or 8.0
        v["funding_annualized"] = (f * (HOURS_PER_YEAR / ih)) if f is not None else None
        v["venue"] = name
        venues.append(v)
    total = sum(v["oi_usd"] for v in venues) or 0.0
    for v in venues:
        v["oi_share_pct"] = (v["oi_usd"] / total * 100) if total else None
        v["oi_vol_ratio"] = (v["oi_usd"] / v["vol24h_usd"]) if v.get("vol24h_usd") else None
    venues.sort(key=lambda v: -v["oi_usd"])
    return {"symbol": sym, "total_oi_usd": total, "n_venues": len(venues),
            "venues": venues, "fetched_at": int(time.time())}


def fetch_token(symbol: str, spot_price, bulk_maps=None) -> dict:
    """One token's per-venue OI+funding. With bulk_maps (all-token path) the bulk
    venues are read from the pre-fetched maps; otherwise (CLI) every venue is
    queried per-symbol. Per-symbol-only venues are always queried directly."""
    sym = symbol.upper()
    raw = []
    if bulk_maps is not None:
        for name in BULK_VENUES:
            d = (bulk_maps.get(name) or {}).get(sym)
            if d:
                raw.append((name, d))
    else:
        for name, fn in [("Bybit", _bybit), ("KuCoin", _kucoin), ("Gate", _gate),
                         ("Bitget", _bitget), ("MEXC", _mexc)]:
            try:
                d = fn(sym)
            except Exception:
                d = None
            if d:
                raw.append((name, d))
    for name, fn in PER_SYMBOL:
        try:
            d = fn(sym)
        except Exception:
            d = None
        if d:
            raw.append((name, d))
    return _assemble(sym, spot_price, raw)


# ── CoinGecko derivatives source (AUTONOMOUS / CI default) ───────────────────
# Binance/OKX/Bybit geo-block GitHub's runner IP, so direct fetching can't run in
# CI. CoinGecko aggregates every exchange's perp tickers SERVER-SIDE (one keyless
# call the runner CAN reach), giving per-venue OI + funding for all our venues.
# Validated against the direct adapters: OI matches, funding matches within
# snapshot noise. CG lacks the funding interval, so we annualize using KuCoin's
# interval (one bulk call, confirmed reachable from CI), defaulting to 8h.

CG_DERIV = "https://api.coingecko.com/api/v3/derivatives?include_tickers=all"
# CoinGecko market name -> our venue label (only the venues we track).
CG_MARKET = {"Binance (Futures)": "Binance", "OKX (Futures)": "OKX",
             "Bybit (Futures)": "Bybit", "KuCoin Futures": "KuCoin",
             "Gate (Futures)": "Gate", "Bitget Futures": "Bitget",
             "BingX (Futures)": "BingX", "MEXC (Futures)": "MEXC"}


def _cg_interval_map(wanted):
    """Per-token funding interval (hours) from KuCoin's bulk feed — one call,
    confirmed reachable from the CI runner. Tokens KuCoin doesn't list fall back
    to 8h at use time. Funding intervals move together across venues, so one
    venue's interval is a good proxy for the token."""
    try:
        ku = _bulk_kucoin()
    except Exception:
        ku = {}
    return {s: d["interval_h"] for s, d in ku.items()
            if s in wanted and d.get("interval_h")}


def fetch_all_cg(tokens) -> dict:
    """Autonomous, CI-safe per-venue OI+funding via CoinGecko derivatives.
    tokens: list of (symbol, spot)."""
    wanted = {s.upper() for s, _ in tokens}
    spot = {s.upper(): sp for s, sp in tokens}
    try:
        deriv = _get(CG_DERIV, tries=3) or []
    except Exception:
        deriv = []
    intervals = _cg_interval_map(wanted)
    # group: token -> {venue: norm_dict} (keep the larger-OI ticker per venue)
    by_tok = {}
    for t in deriv:
        idx = (t.get("index_id") or "").upper()
        venue = CG_MARKET.get(t.get("market"))
        if idx not in wanted or not venue:
            continue
        oi, price = _f(t.get("open_interest")), _f(t.get("price"))
        if not oi:
            continue
        fr = _f(t.get("funding_rate"))            # CoinGecko quotes funding in %
        d = {"oi_usd": oi, "oi_coins": (oi / price) if price else None, "mark": price,
             "funding": (fr / 100.0) if fr is not None else None,
             "interval_h": intervals.get(idx, 8.0), "vol24h_usd": _f(t.get("volume_24h"))}
        slot = by_tok.setdefault(idx, {})
        if venue not in slot or oi > slot[venue]["oi_usd"]:
            slot[venue] = d
    out = {}
    for sym in wanted:
        raw = [(v, d) for v, d in (by_tok.get(sym) or {}).items()]
        res = _assemble(sym, spot.get(sym), raw)
        res["source"] = "coingecko"
        out[sym] = res
    return out


def fetch_all(tokens) -> dict:
    """tokens: list of (symbol, spot). Pre-fetch the bulk venue maps ONCE (~10
    calls total), then assemble every token — the per-symbol venues run in
    parallel across tokens. ~120 calls/run vs ~600 for naive per-symbol."""
    wanted = {s.upper() for s, _ in tokens}
    bulk_maps = {"Bybit": _bulk_bybit(), "KuCoin": _bulk_kucoin(), "Gate": _bulk_gate(),
                 "Bitget": _bulk_bitget(), "MEXC": _bulk_mexc(wanted)}
    out = {}
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = {ex.submit(fetch_token, s, sp, bulk_maps): s.upper() for s, sp in tokens}
        for fut, sym in futs.items():
            try:
                out[sym] = fut.result()
            except Exception:
                out[sym] = None
    return out


def _print(res):
    print(f"\n{res['symbol']}  total OI ${res['total_oi_usd']/1e6:.2f}M  "
          f"across {res['n_venues']} venues")
    print(f"  {'venue':9} {'OI(USD)':>12} {'share':>7} {'funding':>10} {'every':>6} {'annual':>9} {'OI/vol':>7}")
    for v in res["venues"]:
        fund = f"{v['funding']*100:+.4f}%" if v.get("funding") is not None else "  -"
        ann = f"{v['funding_annualized']*100:+.0f}%" if v.get("funding_annualized") is not None else "  -"
        ovr = f"{v['oi_vol_ratio']:.3f}" if v.get("oi_vol_ratio") is not None else "-"
        iv = f"{v.get('interval_h',8):g}h"
        print(f"  {v['venue']:9} {v['oi_usd']/1e6:>10.2f}M {v['oi_share_pct']:>6.1f}% "
              f"{fund:>10} {iv:>6} {ann:>9} {ovr:>7}")


def main(argv):
    OUT.mkdir(parents=True, exist_ok=True)
    # --direct = hit each exchange's own API (full fidelity, but Binance/OKX/Bybit
    # geo-block datacenter IPs, so it's a LOCAL oracle only). Default = CoinGecko
    # derivatives (autonomous / CI-safe).
    direct = "--direct" in argv
    argv = [a for a in argv if a != "--direct"]
    if len(argv) >= 1 and argv[0].upper() != "--ALL":
        sym = argv[0].upper()
        spot = _f(argv[1]) if len(argv) > 1 else None
        res = (fetch_token(sym, spot) if direct
               else fetch_all_cg([(sym, spot)])[sym])
        (OUT / f"{sym}.json").write_text(json.dumps(res, ensure_ascii=False), encoding="utf-8")
        _print(res)
        return 0
    # refresh all scam-watchlist tokens
    data = json.loads((CACHE / "scam_data.json").read_text(encoding="utf-8"))
    tokens = [(rec["symbol"], rec.get("price") or rec.get("csv_price"))
              for rec in data.values()]
    results = fetch_all(tokens) if direct else fetch_all_cg(tokens)
    for sym, res in sorted(results.items()):
        if not res:
            print(f"{sym:9} (failed)")
            continue
        path = OUT / f"{sym}.json"
        # Coverage guard: some exchanges (Binance/OKX/Bybit) geo-block datacenter
        # IPs, so a CI run can silently lose the major venues and recompute every
        # share over a much smaller total — confidently wrong, freshly stamped.
        # Don't let a materially-worse run clobber a recent good snapshot; the
        # 24h staleness escape lets a *real* coverage change settle.
        if path.exists():
            try:
                prev = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                prev = {}
            worse = (res["n_venues"] < (prev.get("n_venues") or 0)
                     and res["total_oi_usd"] < 0.7 * (prev.get("total_oi_usd") or 0))
            fresh = (res["fetched_at"] - (prev.get("fetched_at") or 0)) < 24 * 3600
            if worse and fresh:
                print(f"{sym:9} coverage dropped to {res['n_venues']}v/"
                      f"${res['total_oi_usd']/1e6:.0f}M (cached {prev.get('n_venues')}v/"
                      f"${(prev.get('total_oi_usd') or 0)/1e6:.0f}M) — kept cached")
                continue
        path.write_text(json.dumps(res, ensure_ascii=False), encoding="utf-8")
        print(f"{sym:9} OI ${res['total_oi_usd']/1e6:8.2f}M  {res['n_venues']} venues")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
