"""Render a price chart of Nexus (NEX) annotated with each exchange's listing event.

Fetches 5-minute OHLC for NEXUSDT across the launch window
(2026-05-20 12:00 UTC -> 2026-05-21 18:00 UTC), tries Binance spot first then
Binance perp as fallback, and writes:
  cache/nex_klines_5m.json          raw kline rows from whichever source worked
  charts/nex_listing_reaction.png   annotated chart

Listing event timestamps live in listing_events.json so they can be refined
without editing the script.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import requests
import matplotlib.pyplot as plt
import matplotlib.dates as mdates

ROOT = Path(__file__).parent
CACHE = ROOT.parent.parent / "cache"
CACHE.mkdir(exist_ok=True)
EVENTS_FILE = ROOT / "listing_events.json"
OUT_PNG = ROOT.parent / "charts" / "nex_listing_reaction.png"

WINDOW_START = datetime(2026, 5, 20, 12, 0, tzinfo=timezone.utc)
WINDOW_END = datetime(2026, 5, 21, 18, 0, tzinfo=timezone.utc)
SYMBOL = "NEXUSDT"

# Binance Alpha routes through this Pancakeswap Infinity pool on BSC
# (NEX token contract 0x365de036a1f7dccb621530d517133521debb2013).
GECKO_NETWORK = "bsc"
GECKO_POOL = "0xae74941d0ff92e1e6c26a11fa0762ef29b87786e60daf62be00477288ec41abd"

UA = {"User-Agent": "Mozilla/5.0 verifysheet/nex-listing"}


def to_ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


_SUBSCRIPT = str.maketrans("0123456789", "₀₁₂₃₄₅₆₇₈₉")


def fmt_subscript_price(p: float, sig_digits: int = 3) -> str:
    """Render small USD prices in the $0.0₅556 convention used by CoinGecko etc.,
    where the subscript counts the leading zeros after the decimal point."""
    if p <= 0:
        return "$0"
    if p >= 1:
        return f"${p:,.4f}"
    s = f"{p:.20f}".split(".")[1]
    stripped = s.lstrip("0")
    n_zeros = len(s) - len(stripped)
    if n_zeros < 4:
        # not enough zeros to benefit from subscript notation
        return f"${p:.{n_zeros + sig_digits}f}"
    digits = (stripped + "000")[:sig_digits]
    return f"$0.0{str(n_zeros).translate(_SUBSCRIPT)}{digits}"


def fetch_geckoterminal_pool() -> list:
    """GeckoTerminal pool OHLCV for the Pancakeswap Infinity NEX/USDT pool.
    This is Binance Alpha's actual on-chain routing venue, so prices are
    clean from 14:00 UTC May 20 (no exchange-side pre-open noise).

    Response: {data: {attributes: {ohlcv_list: [[unix_s, o, h, l, c, vol], ...]}}}
    The list is newest-first; we reverse and convert to [ts_ms, o, h, l, c]."""
    url = (
        f"https://api.geckoterminal.com/api/v2/networks/{GECKO_NETWORK}"
        f"/pools/{GECKO_POOL}/ohlcv/minute"
    )
    r = requests.get(
        url,
        params={"aggregate": 5, "limit": 1000},
        headers=UA, timeout=20,
    )
    r.raise_for_status()
    items = (
        r.json()
        .get("data", {})
        .get("attributes", {})
        .get("ohlcv_list")
        or []
    )
    out = []
    for row in reversed(items):
        ts_s, o, h, l, c, _vol = row
        ts_ms = int(ts_s) * 1000
        if ts_ms < to_ms(WINDOW_START) or ts_ms > to_ms(WINDOW_END):
            continue
        out.append([ts_ms, float(o), float(h), float(l), float(c)])
    return out


def fetch_bitget() -> list:
    """Bitget v2 spot candles. Returns rows in form
    [ts_ms_str, open, high, low, close, baseVol, quoteVol, quoteVol].
    Normalize to [ts_ms_int, open_f, high_f, low_f, close_f]."""
    r = requests.get(
        "https://api.bitget.com/api/v2/spot/market/candles",
        params={
            "symbol": SYMBOL,
            "granularity": "5min",
            "startTime": to_ms(WINDOW_START),
            "endTime": to_ms(WINDOW_END),
            "limit": 1000,
        },
        headers=UA, timeout=20,
    )
    r.raise_for_status()
    data = r.json().get("data") or []
    out = []
    for row in data:
        out.append([
            int(row[0]),
            float(row[1]),
            float(row[2]),
            float(row[3]),
            float(row[4]),
        ])
    return out


def fetch_kucoin() -> list:
    """KuCoin spot klines as fallback. Returns rows [ts_s_str, o, c, h, l, vol, turnover].
    Normalize to [ts_ms_int, o, h, l, c]."""
    r = requests.get(
        "https://api.kucoin.com/api/v1/market/candles",
        params={
            "symbol": "NEX-USDT",
            "type": "5min",
            "startAt": int(WINDOW_START.timestamp()),
            "endAt": int(WINDOW_END.timestamp()),
        },
        headers=UA, timeout=20,
    )
    r.raise_for_status()
    data = r.json().get("data") or []
    out = []
    for row in data:
        ts_ms = int(row[0]) * 1000
        o, c, h, l = float(row[1]), float(row[2]), float(row[3]), float(row[4])
        out.append([ts_ms, o, h, l, c])
    out.sort(key=lambda r: r[0])
    return out


def _cache_path(label: str) -> Path:
    slug = label.lower().replace(" ", "_")
    return CACHE / f"nex_klines_5m_{slug}.json"


def fetch_klines() -> tuple[list, str]:
    sources = [
        (fetch_geckoterminal_pool, "Binance Alpha (on-chain, BSC)"),
        (fetch_bitget, "Bitget Spot"),
        (fetch_kucoin, "KuCoin Spot"),
    ]

    # honor a cache for the highest-priority source if present
    for _, label in sources:
        cp = _cache_path(label)
        if cp.exists():
            cached = json.loads(cp.read_text(encoding="utf-8"))
            if cached.get("rows"):
                print(f"[{label}] using cached {len(cached['rows'])} candles")
                return cached["rows"], label
            break

    for fetcher, label in sources:
        try:
            rows = fetcher()
        except Exception as e:
            print(f"[{label}] error: {e}")
            rows = []
        if rows:
            _cache_path(label).write_text(
                json.dumps({"source": label, "rows": rows}, ensure_ascii=False),
                encoding="utf-8",
            )
            print(f"[{label}] fetched {len(rows)} candles")
            return rows, label
    raise SystemExit("No klines available from GeckoTerminal, Bitget, or KuCoin.")


def render(rows: list, source_label: str) -> None:
    events = json.loads(EVENTS_FILE.read_text(encoding="utf-8"))["events"]

    times = [datetime.fromtimestamp(r[0] / 1000, tz=timezone.utc) for r in rows]
    closes = [float(r[4]) for r in rows]

    fig, ax = plt.subplots(figsize=(16, 9), dpi=150)
    ax.plot(times, closes, color="#1f4e79", linewidth=1.6,
            label=f"NEX close — {source_label}, 5m")

    palette = ["#d62728", "#2ca02c", "#9467bd", "#ff7f0e", "#17becf", "#8c564b"]
    price_max = max(closes)
    price_min = min(closes)
    span = price_max - price_min

    for i, ev in enumerate(events):
        t = datetime.fromisoformat(ev["iso_time_utc"].replace("Z", "+00:00"))
        if t > times[-1]:
            continue
        color = palette[i % len(palette)]
        # nearest close to event time for arrow target; if event predates
        # the first candle (e.g. Binance Alpha listed before any other venue
        # started quoting), snap the arrow to the first available price.
        nearest = min(range(len(times)), key=lambda k: abs(times[k] - t))
        target_price = closes[nearest]

        ax.axvline(t, color=color, linestyle="--", linewidth=1.0, alpha=0.6)

        # stagger label heights so they don't collide
        label_y = price_min + span * (0.85 - 0.10 * (i % 4))
        ax.annotate(
            f"{ev['exchange']}\n{t.strftime('%H:%M UTC')}",
            xy=(t, target_price),
            xytext=(t, label_y),
            ha="center",
            fontsize=9,
            color=color,
            arrowprops=dict(arrowstyle="->", color=color, lw=1.0),
            bbox=dict(boxstyle="round,pad=0.3", fc="white", ec=color, lw=0.8),
        )

    ax.set_title("Nexus (NEX) — Price Reaction to Exchange Listings, 20–21 May 2026",
                 fontsize=14, pad=14)
    ax.set_xlabel("Time (UTC)")
    ax.set_ylabel("Price (USD)")
    ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda v, _p: fmt_subscript_price(v)))
    # ensure 14:00 UTC (Binance Alpha) is on-screen even though first candle is later
    left_bound = min(times[0], WINDOW_START.replace(hour=13, minute=50))
    ax.set_xlim(left=left_bound, right=times[-1])
    ax.xaxis.set_major_locator(mdates.HourLocator(interval=2))
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%m-%d %H:%M"))
    ax.grid(True, alpha=0.25)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    ax.legend(loc="lower right", frameon=False)
    fig.autofmt_xdate()

    OUT_PNG.parent.mkdir(exist_ok=True)
    fig.tight_layout()
    fig.savefig(OUT_PNG)
    print(f"wrote {OUT_PNG}")


def main() -> None:
    rows, source = fetch_klines()
    render(rows, source)


if __name__ == "__main__":
    main()
