"""Render an annotated price chart for a Binance Alpha listing.

Driven by a JSON config in `listings/<token>.json`. Pulls 5-minute OHLCV from
the on-chain Binance Alpha pool via GeckoTerminal (true Alpha price) with
optional CEX fallback sources, then annotates each exchange listing event.

Usage:
    python listing_chart.py listings/ctr.json
"""
from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
import matplotlib.pyplot as plt
import matplotlib.dates as mdates

from venues import venue_color

HERE = Path(__file__).parent
CACHE = HERE.parent / "cache"
CACHE.mkdir(exist_ok=True)
CHARTS = HERE / "charts"
CHARTS.mkdir(exist_ok=True)

UA = {"User-Agent": "Mozilla/5.0 verifysheet/listing-chart"}

_SUBSCRIPT = str.maketrans("0123456789", "₀₁₂₃₄₅₆₇₈₉")


def fmt_usd_compact(v: float) -> str:
    if v >= 1e9: return f"${v/1e9:.2f}B"
    if v >= 1e6: return f"${v/1e6:.1f}M"
    if v >= 1e3: return f"${v/1e3:.1f}K"
    return f"${v:,.0f}"


def fmt_subscript_price(p: float, sig_digits: int = 3) -> str:
    if p <= 0:
        return "$0"
    if p >= 1:
        return f"${p:,.4f}"
    s = f"{p:.20f}".split(".")[1]
    stripped = s.lstrip("0")
    n_zeros = len(s) - len(stripped)
    if n_zeros < 4:
        return f"${p:.{n_zeros + sig_digits}f}"
    digits = (stripped + "000")[:sig_digits]
    return f"$0.0{str(n_zeros).translate(_SUBSCRIPT)}{digits}"


def to_ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def parse_iso(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def fetch_geckoterminal(network: str, pool: str, w_start: datetime, w_end: datetime) -> list:
    """Page back through 5m OHLCV (limit 1000/call) until w_start is covered."""
    url = f"https://api.geckoterminal.com/api/v2/networks/{network}/pools/{pool}/ohlcv/minute"
    start_ms, end_ms = to_ms(w_start), to_ms(w_end)
    by_ts: dict[int, list] = {}
    before = int(end_ms / 1000) + 1
    for _ in range(12):  # safety cap: 12k candles
        for attempt in range(5):
            r = requests.get(url, params={"aggregate": 5, "limit": 1000, "before_timestamp": before},
                             headers=UA, timeout=20)
            if r.status_code == 429:
                time.sleep(3 * (attempt + 1))
                continue
            r.raise_for_status()
            break
        items = r.json().get("data", {}).get("attributes", {}).get("ohlcv_list") or []
        if not items:
            break
        for row in items:
            ts_s, o, h, l, c, _vol = row
            ts_ms = int(ts_s) * 1000
            if start_ms <= ts_ms <= end_ms:
                by_ts[ts_ms] = [ts_ms, float(o), float(h), float(l), float(c)]
        oldest_s = min(int(row[0]) for row in items)
        if oldest_s * 1000 <= start_ms:
            break
        before = oldest_s
        time.sleep(2.5)
    return [by_ts[k] for k in sorted(by_ts)]


def load_klines(cfg: dict) -> tuple[list, str]:
    token = cfg["token"]
    w_start = parse_iso(cfg["window_start_utc"])
    w_end = parse_iso(cfg["window_end_utc"])
    label = f"Binance Alpha ({cfg['chain']}, on-chain)"
    cache_path = CACHE / f"{token.lower()}_klines_5m_alpha.json"
    if cache_path.exists():
        cached = json.loads(cache_path.read_text(encoding="utf-8"))
        if cached.get("rows"):
            print(f"[{label}] cached {len(cached['rows'])} candles")
            return cached["rows"], label
    rows = fetch_geckoterminal(cfg["chain"], cfg["gecko_pool"], w_start, w_end)
    if not rows:
        raise SystemExit(f"No candles returned for {token} pool {cfg['gecko_pool']}")
    cache_path.write_text(
        json.dumps({"source": label, "rows": rows}, ensure_ascii=False),
        encoding="utf-8",
    )
    print(f"[{label}] fetched {len(rows)} candles")
    return rows, label


PALETTE = ["#d62728", "#2ca02c", "#9467bd", "#ff7f0e", "#17becf",
           "#8c564b", "#e377c2", "#7f7f7f", "#bcbd22"]


def _series(rows: list) -> tuple[list, list]:
    times = [datetime.fromtimestamp(r[0] / 1000, tz=timezone.utc) for r in rows]
    closes = [float(r[4]) for r in rows]
    return times, closes


def _events_in(events: list, lo: datetime, hi: datetime) -> list:
    out = []
    for ev in events:
        t = parse_iso(ev["iso_time_utc"])
        if lo <= t <= hi:
            out.append((ev, t))
    return out


def _style_price_ax(ax) -> None:
    ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda v, _p: fmt_subscript_price(v)))
    ax.grid(True, alpha=0.25)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)


