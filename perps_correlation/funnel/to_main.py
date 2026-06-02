"""Render the 74 funnel tokens in the SAME format as the curated 4 (NEX/CTR/
SLX/QAIT): write a listings/<token>.json + a cache/<token>_klines_5m_alpha.json
(fed from Binance USDT-M perp 5m klines, since on-chain history is capped), then
the standard build_listing_report.py turns them into identical detail pages.

Window: Alpha listing -6h .. min(now, Alpha+30d) — a uniform reaction window.
"""
from __future__ import annotations
import json, sys, time
from datetime import datetime, timezone, timedelta
from pathlib import Path
import requests

sys.path.insert(0, str(Path(__file__).parent.parent))
from listing_chart import render_twopanel, parse_iso  # noqa
from funnel_chart import scale_factor  # noqa

UA = {"User-Agent": "Mozilla/5.0 verifysheet/to-main"}
ROOT = Path(__file__).parent.parent
CACHE = ROOT.parent / "cache"
LISTINGS = ROOT / "listings"
CHARTS = ROOT / "charts"
for d in (CACHE, LISTINGS, CHARTS):
    d.mkdir(exist_ok=True)

NET2CHAIN = {"bsc": "bsc", "eth": "ethereum", "base": "base", "solana": "solana",
             "arbitrum": "arbitrum", "sui-network": "sui", "linea": "linea", "tron": "tron"}
# Real perp onboard timestamps (intraday) — master.json only keeps the date, but
# the perp's first candle is at the actual onboard time, so the marker must use
# the full timestamp or it falls before the first candle and gets clipped.
_DATED = json.loads((Path(__file__).parent / "funnel_dated.json").read_text(encoding="utf-8"))
PERP_ISO = {r["symbol"]: r.get("perp_onboard_iso") for r in _DATED}
EVENT_KEYS = [("alpha_date", "Binance Alpha"), ("perp_date", "Binance Perp"),
              ("coinbase_date", "Coinbase Spot"), ("upbit_date", "Upbit"), ("bithumb_date", "Bithumb")]


def to_ms(dt): return int(dt.timestamp() * 1000)


def pick_interval(span_days: float) -> str:
    # Keep 5m for spans up to ~100d so new launches (which now extend to launch+90d
    # for the +90d checkpoint) stay crisp like the curated 4; coarsen only for the
    # genuinely long, multi-venue-over-months spans.
    if span_days <= 100: return "5m"
    if span_days <= 400: return "1h"
    return "1d"


def fetch_klines(perp_symbol, base, start, end, interval):
    fac = scale_factor(perp_symbol, base)
    rows, cur, end_ms = [], to_ms(start), to_ms(end)
    for _ in range(80):
        r = requests.get("https://fapi.binance.com/fapi/v1/klines",
                         params={"symbol": perp_symbol, "interval": interval, "startTime": cur,
                                 "endTime": end_ms, "limit": 1500}, headers=UA, timeout=20)
        if r.status_code != 200:
            break
        d = r.json()
        if not d:
            break
        for k in d:
            rows.append([int(k[0]), float(k[1]) / fac, float(k[2]) / fac, float(k[3]) / fac, float(k[4]) / fac])
        if len(d) < 1500:
            break
        cur = int(d[-1][0]) + 1
        time.sleep(0.08)
    return rows


def fetch_coinbase(base, start, end, gran):
    """Coinbase Exchange candles -> [ts_ms, o, h, l, c]. Coinbase row order is
    [time, low, high, open, close, vol]. Max 300 candles/request -> paginate."""
    win = timedelta(seconds=gran * 300)
    for quote in ("USD", "USDC"):
        pid = f"{base}-{quote}"
        by = {}
        cur = start
        while cur < end:
            ce = min(cur + win, end)
            try:
                r = requests.get(f"https://api.exchange.coinbase.com/products/{pid}/candles",
                                 params={"granularity": gran, "start": cur.isoformat(), "end": ce.isoformat()},
                                 headers=UA, timeout=20)
                if r.status_code == 200:
                    for row in (r.json() or []):
                        ts = int(row[0]) * 1000
                        by[ts] = [ts, float(row[3]), float(row[2]), float(row[1]), float(row[4])]
            except Exception:
                pass
            cur = ce
            time.sleep(0.1)
        if by:
            return [by[k] for k in sorted(by)]
    return []


def events_for(m, win_start, win_end):
    out = []
    for key, label in EVENT_KEYS:
        v = m.get(key)
        if not v:
            continue
        # alpha/perp have full timestamps in the iso fields; CB/Korea are dates
        if key == "alpha_date":
            iso = m["alpha_iso"] if "alpha_iso" in m else v + "T00:00:00Z"
        elif key == "perp_date":
            iso = PERP_ISO.get(m["symbol"]) or m.get("perp_onboard_iso") or (v + "T00:00:00Z")
        else:
            iso = v + "T00:00:00Z"
        out.append({"exchange": label, "iso_time_utc": iso})
    return out


