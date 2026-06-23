"""Hourly perp-screener fetcher for the Manipulated tab.

RUNS ON THE ALWAYS-ON BOX, not GitHub CI: Binance's futures API (fapi.binance.com)
returns 451 to datacenter IPs, so CI can't fetch this. For every screenable coin
in cache/manip_watchlist.json (has_perp) it pulls from Binance:

  * hourly open-interest history  (futures/data/openInterestHist, period=1h)
  * 1H klines                     (fapi/v1/klines, interval=1h)
  * funding history + interval    (fapi/v1/fundingRate, fapi/v1/fundingInfo)

plus current Bybit OI (one bulk call), price-checked CMC market data
(FDV/mcap/vol/supply/%changes/chain/contract — the PRICE CHECK kills wrong-coin
slug matches like TRUTH->truth-technology), and best-effort GoPlus holders for
EVM contracts. Runs the Buy v1/v2/v3 engine (lib.signals) and writes ONE compact
file the site builds from: cache/screener/screener.json.

Stateless on history: openInterestHist returns ~real hourly OI each call, so the
signals are correct from the very first run (no multi-day warm-up).

    python fetch/fetch_screener.py             # all screenable coins
    python fetch/fetch_screener.py SIREN GUA   # just these (debug)
"""
from __future__ import annotations

import json
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))
from fetch.fetch_perp_markets import _get, _f, _bulk_bybit, _binance_intervals
from lib import signals
from lib.signals import evaluate

HERE = Path(__file__).resolve().parents[1]   # perps_correlation/
CACHE = HERE.parent / "cache"
WATCHLIST = CACHE / "manip_watchlist.json"
OUTDIR = CACHE / "screener"
OUT = OUTDIR / "screener.json"

FAPI = "https://fapi.binance.com"
OI_LIMIT = 300
KLINE_LIMIT = 300
FUND_LIMIT = 20
GATE = 0.08
WORKERS = 6
SPARK_POINTS = 120

# ── Market enrichment (CMC keyless detail endpoint) ──────────────────────────
MARKET_CACHE_FILE = OUTDIR / "market.json"
MARKET_TTL_S = 6 * 3600      # refresh a coin's market data at most every 6h
MARKET_MAX_PER_RUN = 40       # cap CMC detail calls per run
CMC_DETAIL_URL = "https://api.coinmarketcap.com/data-api/v3/cryptocurrency/detail?slug={slug}"
CMC_UA = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
CMC_MAP_FILE = HERE.parent / "cmc_map.json"   # may not exist on the box (gitignored)
PRICE_TOL = 3.0               # CMC price must be within 3x of Binance perp mark

# ── Forward fire-log (out-of-sample ledger) ─────────────────────────────────
# Append-only record of every NEW (sym, setup, fire-hour) the screener reports,
# with the entry reference price + stop, so tools/grade_fires.py can score the
# outcome once the future arrives. This is the clean out-of-sample evidence the
# tuner (tools/optimize_signals.py) validates against — it accrues forever.
FIRES_LOG = OUTDIR / "fires_log.json"
FIRES_LOG_MAX = 8000

# Optional pump-probability scorer (LOCAL/BOX-only module + model; absent in CI,
# so this no-ops there). When present, writes a 0-100 `pump_score` per coin.
try:
    from tools.pump_score import load_model as _load_pump_model, score_series as _pump_score
    _PUMP_MODEL = _load_pump_model()
except Exception:
    _pump_score = None
    _PUMP_MODEL = None

# ── Hourly training series (append-only, month-sharded) ─────────────────────
# One line per coin per hourly run: OI / volume / funding / price / FDV. This is
# the PERMANENT training set the model learns on — it survives Binance's ~30-day
# OI retention wall because we keep our own copy forever. Month-sharded JSONL so
# old months stay static (clean git diffs = pure appends). Loaders dedup by
# (sym, t) keeping the last. See tools/CLAUDE.md (signal loop).
HOURLY_DIR = OUTDIR / "hourly"

# ── Holders (best-effort, EVM only) ─────────────────────────────────────────
HOLDERS_DIR = CACHE / "scam_holders"
HOLDER_MAX_PER_RUN = 30
HOLDER_TTL_S = 24 * 3600