def _format_xaxis(ax, lo: datetime, hi: datetime) -> None:
    ax.set_xlim(lo, hi)
    hours = max((hi - lo).total_seconds() / 3600, 1)
    ax.xaxis.set_major_locator(mdates.HourLocator(interval=max(1, int(hours / 10))))
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%m-%d %H:%M"))


def _nearest_close(times: list, closes: list, t: datetime) -> float:
    k = min(range(len(times)), key=lambda i: abs((times[i] - t).total_seconds()))
    return closes[k]


def _annotate_fanned(ax, ev_pairs: list, times: list, closes: list) -> None:
    """Fan labels into a stacked column near the top so boxes never overlap,
    with leader arrows back to each (clustered) vertical line."""
    n = len(ev_pairs)
    for i, (ev, t) in enumerate(ev_pairs):
        color = venue_color(ev["exchange"])
        ax.axvline(t, color=color, linestyle="--", linewidth=1.0, alpha=0.6)
        fx = 0.30 + 0.60 * (i / max(n - 1, 1))
        fy = 0.96 - 0.085 * i
        ax.annotate(
            f"{ev['exchange']}  {t.strftime('%m-%d %H:%M')} UTC",
            xy=(t, _nearest_close(times, closes, t)), xycoords="data",
            xytext=(fx, fy), textcoords="axes fraction",
            ha="left", va="top", fontsize=8.5, color=color,
            arrowprops=dict(arrowstyle="->", color=color, lw=0.9, alpha=0.8,
                            connectionstyle="arc3,rad=0.15"),
            bbox=dict(boxstyle="round,pad=0.3", fc="white", ec=color, lw=0.8),
        )


def _annotate_spread(ax, ev_pairs: list, times: list, closes: list) -> None:
    """For a zoomed panel: place every label in a headroom band above the
    price line (with a leader arrow down to the line) so labels never cover
    the data. Each event gets its own rung, so same-x events don't overlap."""
    xlo, xhi = ax.get_xlim()
    zc = [c for t, c in zip(times, closes) if xlo <= mdates.date2num(t) <= xhi] or closes
    pmin, pmax = min(zc), max(zc)
    span = (pmax - pmin) or pmax * 0.05 or 1.0
    base, step, min_dx = 0.07, 0.105, (xhi - xlo) * 0.17
    # Assign each label the lowest rung free of nearby labels, so only events
    # clustered in time stack; well-separated events stay on the low rung.
    placed: list[tuple[float, int]] = []
    rungs = []
    for _ev, t in ev_pairs:
        xnum = mdates.date2num(t)
        used = {r for xx, r in placed if abs(xx - xnum) < min_dx}
        rung = 0
        while rung in used:
            rung += 1
        placed.append((xnum, rung))
        rungs.append(rung)
    ax.set_ylim(pmin - span * 0.08, pmax + span * (base + step * (max(rungs) if rungs else 0) + 0.12))
    for i, (ev, t) in enumerate(ev_pairs):
        color = venue_color(ev["exchange"])
        ax.axvline(t, color=color, linestyle="--", linewidth=1.0, alpha=0.6)
        label_y = pmax + span * (base + step * rungs[i])
        ax.annotate(
            f"{ev['exchange']}  {t.strftime('%H:%M')} UTC",
            xy=(t, _nearest_close(times, closes, t)),
            xytext=(t, label_y), ha="center", va="bottom", fontsize=8.5, color=color,
            arrowprops=dict(arrowstyle="->", color=color, lw=1.0, alpha=0.85),
            bbox=dict(boxstyle="round,pad=0.3", fc="white", ec=color, lw=0.8),
        )


