"""Real-time Telegram alerting daemon for Buy v1/v2/v4/probe fires.

RUNS ON THE ALWAYS-ON BOX, ALONGSIDE the existing hourly `fetch_screener.py`
cron (this does NOT replace it). The hourly cron only checks once per hour
(cron fires :17 past, plus ~1-2min compute), so a v1/v4/v2/probe fire can sit
unalerted for up to ~20-70 minutes. This daemon keeps two Binance futures
websocket streams open — kline_1m (to build the CURRENTLY FORMING hourly
candle) and markPrice (live mark price + live estimated funding rate) — and
re-evaluates lib.signals.evaluate() every EVAL_INTERVAL_S so a fire alerts
within seconds of the condition being true, not up to an hour late.

THE HOURLY CRON STAYS THE SOURCE OF TRUTH. This daemon is a faster notifier
only:
  - historical OI/klines/funding come from the SAME REST endpoints
    fetch_screener.py uses (re-bootstrapped every BOOTSTRAP_REFRESH_S so any
    websocket drift self-heals); only the CURRENT, not-yet-closed hour is
    filled in live from the websockets (current OI via a cheap 90s self-poll,
    current candle from kline_1m, current price/funding from markPrice).
  - a newly-detected fire is appended to cache/screener/daemon/fires_live.jsonl
    (source:"daemon", NOT git-tracked — a local hand-off file on the box).
    fetch_screener.py's hourly _log_fires() folds those lines into the real
    fires_log.json ledger so grading/tuning always sees ONE unified log
    regardless of which loop noticed the fire first.
  - dedup uses the SAME key (sym, strat) + the SAME FIRE_REFRACTORY_H as the
    hourly cron, seeded from fires_log.json (+ any not-yet-merged
    fires_live.jsonl) at startup, so a restart never double-fires and the
    hourly cron's independent re-derivation of the same fire is a no-op.
  - the Telegram message itself reuses fetch_screener._alert_buy() verbatim
    (same format, same ALERT_MIN_PUMP gate) so a "daemon" alert looks
    identical to an "hourly" one.

DRY_RUN=1 env: still computes + writes fires_live.jsonl + heartbeat.json, but
suppresses the Telegram send (prints what it WOULD have sent instead) — for
validating against the hourly ledger with tools/compare_daemon_fires.py
before flipping alerts on for real.

Needs `pip3 install websockets` (BOX-ONLY dependency; absent locally is fine —
the daemon degrades to REST-only, i.e. no worse than the hourly cron, it just
never picks up the live edge). See deploy/screener_daemon.service +
docs/SCREENER_DAEMON_SETUP.md for the systemd install.

    DRY_RUN=1 python3 fetch/screener_daemon.py     # validate before going live
    python3 fetch/screener_daemon.py                # live alerts
"""
from __future__ import annotations

import asyncio
import json
import os
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))

from fetch.fetch_perp_markets import _get, _f, _binance_intervals
from fetch import fetch_screener as fs
from lib.signals import evaluate

try:
    import websockets
except ImportError:
    websockets = None

HERE = Path(__file__).resolve().parents[1]        # perps_correlation/
CACHE = HERE.parent / "cache"
DAEMON_DIR = CACHE / "screener" / "daemon"
FIRES_LIVE = DAEMON_DIR / "fires_live.jsonl"       # local hand-off, NOT git-tracked
HEARTBEAT = DAEMON_DIR / "heartbeat.json"          # git-tracked hourly (screener_cron.sh)

DRY_RUN = bool(os.environ.get("DRY_RUN"))
EVAL_INTERVAL_S = 20            # how often evaluate() re-runs on live-updated buffers
OI_POLL_S = 90                  # current-OI self-poll sweep (Binance has no OI websocket)
BOOTSTRAP_REFRESH_S = 30 * 60   # full REST re-bootstrap (self-heals websocket drift)
DEDUP_REFRESH_TICKS = 15        # ~5min at EVAL_INTERVAL_S=20s: reload fires_log.json dedup
WS_BASE = "wss://fstream.binance.com/stream"

_TICKER_TO_STATE: dict[str, "_Sym"] = {}
_WS_STATUS = {"kline": False, "mark": False}


