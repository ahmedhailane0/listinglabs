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
KLINE_5M_LIMIT = 288          # ~24h of 5m closes for the detail-page 24h view
FUND_LIMIT = 20
GATE = 0.08
WORKERS = 6
SPARK_POINTS = 120
# Telegram quality gate: only PING for a v1/v4 fire whose pump-score (model
# confidence, 0-100) clears this — standouts only. Scores run low (rare-event
# model) so this is strict by design. It gates ONLY the Telegram send: every fire
# is still logged + graded, and every token still shows on the Manipulated list —
# nothing is removed from the page. A fire with no score (model unavailable) still
# sends — fail-open, never mute a real fire just because the score is missing.
# Tune in one place here.
ALERT_MIN_PUMP = 7.0
# Only forward-proven setups may ping Telegram; everything else in the fire-log
# (dump short, bear_trap / spike_retrace price-action longs, and now probe) is
# journal-only until its live ledger earns promotion.
# probe DEMOTED to journal-only on 2026-07-05: its live ledger was a net loser
# across its whole alertable population — 59 graded fires, 30.5% win, -$67.67
# cumulative @ $100/trade, and EVERY pump-score band below 30 is negative
# (0-5 -$46, 5-10 -$55, 10-15 -$28, 15-20 -$15). Only the 30+ band is positive
# (+$86) but on just 3 fires — too thin to keep alerting on. Still logged +
# graded below, so it earns alerts back if that high-score edge holds up.
ALERT_STRATS = {"v1", "v4", "v2"}
# Every detector that goes into the fire-log (order = log order).
LOGGED_STRATS = ("v1", "v4", "v2", "probe", "dump", "bear_trap", "spike_retrace")
# Breadth gate: when MORE than this many coins fire the SAME setup in the SAME
# hour, that's one market-wide event (BTC move / broad volume burst), not N
# independent coin setups. Those fires are tagged market_wide (the graders
# collapse each cluster to one sample) and their Telegram pings are skipped —
# market beta, not a coin story. Probe is the main offender (it once flagged
# 46/141 coins across a 72h window).
BREADTH_MAX = 7

# Score-watch ping: a SECOND net, independent of any fired setup. When the model
# flags a coin as a standout (pump_score >= this) we ping even if no v1/v4/probe
# setup fired — the safety net for pumps whose pattern none of the discrete
# setups capture. Deliberately a HIGH bar (standouts only; most coins clear 7,
# so 7 here would spam) and clearly labelled a heads-up, NOT a setup entry — the
# score is coincident, so this tends to flag a move in progress, not before it.
# Per-coin cooldown so a coin parked above the line doesn't re-ping hourly.
# Both thresholds are tuned in this one place.
ALERT_SCORE_WATCH = 35.0
SCORE_WATCH_COOLDOWN_S = 24 * 3600
SCORE_WATCH_FILE = OUTDIR / "score_watch.json"

# Buy-signal play-by-play on the +50% pump odds (pump_score). When a coin's +50%
# chance crosses ALERT_BUY_ODDS we ping Telegram, then ping again on each NEW HIGH
# while it stays above, and once when it drops back under (re-arming a later
# re-cross). Per-coin state in BUY_WATCH_FILE. Logs ONE journal fire (strat
# "buy15") on the first crossing (see _log_fires) so the Buy Signals journal can
# score it 72h later. Hourly cadence (the model only re-scores each closed hour).
# Raised 15->20 on 2026-07-05: graded live fires showed the 15-20% score band was
# the sole source of the journal's net loss (-$47 of -$33 total across 25 trades,
# mostly clean stop-outs with no real move), while 20%+ was ~breakeven/positive
# (still a thin sample, ~9 trades) — see memory trade_journal_pnl_card.md.
ALERT_BUY_ODDS = 20.0
BUY_WATCH_FILE = OUTDIR / "buy_watch.json"
BUY_LINK_BASE = "https://ahmedhailane0.github.io/listinglabs/scams/"

# Smooth intraday close candles per perp, for the Manipulated detail-page price
# chart (CMC-style 24h/1W). 5m (last ~24h) + 1h (last ~12d); the daily history
# fills the longer ranges at build time. Written hourly by this box; committed
# by deploy/screener_cron.sh. See build/build_scams.py (_price_chart stitch).
PRICE_CANDLES_DIR = CACHE / "price_candles"