def _pre_move_close(times: list, closes: list, t: datetime) -> float:
    """Price level at the moment of the marked time, *before* it moved: the close
    of the candle preceding the one containing t. Candles are 5m, so a jump that
    completes inside one bar would otherwise snap the marker to the post-spike
    high — this keeps the arrow tip at the foot of the move."""
    idx = [i for i, tt in enumerate(times) if tt <= t]
    if not idx:
        return closes[0]
    return closes[max(idx[-1] - 1, 0)]


def _annotate_marks(ax, mark_pairs: list, times: list, closes: list) -> None:
    """Lightweight markers (e.g. an announcement time): no vertical line — just a
    small label parked in empty space above the price, with a leader arrow whose
    tip lands at the foot of the move at the marked time."""
    for mk, t in mark_pairs:
        ax.annotate(
            f"{mk['label']} {t.strftime('%H:%M')} UTC",
            xy=(t, _pre_move_close(times, closes, t)), xycoords="data",
            xytext=(0.62, 0.90), textcoords="axes fraction",
            ha="right", va="top", fontsize=8, color="#555555", style="italic",
            arrowprops=dict(arrowstyle="->", color="#555555", lw=0.9, alpha=0.85,
                            connectionstyle="arc3,rad=-0.2"),
            bbox=dict(boxstyle="round,pad=0.3", fc="white", ec="#999999", lw=0.7),
        )


def _title(ax, cfg, w_start, t_last) -> None:
    name = cfg.get("name", cfg["token"])
    date_range = f"{w_start.strftime('%d %b')}–{t_last.strftime('%d %b %Y')}"
    ax.set_title(f"{name} ({cfg['token']}) — Price Reaction to Exchange Listings, {date_range}",
                 fontsize=14, pad=14)
    fdv = cfg.get("fdv_usd")
    if fdv:
        ax.text(0.995, 1.012, f"FDV  {fmt_usd_compact(fdv)}",
                transform=ax.transAxes, ha="right", va="bottom",
                fontsize=13, fontweight="bold", color="#1f4e79",
                bbox=dict(boxstyle="round,pad=0.4", fc="#eaf2fb", ec="#1f4e79", lw=1.2))


def _launch_window(cfg, events, times) -> tuple[datetime, datetime]:
    w_start = parse_iso(cfg["window_start_utc"])
    ev_times = [parse_iso(e["iso_time_utc"]) for e in events]
    lo = min([w_start] + ev_times)
    hi = max(ev_times) + timedelta(hours=4)
    return lo, min(hi, times[-1])


def render_stagger(cfg, rows, source_label, out_path) -> None:
    events, token = cfg["events"], cfg["token"]
    w_start = parse_iso(cfg["window_start_utc"])
    times, closes = _series(rows)
    fig, ax = plt.subplots(figsize=(16, 9), dpi=150)
    ax.plot(times, closes, color="#1f4e79", linewidth=1.4,
            label=f"{token} close — {source_label}, 5m")
    lo = min(times[0], w_start)
    _format_xaxis(ax, lo, times[-1])
    _annotate_fanned(ax, _events_in(events, lo, times[-1]), times, closes)
    _title(ax, cfg, w_start, times[-1])
    ax.set_xlabel("Time (UTC)"); ax.set_ylabel("Price (USD)")
    _style_price_ax(ax)
    ax.legend(loc="upper right", frameon=False)
    fig.autofmt_xdate(); fig.tight_layout(); fig.savefig(out_path)
    print(f"wrote {out_path}")