class _Sym:
    """Per-token live buffer. `ticker` is the Binance perp symbol (no USDT
    suffix) — may differ from the watchlist key via `perp_sym` overrides."""
    __slots__ = ("ticker", "oi_hist", "kl_hist", "funding_hist",
                 "funding_interval_h", "cur_candle", "cur_oi",
                 "mark_price", "mark_funding")

    def __init__(self, ticker: str):
        self.ticker = ticker
        self.oi_hist: list[tuple] = []
        self.kl_hist: list[tuple] = []
        self.funding_hist: list[tuple] = []
        self.funding_interval_h = 8.0
        self.cur_candle: dict | None = None   # {t,o,h,l,c,closed_vol,live_min_t,live_min_v}
        self.cur_oi: float | None = None
        self.mark_price: float | None = None
        self.mark_funding: float | None = None

    def kl_tuple(self):
        """The forming hour as a (t,o,h,l,c,v) row, or None."""
        c = self.cur_candle
        if not c:
            return None
        vol = (c.get("closed_vol") or 0.0) + (c.get("live_min_v") or 0.0)
        return (c["t"], c["o"], c["h"], c["l"], c["c"], vol)


# ── bootstrap (REST — the same fetchers the hourly cron uses) ───────────────

def _bootstrap_one(ticker: str):
    return (fs._oi_hist(ticker), fs._klines_1h(ticker), fs._funding(ticker))


def bootstrap(state: dict[str, _Sym], workers: int = 6) -> None:
    try:
        intervals = _binance_intervals()
    except Exception:
        intervals = {}
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(_bootstrap_one, st.ticker): sym for sym, st in state.items()}
        for fut, sym in futs.items():
            st = state[sym]
            try:
                oi, kl, fund = fut.result()
            except Exception as e:
                print(f"[daemon] bootstrap {sym} failed: {e}")
                continue
            st.oi_hist, st.kl_hist, st.funding_hist = oi, kl, fund
            st.funding_interval_h = float(intervals.get(f"{st.ticker}USDT", 8) or 8)
    print(f"[daemon] bootstrapped {len(state)} symbols")


async def bootstrap_refresh_loop(state, executor):
    loop = asyncio.get_event_loop()
    while True:
        await asyncio.sleep(BOOTSTRAP_REFRESH_S)
        try:
            await loop.run_in_executor(executor, bootstrap, state)
            print("[daemon] bootstrap refresh complete")
        except Exception as e:
            print(f"[daemon] bootstrap refresh failed: {e}")


# ── current-OI self-poll (Binance has no OI websocket) ──────────────────────

def _poll_oi_one(ticker: str):
    d = _get(f"{fs.FAPI}/fapi/v1/openInterest?symbol={ticker}USDT")
    return _f(d.get("openInterest")) if d else None


def _poll_oi_all(state: dict[str, _Sym]):
    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = {ex.submit(_poll_oi_one, st.ticker): sym for sym, st in state.items()}
        for fut, sym in futs.items():
            st = state[sym]
            try:
                coins = fut.result()
            except Exception:
                coins = None
            if coins is None:
                continue
            mark = (st.mark_price or (st.cur_candle or {}).get("c")
                    or (st.kl_hist[-1][4] if st.kl_hist else None))
            if mark:
                st.cur_oi = coins * mark


async def oi_poll_loop(state, executor):
    loop = asyncio.get_event_loop()
    while True:
        t0 = time.time()
        try:
            await loop.run_in_executor(executor, _poll_oi_all, state)
        except Exception as e:
            print(f"[daemon] oi poll failed: {e}")
        await asyncio.sleep(max(1.0, OI_POLL_S - (time.time() - t0)))


# ── websocket listeners ──────────────────────────────────────────────────────

def _ticker_from_stream_symbol(s: str) -> str:
    s = (s or "").upper()
    return s[:-4] if s.endswith("USDT") else s