# CMC contractPlatform name → CoinGecko platform key (GoPlus chain mapping)
CMC_TO_CG_PLATFORM = {
    "Ethereum": "ethereum",
    "BNB Smart Chain (BEP20)": "binance-smart-chain",
    "BNB Chain": "binance-smart-chain",
    "Base": "base",
    "Polygon": "polygon-pos",
    "Arbitrum": "arbitrum-one",
    "Arbitrum One": "arbitrum-one",
    "Optimism": "optimistic-ethereum",
    "Avalanche C-Chain": "avalanche",
    "Avalanche": "avalanche",
}


_ALPHA_SLUG_CACHE: dict[str, str] = {}


def _load_alpha_slugs() -> dict[str, str]:
    """Bulk-fetch CMC binance-alpha tagged coins → {SYM: slug}. One call covers
    ~400 coins. Cached for the run."""
    if _ALPHA_SLUG_CACHE:
        return _ALPHA_SLUG_CACHE
    by_sym: dict[str, str] = {}
    for start in range(1, 600, 100):
        url = ("https://api.coinmarketcap.com/data-api/v3/cryptocurrency/listing"
               f"?start={start}&limit=100&sortBy=market_cap&sortType=desc"
               "&cryptoType=all&tagType=all&audited=false&aux=cmc_rank"
               "&tagSlugs=binance-alpha")
        req = urllib.request.Request(url, headers=CMC_UA)
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                items = json.loads(resp.read()).get("data", {}).get("cryptoCurrencyList", [])
        except Exception:
            break
        for c in items:
            sym = (c.get("symbol") or "").upper()
            slug = c.get("slug") or ""
            if sym and slug:
                by_sym.setdefault(sym, slug)
        if len(items) < 100:
            break
        time.sleep(0.5)
    _ALPHA_SLUG_CACHE.update(by_sym)
    return _ALPHA_SLUG_CACHE


def _now() -> int:
    return int(time.time())


def _load_json(p: Path, default):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return default


# ── Binance data fetchers ────────────────────────────────────────────────────