def render_inset(cfg, rows, source_label, out_path) -> None:
    events, token = cfg["events"], cfg["token"]
    w_start = parse_iso(cfg["window_start_utc"])
    times, closes = _series(rows)
    fig, ax = plt.subplots(figsize=(16, 9), dpi=150)
    ax.plot(times, closes, color="#1f4e79", linewidth=1.4,
            label=f"{token} close — {source_label}, 5m")
    lo = min(times[0], w_start)
    _format_xaxis(ax, lo, times[-1])
    # light, unlabeled launch markers on the main week panel
    z_lo, z_hi = _launch_window(cfg, events, times)
    for i, (ev, t) in enumerate(_events_in(events, lo, times[-1])):
        ax.axvline(t, color=venue_color(ev["exchange"]), linestyle="--", linewidth=0.8, alpha=0.4)
    _title(ax, cfg, w_start, times[-1])
    ax.set_xlabel("Time (UTC)"); ax.set_ylabel("Price (USD)")
    _style_price_ax(ax)
    ax.legend(loc="upper right", frameon=False)
    # zoomed inset over the launch window
    axin = ax.inset_axes([0.40, 0.46, 0.56, 0.50])
    zt = [t for t in times if z_lo <= t <= z_hi]
    zc = [c for t, c in zip(times, closes) if z_lo <= t <= z_hi]
    axin.plot(zt, zc, color="#1f4e79", linewidth=1.4)
    _format_xaxis(axin, z_lo, z_hi)
    _annotate_spread(axin, _events_in(events, z_lo, z_hi), times, closes)
    _style_price_ax(axin)
    axin.tick_params(labelsize=7)
    axin.set_title("Launch window (zoom)", fontsize=9)
    for lbl in axin.get_xticklabels():
        lbl.set_rotation(30); lbl.set_ha("right")
    fig.autofmt_xdate(); fig.tight_layout(); fig.savefig(out_path)
    print(f"wrote {out_path}")


def render_twopanel(cfg, rows, source_label, out_path) -> None:
    events, token = cfg["events"], cfg["token"]
    w_start = parse_iso(cfg["window_start_utc"])
    times, closes = _series(rows)
    fig, (ax_top, ax_bot) = plt.subplots(
        2, 1, figsize=(16, 11), dpi=150, gridspec_kw={"height_ratios": [2, 1.4], "hspace": 0.32})
    # top: full week
    ax_top.plot(times, closes, color="#1f4e79", linewidth=1.4,
                label=f"{token} close — {source_label}, 5m")
    lo = min(times[0], w_start)
    _format_xaxis(ax_top, lo, times[-1])
    z_lo, z_hi = _launch_window(cfg, events, times)
    for i, (ev, t) in enumerate(_events_in(events, lo, times[-1])):
        ax_top.axvline(t, color=venue_color(ev["exchange"]), linestyle="--", linewidth=0.8, alpha=0.4)
    ax_top.axvspan(z_lo, z_hi, color="#ffd28a", alpha=0.25, label="launch window (below)")
    _title(ax_top, cfg, w_start, times[-1])
    ax_top.set_ylabel("Price (USD)")
    _style_price_ax(ax_top)
    ax_top.legend(loc="upper right", frameon=False)
    # bottom: launch-window zoom with full annotations
    zt = [t for t in times if z_lo <= t <= z_hi]
    zc = [c for t, c in zip(times, closes) if z_lo <= t <= z_hi]
    ax_bot.plot(zt, zc, color="#1f4e79", linewidth=1.5)
    _format_xaxis(ax_bot, z_lo, z_hi)
    _annotate_spread(ax_bot, _events_in(events, z_lo, z_hi), times, closes)
    _annotate_marks(ax_bot, _events_in(cfg.get("annotations", []), z_lo, z_hi), times, closes)
    ax_bot.set_title("Launch window — listing detail", fontsize=11)
    ax_bot.set_xlabel("Time (UTC)"); ax_bot.set_ylabel("Price (USD)")
    _style_price_ax(ax_bot)
    for lbl in ax_bot.get_xticklabels():
        lbl.set_rotation(30); lbl.set_ha("right")
    fig.savefig(out_path)
    print(f"wrote {out_path}")


LAYOUTS = {"stagger": render_stagger, "inset": render_inset, "twopanel": render_twopanel}


def main() -> None:
    if len(sys.argv) < 2:
        raise SystemExit("usage: python listing_chart.py listings/<token>.json [stagger|inset|twopanel|all]")
    cfg_path = Path(sys.argv[1])
    if not cfg_path.is_absolute():
        cfg_path = HERE / cfg_path
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    rows, src = load_klines(cfg)
    token = cfg["token"].lower()
    # Default: canonical two-panel chart. Pass a layout name (or "all") to
    # write suffixed comparison variants instead.
    if len(sys.argv) < 3:
        render_twopanel(cfg, rows, src, CHARTS / f"{token}_listing_reaction.png")
        return
    which = sys.argv[2]
    layouts = LAYOUTS if which == "all" else {which: LAYOUTS[which]}
    for name, fn in layouts.items():
        fn(cfg, rows, src, CHARTS / f"{token}_listing_reaction_{name}.png")


if __name__ == "__main__":
    main()