def _handle_kline_msg(raw: str) -> None:
    try:
        msg = json.loads(raw)
        d = msg.get("data") or {}
        k = d.get("k") or {}
        st = _TICKER_TO_STATE.get(_ticker_from_stream_symbol(d.get("s")))
        if not st:
            return
        t = int(k.get("t", 0)) // 1000
        hour_t = (t // 3600) * 3600
        o, h, l, c = _f(k.get("o")), _f(k.get("h")), _f(k.get("l")), _f(k.get("c"))
        v = _f(k.get("v")) or 0.0
        if c is None:
            return
        cur = st.cur_candle
        if not cur or cur["t"] != hour_t:
            cur = {"t": hour_t, "o": o if o is not None else c,
                   "h": h if h is not None else c, "l": l if l is not None else c,
                   "c": c, "closed_vol": 0.0, "live_min_t": t, "live_min_v": v}
            st.cur_candle = cur
        else:
            cur["h"] = max(cur["h"], h if h is not None else c)
            cur["l"] = min(cur["l"], l if l is not None else c)
            cur["c"] = c
            if cur.get("live_min_t") != t:
                # a new minute started — the previous forming minute is done
                cur["closed_vol"] = (cur.get("closed_vol") or 0.0) + (cur.get("live_min_v") or 0.0)
                cur["live_min_t"] = t
            cur["live_min_v"] = v   # v is cumulative for the current minute, not a delta
    except Exception:
        pass


def _handle_mark_msg(raw: str) -> None:
    try:
        msg = json.loads(raw)
        d = msg.get("data") or {}
        st = _TICKER_TO_STATE.get(_ticker_from_stream_symbol(d.get("s")))
        if not st:
            return
        p, r = _f(d.get("p")), _f(d.get("r"))
        if p is not None:
            st.mark_price = p
        if r is not None:
            st.mark_funding = r
    except Exception:
        pass


async def _ws_loop(stream_suffix: str, handler, status_key: str):
    if websockets is None:
        print(f"[daemon] websockets not installed — {status_key} stream disabled "
              f"(daemon falls back to REST-only cadence)")
        return
    streams = "/".join(f"{st.ticker.lower()}usdt@{stream_suffix}"
                        for st in _TICKER_TO_STATE.values())
    url = f"{WS_BASE}?streams={streams}"
    backoff = 2
    while True:
        try:
            async with websockets.connect(url, ping_interval=180, ping_timeout=60,
                                          max_size=2 ** 20) as ws:
                print(f"[daemon] {status_key} stream connected")
                _WS_STATUS[status_key] = True
                backoff = 2
                async for raw in ws:
                    handler(raw)
        except Exception as e:
            _WS_STATUS[status_key] = False
            print(f"[daemon] {status_key} stream error: {e} — reconnecting in {backoff}s")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)


def kline_listener():
    return _ws_loop("kline_1m", _handle_kline_msg, "kline")


def mark_listener():
    return _ws_loop("markPrice@1s", _handle_mark_msg, "mark")


# ── dedup state (shared with the hourly ledger) ──────────────────────────────

def _read_fires_live() -> list[dict]:
    if not FIRES_LIVE.exists():
        return []
    out = []
    try:
        for line in FIRES_LIVE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                out.append(json.loads(line))
    except Exception:
        pass
    return out


def _load_dedup_state() -> dict[tuple, int]:
    last_fire: dict[tuple, int] = {}
    for source in (fs._load_json(fs.FIRES_LOG, []), _read_fires_live()):
        if not isinstance(source, list):
            continue
        for e in source:
            k = (e.get("sym"), e.get("strat"))
            t = e.get("t") or 0
            if t > last_fire.get(k, 0):
                last_fire[k] = t
    print(f"[daemon] loaded dedup state: {len(last_fire)} (sym,strat) pairs")
    return last_fire


def _refresh_dedup_state(last_fire: dict[tuple, int]) -> None:
    log = fs._load_json(fs.FIRES_LOG, [])
    if not isinstance(log, list):
        return
    for e in log:
        k = (e.get("sym"), e.get("strat"))
        t = e.get("t") or 0
        if t > last_fire.get(k, 0):
            last_fire[k] = t


# ── evaluate() tick ───────────────────────────────────────────────────────────

def _append_fires_live(entry: dict) -> None:
    DAEMON_DIR.mkdir(parents=True, exist_ok=True)
    with open(FIRES_LIVE, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, separators=(",", ":")) + "\n")