# ── Market enrichment (CMC keyless detail endpoint) ──────────────────────────
MARKET_CACHE_FILE = OUTDIR / "market.json"
MARKET_TTL_S = 6 * 3600      # refresh a coin's market data at most every 6h
MARKET_MAX_PER_RUN = 40       # cap CMC detail calls per run
CMC_DETAIL_URL = "https://api.coinmarketcap.com/data-api/v3/cryptocurrency/detail?slug={slug}"
CMC_UA = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
CMC_MAP_FILE = HERE.parent / "cmc_map.json"   # may not exist on the box (gitignored)
PRICE_TOL = 3.0               # CMC price must be within 3x of Binance perp mark
FDV_DIVERGE = 1.20            # accepted CMC price >20% off the live mark → derive
                              # FDV/mcap from mark × CMC supply instead (audit F-18)

# ── Forward fire-log (out-of-sample ledger) ─────────────────────────────────
# Append-only record of every NEW (sym, setup, fire-hour) the screener reports,
# with the entry reference price + stop, so tools/grade_fires.py can score the
# outcome once the future arrives. This is the clean out-of-sample evidence the
# tuner (tools/optimize_signals.py) validates against — it accrues forever.
FIRES_LOG = OUTDIR / "fires_log.json"
FIRES_LOG_MAX = 8000
FIRE_REFRACTORY_H = 72        # one fire per (sym, setup) per 72h — collapses a setup
                              # that fires on several consecutive bars for one event
                              # into a single trade/alert (matches the 72h time-stop).

# Local hand-off from fetch/screener_daemon.py (real-time websocket alerter, runs
# as its own systemd service on the box — NOT git-tracked, not committed). It
# appends a line per NEW live fire it Telegram-alerted; _log_fires folds those
# into the real ledger below so the daemon's dedup key is present BEFORE this
# hour's own scan runs, which makes the cron's independent re-derivation of the
# same fire a no-op (no duplicate log row, no duplicate Telegram send).
DAEMON_FIRES_LIVE = OUTDIR / "daemon" / "fires_live.jsonl"

# Optional pump-probability scorer (LOCAL/BOX-only module + model; absent in CI,
# so this no-ops there). When present, writes a 0-100 `pump_score` per coin.
try:
    from tools.pump_score import (load_model as _load_pump_model,
                                  load_model_logistic as _load_pump_model_log,
                                  load_model_25 as _load_pump_model_25,
                                  load_model_10 as _load_pump_model_10,
                                  score_series as _pump_score)
    _PUMP_MODEL = _load_pump_model()           # +50% (the headline Pump %)
    _PUMP_MODEL_LOG = _load_pump_model_log()   # previous-gen logistic, for the "Old %" column
    _PUMP_MODEL_25 = _load_pump_model_25()     # +25% (pump-odds ladder)
    _PUMP_MODEL_10 = _load_pump_model_10()     # +10% (pump-odds ladder)
except Exception:
    _pump_score = None
    _PUMP_MODEL = None
    _PUMP_MODEL_LOG = None
    _PUMP_MODEL_25 = None
    _PUMP_MODEL_10 = None

# Optional Telegram alerts (reads token from env; no-ops when unconfigured).
try:
    from fetch import notify_telegram as _tg
except Exception:
    _tg = None

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