def build(m, master_raw):
    base = m["symbol"]
    if not m.get("perp_symbol"):
        return False
    alpha = parse_iso(m["alpha_iso"]) if m.get("alpha_iso") else parse_iso(m["alpha_date"] + "T00:00:00Z")
    perp = parse_iso(PERP_ISO.get(m["symbol"]) or (m["perp_date"] + "T00:00:00Z")) if m.get("perp_date") else alpha
    # Window spans ALL listing events so every marker sits on real price. Binance
    # perp data only exists from the perp onboard, so anchor the start at the
    # later of (earliest event, perp onboard); events before that are clipped by
    # the chart but still listed in the events table.
    ev_times = [parse_iso(e["iso_time_utc"]) for e in events_for(m, None, None)]
    if not ev_times:
        return False
    min_t, max_t = min(ev_times), max(ev_times)
    anchor = max(alpha, perp)
    # Price the chart from whichever USD venue listed EARLIEST — Coinbase or the
    # Binance perp — so its history covers the first listing and every marker
    # lands on the line. (Korean venues quote KRW, so they're not price sources.)
    cb_start = parse_iso(m["coinbase_date"] + "T00:00:00Z") if m.get("coinbase_date") else None
    use_cb = cb_start is not None and cb_start <= perp
    src_start = cb_start if use_cb else perp
    start = src_start - timedelta(hours=6)
    # extend to at least launch+90d so the +90d reaction checkpoint can populate
    horizon = max(max_t, anchor + timedelta(days=90))
    end = min(datetime.now(timezone.utc), horizon + timedelta(days=2))
    span_days = (end - start).days
    cache_path = CACHE / f"{base.lower()}_klines_5m_alpha.json"
    if cache_path.exists():
        rows = json.loads(cache_path.read_text(encoding="utf-8")).get("rows") or []
    else:
        if use_cb:
            gran = 300 if span_days <= 10 else (3600 if span_days <= 120 else 86400)
            rows = fetch_coinbase(base, start, end, gran)
            src_label = f"Coinbase spot {gran}s"
            # fall back to perp if Coinbase returned nothing
            if not rows:
                interval = pick_interval(span_days)
                rows = fetch_klines(m["perp_symbol"], base, max(perp, start) - timedelta(hours=6), end, interval)
                src_label = f"Binance USDT-M perp {interval}"
        else:
            interval = pick_interval(span_days)
            rows = fetch_klines(m["perp_symbol"], base, start, end, interval)
            src_label = f"Binance USDT-M perp {interval}"
        cache_path.write_text(json.dumps({"source": src_label, "rows": rows}, ensure_ascii=False), encoding="utf-8")
    if not rows:
        return False
    cfg = {
        "token": base, "name": m.get("name") or base,
        "chain": NET2CHAIN.get(m.get("net"), m.get("net") or ""),
        "token_contract": m.get("contract"), "gecko_pool": m.get("gecko_pool"),
        "fdv_usd": m.get("fdv"), "fdv_source": "CMC/GeckoTerminal (current)",
        "category": "", "cmc_slug": m.get("cmc_slug"),
        "mcap_usd": m.get("mcap"), "circulating_supply": m.get("circ"), "total_supply": m.get("total"),
        "mcap_source": "CMC/GeckoTerminal (current, not at-listing)",
        "window_start_utc": start.isoformat().replace("+00:00", "Z"),
        "window_end_utc": end.isoformat().replace("+00:00", "Z"),
        "events": events_for(m, start, end),
        "not_listed": [], "funnel": True,
    }
    (LISTINGS / f"{base.lower()}.json").write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        render_twopanel(cfg, rows, "Binance perp 5m", CHARTS / f"{base.lower()}_listing_reaction.png")
    except Exception as e:
        print(f"   {base} png err: {e}")
    return True


def main():
    master = json.loads((Path(__file__).parent / "funnel_master.json").read_text(encoding="utf-8"))
    only = {a.upper() for a in sys.argv[1:]} or None
    ok = bad = 0
    for m in master:
        if only and m["symbol"] not in only:
            continue
        try:
            done = build(m, master)
        except Exception as e:
            done = False
            print(f"  {m['symbol']}: ERROR {e}")
        ok += done
        bad += (not done)
        print(f"  {m['symbol']:10} {'ok' if done else 'MISSING'}")
    print(f"\n{ok} built, {bad} missing")


if __name__ == "__main__":
    main()