def _oi_hist(sym: str) -> list[tuple[int, float]]:
    d = _get(f"{FAPI}/futures/data/openInterestHist?symbol={sym}USDT&period=1h&limit={OI_LIMIT}")
    out = []
    for p in d or []:
        v = _f(p.get("sumOpenInterestValue"))
        t = p.get("timestamp")
        if v is not None and t is not None:
            out.append((int(t) // 1000, v))
    return sorted(out)


def _klines_1h(sym: str) -> list[tuple]:
    d = _get(f"{FAPI}/fapi/v1/klines?symbol={sym}USDT&interval=1h&limit={KLINE_LIMIT}")
    now_ms = _now() * 1000
    out = []
    for k in d or []:
        if len(k) < 7 or k[6] >= now_ms:
            continue
        out.append((int(k[0]) // 1000, _f(k[1]), _f(k[2]), _f(k[3]), _f(k[4]), _f(k[5])))
    return out


def _funding(sym: str) -> list[tuple[int, float]]:
    d = _get(f"{FAPI}/fapi/v1/fundingRate?symbol={sym}USDT&limit={FUND_LIMIT}")
    out = []
    for x in d or []:
        r, t = _f(x.get("fundingRate")), x.get("fundingTime")
        if r is not None and t is not None:
            out.append((int(t) // 1000, r))
    return sorted(out)


# ── Spark series (for tile sparklines + detail chart fallback) ───────────────

def _spark_series(klines: list) -> list:
    """Downsample 1H klines to ~SPARK_POINTS close prices.
    Returns [[t_sec, close], ...] ascending."""
    if len(klines) < 2:
        return []
    step = max(1, len(klines) // SPARK_POINTS)
    pts = klines[::step]
    if pts[-1][0] != klines[-1][0]:
        pts.append(klines[-1])
    return [[k[0], round(k[4], 8)] for k in pts]


# ── CMC market enrichment with price-checking ────────────────────────────────

def _cmc_detail_full(slug: str) -> dict | None:
    """Full CMC detail: price, FDV, mcap, vol, supplies, %changes, chain, TGE."""
    url = CMC_DETAIL_URL.format(slug=slug)
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers=CMC_UA)
            with urllib.request.urlopen(req, timeout=30) as r:
                payload = json.loads(r.read().decode("utf-8"))
        except Exception:
            time.sleep(3.0 * (attempt + 1))
            continue
        status = (payload.get("status") or {})
        if status.get("error_code") not in (0, "0", None):
            time.sleep(3.0 * (attempt + 1))
            continue
        data = payload.get("data") or {}
        if not data.get("name"):
            return None
        st = data.get("statistics") or {}
        chain, contract = None, None
        for p in (data.get("platforms") or []):
            cg = CMC_TO_CG_PLATFORM.get(p.get("contractPlatform"))
            if cg and p.get("contractAddress"):
                chain, contract = cg, p["contractAddress"]
                break
        return {
            "slug": slug, "name": data.get("name"), "symbol": data.get("symbol"),
            "price": st.get("price"),
            "fdv": st.get("fullyDilutedMarketCap"),
            "mcap": st.get("marketCap"),
            "vol24h": st.get("volume24h"),
            "circ_supply": st.get("circulatingSupply"),
            "total_supply": st.get("totalSupply"),
            "max_supply": st.get("maxSupply"),
            "p24h": st.get("priceChangePercentage24h"),
            "p7d": st.get("priceChangePercentage7d"),
            "p30d": st.get("priceChangePercentage30d"),
            "p90d": st.get("priceChangePercentage90d"),
            "chain": chain, "contract": contract,
            "tge": data.get("dateLaunched"),
            "ts": _now(),
        }
    return None


def _price_ok(cmc_price, bn_price) -> bool:
    """True if CMC and Binance perp prices are within PRICE_TOL of each other."""
    if not cmc_price or not bn_price or cmc_price <= 0 or bn_price <= 0:
        return False
    ratio = cmc_price / bn_price
    return (1.0 / PRICE_TOL) <= ratio <= PRICE_TOL


def _load_cmc_map() -> dict[str, list[str]]:
    """Symbol(upper) -> [slug, ...] from cmc_map.json (local-only fallback)."""
    entries = _load_json(CMC_MAP_FILE, [])
    if not isinstance(entries, list):
        return {}
    by_sym: dict[str, list] = {}
    for e in entries:
        sym = (e.get("symbol") or "").upper()
        if sym and e.get("slug") and e.get("is_active"):
            by_sym.setdefault(sym, []).append(e["slug"])
    return by_sym


def _resolve_market(sym: str, bn_price: float | None, wl_slug: str | None,
                    cmc_by_sym: dict, market_cache: dict,
                    allow_fetch: bool = True) -> tuple[dict | None, int]:
    """Price-verified CMC market data. Returns (market_dict | None, api_calls_made).
    Tries: watchlist slug -> alpha-tag slug -> lowercase-symbol guess -> cmc_map slugs.
    A slug is REJECTED if CMC price is off the Binance mark by >3x."""
    candidates = []
    if wl_slug:
        candidates.append(wl_slug)
    alpha_slug = _ALPHA_SLUG_CACHE.get(sym)
    if alpha_slug and alpha_slug not in candidates:
        candidates.append(alpha_slug)
    guess = sym.lower()
    if guess not in candidates:
        candidates.append(guess)
    for slug in cmc_by_sym.get(sym, []):
        if slug not in candidates:
            candidates.append(slug)

    fetched = 0
    for slug in candidates:
        cached = market_cache.get(slug)
        fresh = cached and (_now() - cached.get("ts", 0)) < MARKET_TTL_S
        if not fresh:
            if not allow_fetch:
                continue
            detail = _cmc_detail_full(slug)
            if detail:
                market_cache[slug] = detail
                fetched += 1
                time.sleep(1.5)
            cached = detail
        if not cached or not cached.get("price"):
            continue
        if not bn_price or _price_ok(cached["price"], bn_price):
            return cached, fetched
    return None, fetched


# ── Per-coin Binance pull ────────────────────────────────────────────────────

MARKET_KEYS = ("slug", "name", "price", "fdv", "mcap", "vol24h",
               "circ_supply", "total_supply", "max_supply",
               "p24h", "p7d", "p30d", "p90d",
               "chain", "contract", "tge")


def _log_fires(recs: dict) -> int:
    """Append every NEW (sym, setup, fire-hour) to the forward fire-log, keyed by
    sym|strat|t so a fire that persists across hourly runs is recorded ONCE.
    Records the entry reference (Binance mark price), the setup's stop/size, and
    the OI/funding/FDV context at fire time; `outcome` is filled in later by the
    grader. Never raises — logging must not break the hourly fetch."""
    try:
        log = json.loads(FIRES_LOG.read_text(encoding="utf-8")) if FIRES_LOG.exists() else []
        if not isinstance(log, list):
            log = []
    except Exception:
        log = []
    seen = {f"{e.get('sym')}|{e.get('strat')}|{e.get('t')}" for e in log}
    now = _now()
    added = 0
    for sym, r in recs.items():
        sig = r.get("signals") or {}
        for strat in ("v1", "v2", "v3", "v4"):
            d = sig.get(strat) or {}
            if not d.get("fired"):
                continue
            key = f"{sym}|{strat}|{d.get('t')}"
            if key in seen:
                continue
            seen.add(key)
            log.append({
                "sym": sym, "strat": strat, "t": d.get("t"),
                "logged_utc": now,
                "entry_price": r.get("mark_price"),
                "stop": d.get("stop"), "position": d.get("position"),
                "oi_combined": r.get("oi_combined"), "oi_bn": r.get("oi_bn"),
                "funding": r.get("funding"), "fdv": r.get("fdv"),
                "outcome": None,            # graded later by tools/grade_fires.py
            })
            added += 1
    try:
        FIRES_LOG.write_text(
            json.dumps(log[-FIRES_LOG_MAX:], ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8")
        print(f"  fires_log: +{added} new (total {min(len(log), FIRES_LOG_MAX)})")
    except Exception as e:
        print(f"  fires_log: write failed ({e})")
    return added


def _log_hourly(recs: dict) -> int:
    """Append one compact record per coin per hourly run to the month-sharded
    training series (cache/screener/hourly/<YYYY-MM>.jsonl). Captures OI / volume
    / funding / price / FDV so the model has a PERMANENT history beyond Binance's
    ~30d OI wall. Never raises — recording must not break the fetch."""
    import datetime as _dt
    try:
        HOURLY_DIR.mkdir(parents=True, exist_ok=True)
        month = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m")
        path = HOURLY_DIR / f"{month}.jsonl"
        lines, n = [], 0
        for sym, r in recs.items():
            if r.get("data") in ("no_perp", "error"):
                continue
            t = r.get("snap_t") or r.get("as_of")
            if not t:
                continue
            mkt = r.get("market") or {}
            rec = {"t": t, "sym": sym,
                   "oi_bn": r.get("oi_bn"), "oi_comb": r.get("oi_combined"),
                   "oi_byb": r.get("oi_byb"), "funding": r.get("funding"),
                   "fund_int_h": r.get("funding_interval_h"),
                   "price": r.get("mark_price"), "vol1h": r.get("vol1h"),
                   "vol24": mkt.get("vol24h"), "fdv": r.get("fdv"), "mcap": r.get("mcap")}
            lines.append(json.dumps(rec, separators=(",", ":")))
            n += 1
        with open(path, "a", encoding="utf-8") as f:
            f.write("\n".join(lines) + ("\n" if lines else ""))
        print(f"  hourly_history: +{n} rows -> {path.name}")
        return n
    except Exception as e:
        print(f"  hourly_history: write failed ({e})")
        return 0


def _fetch_token(sym: str, intervals: dict) -> dict:
    """Per-coin Binance pull + signal compute + spark series."""
    oi = _oi_hist(sym)
    kl = _klines_1h(sym)
    fund = _funding(sym)
    interval_h = intervals.get(f"{sym}USDT", 8) or 8
    cur_funding = fund[-1][1] if fund else None
    sig = evaluate(oi, kl, funding=fund, current_funding=cur_funding,
                   funding_interval_h=float(interval_h))
    oi_bn = oi[-1][1] if oi else None
    mark_price = kl[-1][4] if kl else None
    spark = _spark_series(kl)
    ok = bool(oi and kl)
    pump = (_pump_score(oi, kl, fund, _PUMP_MODEL)
            if (_pump_score and _PUMP_MODEL) else None)
    return {"oi_bn": oi_bn, "funding": cur_funding, "funding_interval_h": float(interval_h),
            "signals": sig, "as_of": sig.get("as_of"), "data": "ok" if ok else "partial",
            "mark_price": mark_price, "spark": spark, "pump_score": pump,
            # last-hour snapshot for the append-only training series (_log_hourly)
            "snap_t": int(oi[-1][0]) if oi else None,
            "vol1h": float(kl[-1][5]) if kl and len(kl[-1]) > 5 else None}


# ── main ─────────────────────────────────────────────────────────────────────

def main(argv: list[str]) -> int:
    OUTDIR.mkdir(parents=True, exist_ok=True)
    wl = _load_json(WATCHLIST, {})
    if not wl:
        print(f"{WATCHLIST} missing/empty -- run tools/seed_manip_watchlist.py first")
        return 1
    only = {a.upper() for a in argv}
    screenable = [s for s, r in wl.items() if r.get("has_perp") and (not only or s in only)]
    screenable.sort()

    # ── Shared one-shot pulls ────────────────────────────────────────────────
    intervals = {}
    try:
        intervals = _binance_intervals()
    except Exception:
        pass
    try:
        byb = _bulk_bybit()
    except Exception:
        byb = {}
    market_cache = _load_json(MARKET_CACHE_FILE, {})
    cmc_by_sym = _load_cmc_map()
    alpha_slugs = _load_alpha_slugs()
    print(f"  alpha-tag slugs: {len(alpha_slugs)}")

    # ── Per-coin Binance pull + signals (concurrent) ─────────────────────────
    # A token's Binance perp ticker can differ from its watchlist key (e.g. the
    # PlaysOut token trades as PLAYUSDT, Banana Gun as BANANAUSDT). Honour an
    # optional `perp_sym` override; fall back to the key.
    def _tkr(s):
        return wl[s].get("perp_sym") or s
    recs: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futs = {ex.submit(_fetch_token, _tkr(s), intervals): s for s in screenable}
        for fut, s in futs.items():
            try:
                recs[s] = fut.result()
            except Exception as e:
                recs[s] = {"data": "error", "error": f"{type(e).__name__}: {e}",
                           "signals": evaluate([], []), "oi_bn": None,
                           "mark_price": None, "spark": []}

    # ── Market enrichment: price-checked CMC data (capped) ───────────────────
    market_fetches = 0
    for s in screenable:
        rec = recs[s]
        slug = wl[s].get("cmc_slug")
        bn_price = rec.get("mark_price")
        allow = market_fetches < MARKET_MAX_PER_RUN
        mkt, n = _resolve_market(s, bn_price, slug, cmc_by_sym, market_cache, allow)
        market_fetches += n
        if mkt:
            rec["market"] = {k: mkt[k] for k in MARKET_KEYS if k in mkt}

    # ── Fallback FDV for live-perp tokens whose CMC page is deactivated ───────
    # The price-check rejects a CMC slug whose page no longer reports a price
    # (delisted/deactivated), so such tokens get no market block — yet their
    # Binance perp still trades. Derive a CURRENT FDV the project's own way:
    # mark price × total supply (last-known supply from the cached CMC detail).
    for s in screenable:
        rec = recs[s]
        if (rec.get("market") or {}).get("fdv"):
            continue
        bn_price = rec.get("mark_price")
        cand = market_cache.get(wl[s].get("cmc_slug") or "")
        ts = (cand or {}).get("total_supply")
        if bn_price and ts:
            m = {k: cand[k] for k in MARKET_KEYS if cand.get(k) is not None}
            m["price"] = bn_price
            m["fdv"] = bn_price * ts
            m["fdv_src"] = "perp_price_x_supply"
            rec["market"] = m

    # ── Bybit OI + FDV gate (uses market-verified FDV) ───────────────────────
    for s in screenable:
        rec = recs[s]
        mkt = rec.get("market") or {}
        fdv = mkt.get("fdv")
        mcap = mkt.get("mcap")
        oi_byb = (byb.get(_tkr(s)) or {}).get("oi_usd")
        oi_bn = rec.get("oi_bn")
        oi_comb = (oi_bn or 0) + (oi_byb or 0) if (oi_bn or oi_byb) else None
        rec.update({
            "oi_byb": oi_byb, "oi_combined": oi_comb, "fdv": fdv, "mcap": mcap,
            "oi_fdv_pct": (oi_comb / fdv * 100) if (oi_comb and fdv) else None,
            "pass_gate": bool(oi_comb and fdv and (oi_comb / fdv) >= GATE),
            "sections": wl[s].get("sections", []), "sources": wl[s].get("sources", []),
        })

    # ── Non-screenable coins (no Binance perp) ──────────────────────────────
    for s, r in wl.items():
        if not r.get("has_perp") and (not only or s in only):
            recs[s] = {"data": "no_perp", "sections": r.get("sections", []),
                       "sources": r.get("sources", []), "signals": evaluate([], []),
                       "spark": []}
            slug = r.get("cmc_slug")
            if slug and market_fetches < MARKET_MAX_PER_RUN:
                mkt, n = _resolve_market(s, None, slug, cmc_by_sym, market_cache, True)
                market_fetches += n
                if mkt:
                    recs[s]["market"] = {k: mkt[k] for k in MARKET_KEYS if k in mkt}

    # ── Best-effort holders (EVM contracts, capped) ──────────────────────────
    holder_fetches = 0
    try:
        from fetch.fetch_holders import fetch_holders as _hld_fetch
    except ImportError:
        _hld_fetch = None
    if _hld_fetch and HOLDER_MAX_PER_RUN > 0:
        HOLDERS_DIR.mkdir(parents=True, exist_ok=True)
        for s in screenable:
            if holder_fetches >= HOLDER_MAX_PER_RUN:
                break
            mkt = recs[s].get("market") or {}
            chain, contract = mkt.get("chain"), mkt.get("contract")
            if not chain or not contract:
                continue
            hf = HOLDERS_DIR / f"{s}.json"
            if hf.exists():
                try:
                    hdata = json.loads(hf.read_text(encoding="utf-8"))
                    if (_now() - hdata.get("fetched_at", 0)) < HOLDER_TTL_S:
                        continue
                except Exception:
                    pass
            try:
                h = _hld_fetch(s, chain, contract)
                hf.write_text(json.dumps(h, ensure_ascii=False), encoding="utf-8")
                holder_fetches += 1
                tag = f"top10={h['top10_share']}%" if h.get("available") else "unavailable"
                print(f"  holders {s}: {tag} on {chain}")
            except Exception as e:
                print(f"  holders {s}: error {e}")
            time.sleep(1.5)

    # ── Persist market cache ─────────────────────────────────────────────────
    MARKET_CACHE_FILE.write_text(
        json.dumps(market_cache, separators=(",", ":")), encoding="utf-8")

    # ── Forward fire-log + hourly training series (before mark_price stripped) ─
    _log_fires(recs)
    _log_hourly(recs)

    # ── Strip internal fields from output ────────────────────────────────────
    for rec in recs.values():
        rec.pop("mark_price", None)

    # ── Write screener.json ──────────────────────────────────────────────────
    def _fired(strat):
        return sum(1 for r in recs.values()
                   if (r.get("signals") or {}).get(strat, {}).get("fired"))

    as_of = max((r.get("as_of") for r in recs.values() if r.get("as_of")), default=None)
    payload = {
        "generated_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "as_of_hour": as_of,
        "gate": GATE,
        "thresholds": {k: getattr(signals, k) for k in dir(signals)
                       if k.isupper() and isinstance(getattr(signals, k), (int, float))},
        "counts": {
            "universe": len(recs), "screenable": len(screenable),
            "passing_gate": sum(1 for r in recs.values() if r.get("pass_gate")),
            "v1": _fired("v1"), "v2": _fired("v2"), "v3": _fired("v3"), "v4": _fired("v4"),
        },
        "tokens": recs,
    }
    OUT.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
    c = payload["counts"]
    mkt_ok = sum(1 for r in recs.values() if r.get("market"))
    print(f"wrote {OUT}")
    print(f"  screenable={c['screenable']}  gate-pass={c['passing_gate']}  "
          f"v1={c['v1']} v2={c['v2']} v3={c['v3']} v4={c['v4']}")
    print(f"  market={mkt_ok}  (CMC fetched {market_fetches})  "
          f"holders={holder_fetches}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