def _klines_5m(sym: str) -> list[tuple]:
    d = _get(f"{FAPI}/fapi/v1/klines?symbol={sym}USDT&interval=5m&limit={KLINE_5M_LIMIT}")
    now_ms = _now() * 1000
    out = []
    for k in d or []:
        if len(k) < 7 or k[6] >= now_ms:     # drop the still-forming last candle
            continue
        out.append((int(k[0]) // 1000, _f(k[4])))   # (t_sec, close)
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


def _r8(v):
    """Round to 8 significant figures — keeps sub-cent token prices precise while
    shaving committed-cache bytes (data-correctness: significant figures, not a
    fixed decimal count that would crush a $0.00004 price to 4 sig-figs)."""
    if v is None or v == 0:
        return v
    from math import floor, log10
    return round(v, 7 - floor(log10(abs(v))))


def _candle_series(rows: list, close_idx: int) -> list:
    """[[t_sec, close], ...] ascending from kline rows (close at close_idx)."""
    return [[r[0], _r8(r[close_idx])] for r in rows if r[close_idx] is not None]


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


def _fmt_px(x) -> str:
    if x is None:
        return "?"
    return f"{x:,.4f}".rstrip("0").rstrip(".") if x >= 1 else f"{x:.6g}"


def _esc(s) -> str:
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _pump_pct(s) -> str:
    """The pump score is a calibrated P(+50% within 72h), so show it as a percent
    in the alerts (matches the site). Sub-1% -> "<1%" (HTML-escaped for Telegram's
    HTML parse mode). None -> "n/a"."""
    if s is None:
        return "n/a"
    return f"{s:.0f}%" if s >= 1 else "&lt;1%"


def _alert_buy(e: dict, r: dict) -> bool:
    """Send a Telegram BUY alert for a brand-new v1/v4 fire (tiered exit:
    TP1 +25% sell half, TP2 +50% sell rest, the setup's own stop, 72h time-stop).
    Returns True iff a message was actually sent. No-ops when Telegram is
    unconfigured — a missing token never blocks logging. Never raises."""
    try:
        if not _tg or not _tg.enabled():
            return False
        px, stop = e.get("entry_price"), e.get("stop")
        if not px:
            return False
        # Quality gate: skip the ping for weak-confidence fires (the fire is still
        # logged + graded by the caller — only the Telegram SEND is suppressed).
        score = e.get("pump_score")
        if score is not None and score < ALERT_MIN_PUMP:
            print(f"  telegram: skip {e['sym']} {e['strat']} — pump {score:.0f} < {ALERT_MIN_PUMP:.0f}")
            return False
        name = (r.get("market") or {}).get("name") or e["sym"]
        oifdv, fund = r.get("oi_fdv_pct"), e.get("funding")
        stop_line = (f"Stop   ${_fmt_px(stop)}  ({(stop/px-1)*100:+.0f}%) — exit if wrong"
                     if stop else "Stop   n/a")
        ctx = f"Size {e.get('position') or '?'}"
        if score is not None:
            ctx += f" · pump chance {_pump_pct(score)}"
        if oifdv is not None:
            ctx += f" · OI/FDV {oifdv:.0f}%"
        if fund is not None:
            ctx += f" · funding {fund*100:+.4f}%"
        msg = "\n".join([
            f"\U0001F7E2 <b>BUY — {_esc(e['sym'])}</b> ({_esc(name)})  [{e['strat']}]",
            f"Entry  ~ ${_fmt_px(px)}",
            stop_line,
            f"TP1    +25% → ${_fmt_px(e.get('tp1'))}  (sell half)",
            f"TP2    +50% → ${_fmt_px(e.get('tp2'))}  (sell rest)",
            "Time-stop: 72h",
            ctx,
            "⚠ experimental signal — not financial advice",
        ])
        return _tg.send(msg)
    except Exception as ex:
        print(f"  telegram buy-alert failed: {ex}")
        return False


def _alert_result(e: dict) -> bool:
    """Send a Telegram RESULT follow-up for a fire that has now been graded — the
    close of the loop on the BUY alert, so the bot is a self-scoring journal. The
    tier reached is inferred from the max favourable excursion vs the +25%/+50%
    take-profits; P&L is the grader's realised return. No-ops when Telegram is
    unconfigured. Never raises."""
    try:
        if not _tg or not _tg.enabled():
            return False
        o = e.get("outcome") or {}
        pnl, mfe = o.get("pnl_ownstop"), o.get("mfe")
        if pnl is None:
            return False
        win = pnl > 0
        tp1 = mfe is not None and mfe >= 0.25
        tp2 = bool(o.get("pump")) or (mfe is not None and mfe >= 0.50)
        head = "✅" if win else "\U0001F534"          # ✅ / 🔴
        tier = ("TP2 +50% ✓ (sold rest)" if tp2 else
                "TP1 +25% ✓ (sold half)" if tp1 else
                "no TP — stop / 72h time-stop")
        name = (e.get("name") or e.get("sym"))
        strat = e.get("strat")
        label = "buy signal" if strat == "buy15" else strat
        lines = [
            f"{head} <b>RESULT — {_esc(e.get('sym'))}</b> ({_esc(name)})  "
            f"[{label}]  {'WIN' if win else 'LOSS'}",
            f"Entry  ~ ${_fmt_px(e.get('entry_price'))}",
            (f"Best   {mfe*100:+.0f}%" if mfe is not None else "Best   n/a"),
            tier,
            f"P&L    {pnl*100:+.1f}%   (own-stop · 72h)",
        ]
        sc = e.get("pump_score")
        if sc is not None:
            lines.append(f"pump chance was {_pump_pct(sc)}")
        return _tg.send("\n".join(lines))
    except Exception as ex:
        print(f"  telegram result-alert failed: {ex}")
        return False


def _send_results() -> int:
    """Sweep the fire-log for graded fires that were BUY-alerted but not yet
    result-alerted, and send a RESULT follow-up for each (closing the loop one
    hourly run after the nightly grader fills an outcome). Mirrors the BUY gate:
    only fires that cleared ALERT_MIN_PUMP get a result, so weak fires stay quiet
    on both ends. Marks `result_alerted` to send exactly once. Never raises."""
    if not _tg or not _tg.enabled():
        return 0
    try:
        log = json.loads(FIRES_LOG.read_text(encoding="utf-8")) if FIRES_LOG.exists() else []
        if not isinstance(log, list):
            return 0
    except Exception:
        return 0
    sent, changed = 0, False
    for e in log:
        sc = e.get("pump_score")
        if (e.get("alerted") and e.get("outcome") and not e.get("result_alerted")
                and (sc is None or sc >= ALERT_MIN_PUMP)):
            if _alert_result(e):
                e["result_alerted"] = True
                e["result_alerted_utc"] = _now()
                sent += 1
                changed = True
                time.sleep(0.5)               # pace a backlog clearing at once
    if changed:
        try:
            FIRES_LOG.write_text(
                json.dumps(log[-FIRES_LOG_MAX:], ensure_ascii=False, separators=(",", ":")),
                encoding="utf-8")
        except Exception as ex:
            print(f"  fires_log: result-flag write failed ({ex})")
    if sent:
        print(f"  telegram: sent {sent} result follow-up(s)")
    return sent


def _alert_score_watch(recs: dict) -> int:
    """Second alert net: ping when the model rates a coin a standout
    (pump_score >= ALERT_SCORE_WATCH) even if NO discrete setup fired — the
    safety net for pumps whose shape v1/v4/probe don't capture. Suppressed for a
    coin that already got a BUY alert this cycle (any of v1/v4/probe fired AND
    cleared the gate, so it was already sent). Per-coin cooldown
    (SCORE_WATCH_COOLDOWN_S) via SCORE_WATCH_FILE so a coin parked above the line
    doesn't re-ping every hour. No-ops without Telegram. Never raises."""
    if not _tg or not _tg.enabled():
        return 0
    try:
        state = json.loads(SCORE_WATCH_FILE.read_text(encoding="utf-8")) \
            if SCORE_WATCH_FILE.exists() else {}
        if not isinstance(state, dict):
            state = {}
    except Exception:
        state = {}
    now = _now()
    sent, changed = 0, False
    for sym, r in recs.items():
        score = r.get("pump_score")
        if score is None or score < ALERT_SCORE_WATCH:
            continue
        # Already BUY-alerted this coin (a gated setup fired)? Don't double-ping.
        sig = r.get("signals") or {}
        if any((sig.get(s) or {}).get("fired") for s in ("v1", "v4", "probe")) \
                and score >= ALERT_MIN_PUMP:
            continue
        last = state.get(sym, {}).get("t", 0)
        if now - last < SCORE_WATCH_COOLDOWN_S:
            continue
        try:
            px = r.get("mark_price")
            spark = r.get("spark") or []
            chg = ((spark[-1] / spark[0] - 1) * 100
                   if len(spark) >= 2 and spark[0] else None)
            name = (r.get("market") or {}).get("name") or sym
            oifdv, fund = r.get("oi_fdv_pct"), r.get("funding")
            ctx = f"pump chance {_pump_pct(score)}"
            if chg is not None:
                ctx += f" · ~24h {chg:+.0f}%"
            if oifdv is not None:
                ctx += f" · OI/FDV {oifdv:.0f}%"
            if fund is not None:
                ctx += f" · funding {fund*100:+.4f}%"
            msg = "\n".join([
                f"\U0001F440 <b>WATCH — {_esc(sym)}</b> ({_esc(name)})",
                f"Model standout — no setup fired (heads-up, NOT an entry).",
                (f"Price  ~ ${_fmt_px(px)}" if px else "Price  n/a"),
                ctx,
                "⚠ score is coincident — may already be moving. Not financial advice.",
            ])
            if _tg.send(msg):
                state[sym] = {"t": now, "score": score}
                sent += 1
                changed = True
                time.sleep(0.5)
        except Exception as ex:
            print(f"  score-watch {sym} failed: {ex}")
    if changed:
        try:
            SCORE_WATCH_FILE.write_text(
                json.dumps(state, ensure_ascii=False, separators=(",", ":")),
                encoding="utf-8")
        except Exception as ex:
            print(f"  score_watch: state write failed ({ex})")
    if sent:
        print(f"  telegram: sent {sent} score-watch ping(s)")
    return sent


def _alert_buy_climb(recs: dict) -> int:
    """Buy-signal play-by-play on the +50% pump odds (pump_score). For each coin:
    a 🚀 ping the first time it crosses ALERT_BUY_ODDS, a 📈 ping on each NEW HIGH
    while it stays above, and a 🔻 'signal off' when it drops back under (which
    re-arms a later re-cross). Per-coin state in BUY_WATCH_FILE. Hourly cadence;
    no-op without Telegram; never raises."""
    if not _tg or not _tg.enabled():
        return 0
    try:
        state = json.loads(BUY_WATCH_FILE.read_text(encoding="utf-8")) \
            if BUY_WATCH_FILE.exists() else {}
        if not isinstance(state, dict):
            state = {}
    except Exception:
        state = {}
    now = _now()
    sent, changed = 0, False
    for sym, r in recs.items():
        score = r.get("pump_score")
        if score is None:
            continue
        st = state.get(sym) or {}
        armed = bool(st.get("armed"))
        last = st.get("last") or 0.0
        kind = None
        if score >= ALERT_BUY_ODDS:
            if not armed:
                kind = "buy"                      # first crossing
            elif score > last + 0.05:             # a new high (epsilon vs float noise)
                kind = "climb"
        elif armed:
            kind = "off"                          # dropped back under the line
        if kind is None:
            continue
        try:
            px = r.get("mark_price")
            name = (r.get("market") or {}).get("name") or sym
            ladder = (f"+50% {_pump_pct(score)} · +25% {_pump_pct(r.get('pump_score_25'))}"
                      f" · +10% {_pump_pct(r.get('pump_score_10'))}")
            link = BUY_LINK_BASE + f"{sym.lower()}.html"
            if kind == "buy":
                msg = "\n".join([
                    f"\U0001F680 <b>BUY SIGNAL — {_esc(sym)}</b> ({_esc(name)})",
                    f"+50% pump chance just crossed {ALERT_BUY_ODDS:.0f}% → now {_pump_pct(score)}",
                    f"odds  {ladder}",
                    (f"Price ~ ${_fmt_px(px)}" if px else "Price n/a"),
                    link,
                    "Research signal, not financial advice.",
                ])
            elif kind == "climb":
                msg = "\n".join([
                    f"\U0001F4C8 <b>{_esc(sym)}</b> +50% chance now {_pump_pct(score)} "
                    f"↑ from {_pump_pct(last)}",
                    f"odds  {ladder}",
                    (f"Price ~ ${_fmt_px(px)}" if px else ""),
                ])
            else:  # off
                msg = (f"\U0001F53B <b>{_esc(sym)}</b> buy signal off — "
                       f"+50% chance back under {ALERT_BUY_ODDS:.0f}% (now {_pump_pct(score)})")
            if _tg.send(msg):
                if kind == "off":
                    state[sym] = {"armed": False, "last": 0.0, "t": now}
                else:
                    state[sym] = {"armed": True, "last": score, "t": now}
                sent += 1
                changed = True
                time.sleep(0.5)
        except Exception as ex:
            print(f"  buy-climb {sym} failed: {ex}")
    if changed:
        try:
            BUY_WATCH_FILE.write_text(
                json.dumps(state, ensure_ascii=False, separators=(",", ":")),
                encoding="utf-8")
        except Exception as ex:
            print(f"  buy_watch: state write failed ({ex})")
    if sent:
        print(f"  telegram: sent {sent} buy-climb ping(s)")
    return sent


def _alert_targets(recs: dict) -> int:
    """Instant heads-up: ping the moment an OPEN buy-signal (buy15) fire reaches
    +25% or +50% above its entry — so you hear about a hit right away instead of
    waiting for the 72h grade. Each tier pings once (de-dup stored on the fire as
    `tp_alerted`). The final graded WIN/LOSS (net P&L) still comes at 72h via
    _send_results. Hourly cadence; no-op without Telegram; never raises."""
    if not _tg or not _tg.enabled():
        return 0
    try:
        log = json.loads(FIRES_LOG.read_text(encoding="utf-8")) if FIRES_LOG.exists() else []
        if not isinstance(log, list):
            return 0
    except Exception:
        return 0
    now = _now()
    sent, changed = 0, False
    for e in log:
        if e.get("strat") != "buy15" or e.get("outcome"):
            continue                              # only OPEN buy-signal fires
        t0 = e.get("t") or 0
        if not t0 or now - t0 > 72 * 3600:
            continue                              # past the 72h window — grader covers it
        entry = e.get("entry_price")
        rec = recs.get(e.get("sym"))
        cur = rec.get("mark_price") if rec else None
        if not entry or not cur:
            continue
        gain = cur / entry - 1
        done = set(e.get("tp_alerted") or [])
        if gain >= 0.50 and "tp2" not in done:
            tier = ("tp2", "+50%", "\U0001F3AF\U0001F3AF")
        elif gain >= 0.25 and "tp1" not in done:
            tier = ("tp1", "+25%", "\U0001F3AF")
        else:
            continue
        try:
            sym = e.get("sym")
            name = (rec.get("market") or {}).get("name") or sym
            msg = "\n".join([
                f"{tier[2]} <b>TARGET HIT — {_esc(sym)}</b> ({_esc(name)})  {tier[1]}",
                f"Buy-signal entry ~ ${_fmt_px(entry)} → now ${_fmt_px(cur)} ({gain*100:+.0f}%)",
                "Final win/loss (net P&L) grades at 72h.",
            ])
            if _tg.send(msg):
                done.add(tier[0])
                if tier[0] == "tp2":
                    done.add("tp1")               # +50% implies +25%
                e["tp_alerted"] = sorted(done)
                sent += 1
                changed = True
                time.sleep(0.5)
        except Exception as ex:
            print(f"  target-alert {e.get('sym')} failed: {ex}")
    if changed:
        try:
            FIRES_LOG.write_text(
                json.dumps(log[-FIRES_LOG_MAX:], ensure_ascii=False, separators=(",", ":")),
                encoding="utf-8")
        except Exception as ex:
            print(f"  fires_log: target-alert write failed ({ex})")
    if sent:
        print(f"  telegram: sent {sent} target-hit ping(s)")
    return sent


def _merge_daemon_fires(log: list) -> int:
    """Fold cache/screener/daemon/fires_live.jsonl (screener_daemon.py's
    real-time fires, already Telegram-alerted) into `log` in place, then clear
    the hand-off file. Must run BEFORE the caller builds its `seen` set so a
    fire the daemon already alerted doesn't get independently re-detected and
    re-alerted by this hour's own scan. Never raises."""
    if not DAEMON_FIRES_LIVE.exists():
        return 0
    try:
        lines = DAEMON_FIRES_LIVE.read_text(encoding="utf-8").splitlines()
    except Exception:
        return 0
    seen = {f"{e.get('sym')}|{e.get('strat')}|{e.get('t')}" for e in log}
    added = 0
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            e = json.loads(line)
        except Exception:
            continue
        key = f"{e.get('sym')}|{e.get('strat')}|{e.get('t')}"
        if key in seen:
            continue
        seen.add(key)
        log.append(e)
        added += 1
    try:
        DAEMON_FIRES_LIVE.unlink()
    except Exception as ex:
        print(f"  daemon fires: hand-off cleanup failed ({ex})")
    if added:
        print(f"  daemon fires: merged {added} real-time fire(s)")
    return added


def _log_fires(recs: dict) -> int:
    """Append every NEW (sym, setup, fire-hour) to the forward fire-log, keyed by
    sym|strat|t so a fire that persists across hourly runs is recorded ONCE.
    Records the entry reference (Binance mark price), the setup's stop/size, the
    tiered take-profit targets (entry +25% / +50%), and the OI/funding/FDV
    context at fire time; `outcome` is filled in later by the grader. Each new
    v1/v4 fire also pushes a Telegram BUY alert (no-op without a token). Never
    raises — logging/alerting must not break the hourly fetch."""
    try:
        log = json.loads(FIRES_LOG.read_text(encoding="utf-8")) if FIRES_LOG.exists() else []
        if not isinstance(log, list):
            log = []
    except Exception:
        log = []
    _merge_daemon_fires(log)
    seen = {f"{e.get('sym')}|{e.get('strat')}|{e.get('t')}" for e in log}
    # Per-(sym,strat) last fire time, for the refractory below. A breakout setup can
    # fire on several consecutive hourly bars for ONE event (e.g. v2 ignition fires
    # ~3 bars while OI confirms) — without this, that's 3 duplicate alerts/log rows
    # for the same trade. One fire per setup per FIRE_REFRACTORY_H matches the
    # backtest's one-trade-per-horizon counting and the 72h time-stop (you're
    # already in the trade; don't re-enter it).
    last_fire: dict[tuple, int] = {}
    for e in log:
        k = (e.get("sym"), e.get("strat"))
        ft = e.get("t") or 0
        if ft > last_fire.get(k, 0):
            last_fire[k] = ft
    now = _now()
    added = 0
    # breadth per (setup, fire-hour) across the whole universe, for the gate below
    breadth: dict[tuple, int] = {}
    for r in recs.values():
        sig = r.get("signals") or {}
        for strat in LOGGED_STRATS:
            d = sig.get(strat) or {}
            if d.get("fired") and d.get("t"):
                k = (strat, d["t"])
                breadth[k] = breadth.get(k, 0) + 1
    for sym, r in recs.items():
        sig = r.get("signals") or {}
        # v3 never fires. v1/v4 are the accumulation setups (quiet OI build under a
        # flat price). v2 (IGNITION, replaced the old coincident EMA-cross v2 on
        # 2026-06-27) is the BREAKOUT setup that catches pumps which skip the
        # accumulation tell entirely (e.g. VELVET +122% on 2026-06-26, which v1/v4
        # missed) via an OI-confirmed coil-break. The proven setups (ALERT_STRATS)
        # go into the fire-log + Telegram, sharing the ALERT_MIN_PUMP gate in
        # _alert_buy. The rest are JOURNAL-ONLY: logged so grade_fires can build
        # their forward ledger, but never alerted and never rendered as buys (the
        # site journals filter to explicit strat allowlists) — `dump` is a SHORT,
        # the price-action longs (bear_trap / spike_retrace, added 2026-07-02)
        # must earn alerting on live fires first, and `probe` (pure price/volume
        # coil-break) was demoted here on 2026-07-05 after its live ledger came in
        # a net loser (see the ALERT_STRATS note above).
        for strat in LOGGED_STRATS:
            d = sig.get(strat) or {}
            if not d.get("fired"):
                continue
            key = f"{sym}|{strat}|{d.get('t')}"
            if key in seen:
                continue
            ft = d.get("t") or 0
            if ft - last_fire.get((sym, strat), 0) < FIRE_REFRACTORY_H * 3600:
                continue                       # same trade still inside the refractory
            seen.add(key)
            last_fire[(sym, strat)] = ft
            px = r.get("mark_price")
            entry = {
                "sym": sym, "strat": strat, "t": d.get("t"),
                "logged_utc": now,
                "entry_price": px,
                "stop": d.get("stop"), "position": d.get("position"),
                "tp1": (px * 1.25 if px else None),   # +25% — sell half
                "tp2": (px * 1.50 if px else None),   # +50% — sell rest
                "time_stop_h": 72,
                "oi_combined": r.get("oi_combined"), "oi_bn": r.get("oi_bn"),
                "funding": r.get("funding"), "fdv": r.get("fdv"),
                "pump_score": r.get("pump_score"),
                "alerted": False,
                "outcome": None,            # graded later by tools/grade_fires.py
            }
            wide = breadth.get((strat, ft), 0) > BREADTH_MAX
            if wide:
                entry["market_wide"] = True
            if strat == "dump":
                # short: the long tiered targets don't apply — cover at -20%
                entry["side"] = "short"
                entry["tp1"], entry["tp2"] = None, (px * 0.80 if px else None)
            elif strat in ALERT_STRATS and wide:
                print(f"  telegram: skip {sym} {strat} — market-wide "
                      f"({breadth[(strat, ft)]} coins fired the same hour)")
            elif strat in ALERT_STRATS and _alert_buy(entry, r):
                entry["alerted"] = True
                entry["alerted_utc"] = now
            log.append(entry)
            added += 1
        # Buy-signal journal fire: ONE per coin per 72h, logged the first time the
        # +50% odds (pump_score) cross ALERT_BUY_ODDS. Separate from the discrete
        # setups above — this is the "Buy Signals" track. The play-by-play Telegram
        # pings (_alert_buy_climb) are notifications and do NOT add journal rows;
        # grade_fires scores this exactly like any other fire (strat-agnostic).
        ps = r.get("pump_score")
        th = r.get("as_of") or sig.get("as_of")
        if ps is not None and ps >= ALERT_BUY_ODDS and th:
            key = f"{sym}|buy15|{th}"
            if key not in seen and (th - last_fire.get((sym, "buy15"), 0)
                                    >= FIRE_REFRACTORY_H * 3600):
                seen.add(key)
                last_fire[(sym, "buy15")] = th
                px = r.get("mark_price")
                log.append({
                    "sym": sym, "strat": "buy15", "t": th, "logged_utc": now,
                    "entry_price": px,
                    "stop": (px * 0.85 if px else None), "position": "research",
                    "tp1": (px * 1.25 if px else None), "tp2": (px * 1.50 if px else None),
                    "time_stop_h": 72,
                    "oi_combined": r.get("oi_combined"), "oi_bn": r.get("oi_bn"),
                    "funding": r.get("funding"), "fdv": r.get("fdv"),
                    "pump_score": ps, "pump_score_25": r.get("pump_score_25"),
                    "pump_score_10": r.get("pump_score_10"),
                    # alerted=True: the play-by-play (_alert_buy_climb) pings the user
                    # on this crossing, so _send_results will close the loop with a
                    # WIN/LOSS Telegram once this fire is graded 72h later.
                    "alerted": True, "alerted_utc": now, "outcome": None,
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
    # Smooth intraday close candles for the detail-page price chart: 5m (~24h,
    # one extra call) + 1h (reuse `kl`, ~12d). Written to its own per-token file
    # in main(); stripped from screener.json. Best-effort — never block a coin.
    try:
        m5 = _candle_series(_klines_5m(sym), 1)
    except Exception:
        m5 = []
    candles = {"m5": m5, "h1": _candle_series(kl, 4)} if (m5 or kl) else None
    ok = bool(oi and kl)
    def _ps(model):
        return _pump_score(oi, kl, fund, model) if (_pump_score and model) else None
    pump = _ps(_PUMP_MODEL)
    pump_log = _ps(_PUMP_MODEL_LOG)
    pump_25 = _ps(_PUMP_MODEL_25)
    pump_10 = _ps(_PUMP_MODEL_10)
    # Logical nesting floor: reaching a bigger move REQUIRES passing the smaller
    # ones first, so P(+10%) >= P(+25%) >= P(+50%). The three models are fit
    # independently and can violate this by estimation noise — clamp to the
    # constraint so the odds ladder is always consistent.
    if pump_25 is not None and pump is not None:
        pump_25 = max(pump_25, pump)
    if pump_10 is not None:
        pump_10 = max(v for v in (pump_10, pump_25, pump) if v is not None)
    return {"oi_bn": oi_bn, "funding": cur_funding, "funding_interval_h": float(interval_h),
            "signals": sig, "as_of": sig.get("as_of"), "data": "ok" if ok else "partial",
            "mark_price": mark_price, "spark": spark, "pump_score": pump,
            "pump_score_log": pump_log, "pump_score_25": pump_25, "pump_score_10": pump_10,
            "_candles": candles,
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

    # ── Divergence guard (2026-07-09 audit F-18): CMC's price can lag a fast
    # mover by 15-35%+ between hourly runs (measured: TAG 36%, US 20%, LAB 19%),
    # which drags FDV/mcap with it while the live layer already shows the real
    # mark. When an ACCEPTED CMC price is > FDV_DIVERGE off the Binance mark,
    # re-derive price/FDV/mcap from the mark × CMC's (slow-moving) supply and
    # tag the source, same convention as the deactivated-page fallback above.
    for s in screenable:
        rec = recs[s]
        mkt = rec.get("market")
        bn_price = rec.get("mark_price")
        if not mkt or not bn_price or mkt.get("fdv_src"):
            continue
        cp = mkt.get("price")
        if not cp or not (cp / bn_price > FDV_DIVERGE or bn_price / cp > FDV_DIVERGE):
            continue
        tot, circ = mkt.get("total_supply"), mkt.get("circ_supply")
        if not tot:
            continue
        mkt["price"] = bn_price
        mkt["fdv"] = bn_price * tot
        if circ:
            mkt["mcap"] = bn_price * circ
        mkt["fdv_src"] = "mark_x_cmc_supply"

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
    # Close the loop: RESULT follow-ups for any BUY-alerted fire the nightly grader
    # has since scored (reads the fires_log _log_fires just wrote, with outcomes).
    _send_results()
    # Second net: ping standout pump-scores with no fired setup (runs before
    # mark_price is stripped below, so the price is still available).
    _alert_score_watch(recs)
    # Buy-signal play-by-play: ping when the +50% odds cross 15% and on each new
    # high while above (the owner's "watch it climb" feed). Same pre-strip spot.
    _alert_buy_climb(recs)
    # Instant target-hit heads-up: ping the moment an open buy-signal reaches
    # +25%/+50% (don't wait for the 72h grade). Needs live mark_price (pre-strip).
    _alert_targets(recs)

    # ── Per-token intraday candle files (smooth detail-page price chart) ──────
    PRICE_CANDLES_DIR.mkdir(parents=True, exist_ok=True)
    candle_writes = 0
    for s in screenable:
        cd = recs[s].get("_candles")
        if not cd or not (cd.get("m5") or cd.get("h1")):
            continue
        try:
            (PRICE_CANDLES_DIR / f"{s}.json").write_text(
                json.dumps({"updated": _now(), "m5": cd["m5"], "h1": cd["h1"]},
                           separators=(",", ":")), encoding="utf-8")
            candle_writes += 1
        except Exception as e:
            print(f"  candles {s}: write failed ({e})")

    # ── Strip internal fields from output ────────────────────────────────────
    for rec in recs.values():
        rec.pop("mark_price", None)
        rec.pop("_candles", None)

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
          f"holders={holder_fetches}  candles={candle_writes}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
