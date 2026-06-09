"""Adaptive-timeframe listing chart for funnel tokens. Span can be days (new
launches) to >1 year (re-featured majors), so the candle resolution is chosen
by span: <=10d -> 5m, <=90d -> 1h, else 1d. Single panel: price line + a marked
vertical line per venue listing. PNG -> charts/funnel/<token>.png.
"""
from __future__ import annotations
import json, sys, time
from datetime import datetime, timezone, timedelta
from pathlib import Path
import requests
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates

sys.path.insert(0, str(Path(__file__).parent.parent))
from lib.venues import venue_color
from lib.listing_chart import fmt_usd_compact, fmt_subscript_price

UA = {"User-Agent": "Mozilla/5.0 verifysheet/funnel-chart"}
HERE = Path(__file__).parent
CACHE = HERE / "klines"
CACHE.mkdir(exist_ok=True)
OUT = HERE.parent / "Listinglabs" / "funnel" / "report" / "charts"
OUT.mkdir(parents=True, exist_ok=True)

EVENT_VENUES = [("alpha_date", "Binance Alpha"), ("perp_date", "Binance Perp"),
                ("coinbase_date", "Coinbase Spot"), ("upbit_date", "Upbit"),
                ("bithumb_date", "Bithumb")]


def to_ms(dt): return int(dt.timestamp() * 1000)
def parse_d(s): return datetime.fromisoformat(s).replace(tzinfo=timezone.utc) if s else None


def pick_interval(span_days: float):
    # Binance fapi klines: 1h limit 1500 (~62d), 1d covers years. Funnel charts
    # are multi-venue timelines, so daily is the natural resolution; 1h only for
    # short spans where daily would be too coarse.
    return ("1h", 3_600_000) if span_days <= 21 else ("1d", 86_400_000)


def scale_factor(perp_symbol: str, base: str) -> float:
    """1000X / 10000X perp contracts quote price x1000 etc. Divide back to the
    real token price so the y-axis matches the actual token."""
    b = (perp_symbol or "").replace("USDT", "")
    for pre, f in (("1000000", 1e6), ("10000", 1e4), ("1000", 1e3)):
        if b == pre + base:
            return f
    return 1.0


def fetch_binance_perp(perp_symbol, base, start, end, interval):
    """Full-history close series from Binance USDT-M perp klines."""
    url = "https://fapi.binance.com/fapi/v1/klines"
    fac = scale_factor(perp_symbol, base)
    rows, cur, end_ms = [], to_ms(start), to_ms(end)
    for _ in range(40):
        r = requests.get(url, params={"symbol": perp_symbol, "interval": interval,
                                      "startTime": cur, "endTime": end_ms, "limit": 1500},
                         headers=UA, timeout=20)
        if r.status_code != 200:
            break
        d = r.json()
        if not d:
            break
        for k in d:
            rows.append([int(k[0]), float(k[1]) / fac, float(k[2]) / fac,
                         float(k[3]) / fac, float(k[4]) / fac])
        if len(d) < 1500:
            break
        cur = int(d[-1][0]) + 1
        time.sleep(0.1)
    return rows


def events_of(m):
    out = []
    for key, label in EVENT_VENUES:
        dt = parse_d(m.get(key))
        if dt:
            out.append((label, dt))
    return out


def render(m, rows):
    token = m["symbol"]
    times = [datetime.fromtimestamp(r[0] / 1000, tz=timezone.utc) for r in rows]
    closes = [r[4] for r in rows]
    evs = events_of(m)
    fig, ax = plt.subplots(figsize=(16, 8), dpi=140)
    ax.plot(times, closes, color="#1f4e79", linewidth=1.3, label=f"{token} — Binance perp close")
    seen_y = {}
    for label, t in evs:
        color = venue_color(label)
        ax.axvline(t, color=color, linestyle="--", linewidth=1.1, alpha=0.7)
        # stagger labels by rounding x to avoid exact overlap
        rung = seen_y.get(t.strftime("%Y-%m"), 0)
        seen_y[t.strftime("%Y-%m")] = rung + 1
        ax.annotate(f"{label}\n{t.strftime('%Y-%m-%d')}",
                    xy=(t, 1.0), xycoords=("data", "axes fraction"),
                    xytext=(0, -12 - 34 * rung), textcoords="offset points",
                    ha="center", va="top", fontsize=8, color=color,
                    bbox=dict(boxstyle="round,pad=0.3", fc="white", ec=color, lw=0.8))
    ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda v, _p: fmt_subscript_price(v)))
    ax.grid(True, alpha=0.25)
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m-%d"))
    name = m.get("name") or token
    ax.set_title(f"{name} ({token}) — Listings across venues", fontsize=14, pad=30)
    fdv = m.get("fdv")
    if fdv:
        ax.text(0.995, 1.01, f"FDV {fmt_usd_compact(fdv)}", transform=ax.transAxes,
                ha="right", va="bottom", fontsize=12, fontweight="bold", color="#1f4e79",
                bbox=dict(boxstyle="round,pad=0.35", fc="#eaf2fb", ec="#1f4e79", lw=1.1))
    ax.set_xlabel("Date (UTC)"); ax.set_ylabel("Price (USD)")
    ax.legend(loc="upper left", frameon=False)
    fig.autofmt_xdate(); fig.tight_layout()
    p = OUT / f"{token.lower()}.png"
    fig.savefig(p); plt.close(fig)
    return p


def build_one(m) -> str | None:
    if not m.get("perp_symbol"):
        return None
    evs = events_of(m)
    if not evs:
        return None
    ev_times = [t for _, t in evs]
    start = min(ev_times) - timedelta(days=2)
    end = min(max(ev_times) + timedelta(days=14), datetime.now(timezone.utc))
    span = (end - start).days
    interval, _ms = pick_interval(span)
    cache = CACHE / f"{m['symbol'].lower()}_{interval}.json"
    if cache.exists():
        rows = json.loads(cache.read_text(encoding="utf-8"))
    else:
        rows = fetch_binance_perp(m["perp_symbol"], m["symbol"], start, end, interval)
        cache.write_text(json.dumps(rows), encoding="utf-8")
    if not rows:
        return None
    return str(render(m, rows))


def main():
    master = json.loads((HERE / "funnel_master.json").read_text(encoding="utf-8"))
    only = {a.upper() for a in sys.argv[1:]} or None
    done = miss = 0
    for m in master:
        if only and m["symbol"] not in only:
            continue
        try:
            p = build_one(m)
        except Exception as e:
            p = None
            print(f"  {m['symbol']}: ERROR {e}")
        if p:
            done += 1
            print(f"  {m['symbol']:10} -> {Path(p).name}")
        else:
            miss += 1
            print(f"  {m['symbol']:10} -> (no chart: pool/rows missing)")
    print(f"\ncharts: {done} ok, {miss} missing")


if __name__ == "__main__":
    main()