def eval_tick(state: dict[str, _Sym], last_fire: dict[tuple, int]) -> bool:
    """One pass over every symbol: assemble live-updated buffers, re-run
    lib.signals.evaluate(), and alert any brand-new ALERT_STRATS fire on the
    CURRENT (still-forming) hour. Returns True iff anything fired this tick."""
    now_hour = (int(time.time()) // 3600) * 3600
    candidates = []   # (sym, strat, d, st)
    for sym, st in state.items():
        oi_eff = list(st.oi_hist)
        if st.cur_oi is not None and (not oi_eff or oi_eff[-1][0] < now_hour):
            oi_eff.append((now_hour, st.cur_oi))
        kl_eff = list(st.kl_hist)
        live_row = st.kl_tuple()
        if live_row and live_row[0] == now_hour and (not kl_eff or kl_eff[-1][0] < now_hour):
            kl_eff.append(live_row)
        if not oi_eff or not kl_eff:
            continue
        cur_funding = st.mark_funding if st.mark_funding is not None else (
            st.funding_hist[-1][1] if st.funding_hist else None)
        try:
            sig = evaluate(oi_eff, kl_eff, funding=st.funding_hist,
                           current_funding=cur_funding,
                           funding_interval_h=st.funding_interval_h or 8.0,
                           lookback_h=6)
        except Exception:
            continue
        for strat in fs.ALERT_STRATS:
            d = sig.get(strat) or {}
            if not d.get("fired") or d.get("t") != now_hour:
                continue
            if now_hour - last_fire.get((sym, strat), 0) < fs.FIRE_REFRACTORY_H * 3600:
                continue
            candidates.append((sym, strat, d, st))

    if not candidates:
        return False

    breadth: dict[str, int] = {}
    for sym, strat, d, st in candidates:
        breadth[strat] = breadth.get(strat, 0) + 1

    for sym, strat, d, st in candidates:
        last_fire[(sym, strat)] = now_hour
        px = st.mark_price or (st.cur_candle or {}).get("c")
        pump = None
        try:
            if fs._pump_score and fs._PUMP_MODEL:
                pump = fs._pump_score(list(st.oi_hist), list(st.kl_hist),
                                      st.funding_hist, fs._PUMP_MODEL)
        except Exception:
            pump = None
        entry = {
            "sym": sym, "strat": strat, "t": now_hour, "logged_utc": fs._now(),
            "entry_price": px, "stop": d.get("stop"), "position": d.get("position"),
            "tp1": (px * 1.25 if px else None), "tp2": (px * 1.50 if px else None),
            "time_stop_h": 72,
            "oi_bn": st.cur_oi, "oi_combined": st.cur_oi,
            "funding": st.mark_funding if st.mark_funding is not None else (
                st.funding_hist[-1][1] if st.funding_hist else None),
            "fdv": None, "pump_score": pump,
            "alerted": False, "outcome": None, "source": "daemon",
        }
        wide = breadth.get(strat, 0) > fs.BREADTH_MAX
        if wide:
            entry["market_wide"] = True
            print(f"[daemon] skip {sym} {strat} — market-wide "
                  f"({breadth[strat]} coins fired this tick)")
        elif DRY_RUN:
            print(f"[daemon] DRY_RUN would alert {sym} {strat} @ {px}")
        else:
            r_like = {"market": {}, "oi_fdv_pct": None}
            if fs._alert_buy(entry, r_like):
                entry["alerted"] = True
                entry["alerted_utc"] = fs._now()
                print(f"[daemon] ALERTED {sym} {strat} @ {px}")
        _append_fires_live(entry)
    return True


def _write_heartbeat(tick: int, n_syms: int, last_fire_ts: int | None) -> None:
    DAEMON_DIR.mkdir(parents=True, exist_ok=True)
    HEARTBEAT.write_text(json.dumps({
        "ts": fs._now(), "tick": tick, "symbols": n_syms,
        "ws_kline_connected": _WS_STATUS["kline"], "ws_mark_connected": _WS_STATUS["mark"],
        "last_fire_ts": last_fire_ts, "dry_run": DRY_RUN,
    }, separators=(",", ":")), encoding="utf-8")


async def eval_loop(state, last_fire, executor):
    loop = asyncio.get_event_loop()
    tick = 0
    last_fire_ts = None
    while True:
        tick += 1
        try:
            fired = await loop.run_in_executor(executor, eval_tick, state, last_fire)
            if fired:
                last_fire_ts = fs._now()
        except Exception:
            print(f"[daemon] eval tick failed:\n{traceback.format_exc()}")
        if tick % DEDUP_REFRESH_TICKS == 0:
            try:
                _refresh_dedup_state(last_fire)
            except Exception:
                pass
        try:
            _write_heartbeat(tick, len(state), last_fire_ts)
        except Exception:
            pass
        await asyncio.sleep(EVAL_INTERVAL_S)


# ── main ─────────────────────────────────────────────────────────────────────

async def main_async() -> int:
    wl = fs._load_json(fs.WATCHLIST, {})
    if not wl:
        print(f"{fs.WATCHLIST} missing/empty -- run tools/seed_manip_watchlist.py first")
        return 1
    screenable = sorted(s for s, r in wl.items() if r.get("has_perp"))
    state: dict[str, _Sym] = {sym: _Sym(wl[sym].get("perp_sym") or sym) for sym in screenable}
    _TICKER_TO_STATE.clear()
    _TICKER_TO_STATE.update({st.ticker.upper(): st for st in state.values()})

    executor = ThreadPoolExecutor(max_workers=8)
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(executor, bootstrap, state)

    last_fire = _load_dedup_state()

    print(f"[daemon] running — {len(state)} symbols, DRY_RUN={DRY_RUN}")
    await asyncio.gather(
        kline_listener(),
        mark_listener(),
        oi_poll_loop(state, executor),
        bootstrap_refresh_loop(state, executor),
        eval_loop(state, last_fire, executor),
    )
    return 0


def main() -> int:
    try:
        return asyncio.run(main_async())
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
